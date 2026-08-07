"""Login por QR do Smart Life (tuya-device-sharing-sdk) para o Tuya BLE Selfhost.

Alternativa ao login legado da Tuya IoT Platform (Access ID/Secret): o usuário
escaneia um QR com o app Smart Life uma única vez; o token fica persistido em
Store e os próximos fluxos nem pedem QR. As credenciais BLE (uuid/local_key)
saem da lista de dispositivos da conta e são casadas com o anúncio Bluetooth
pelo uuid (decodificado do manufacturer_data, sem depender de MAC de fábrica).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from Crypto.Cipher import AES
from tuya_sharing import LoginControl, Manager, SharingTokenListener

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CATEGORY,
    CONF_DEVICE_NAME,
    CONF_FUNCTIONS,
    CONF_LOCAL_KEY,
    CONF_PRODUCT_ID,
    CONF_PRODUCT_MODEL,
    CONF_PRODUCT_NAME,
    CONF_STATUS_RANGE,
    CONF_UUID,
    DOMAIN,
)
from .tuya_ble.const import MANUFACTURER_DATA_ID, SERVICE_UUID

_LOGGER = logging.getLogger(__name__)

# Mesmo client id/schema usados pelo tuya-selfhost (e pelo Tuya oficial do HA)
TUYA_CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
TUYA_SCHEMA = "haauthorize"

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.sharing_token"

CONF_DEVICE_ID = "device_id"


def decode_uuid_from_advertisement(service_info: Any) -> str | None:
    """Extrai o uuid do dispositivo do anúncio BLE (igual ao pareamento da lib)."""
    try:
        service_data = (service_info.service_data or {}).get(SERVICE_UUID)
        manufacturer_data = (service_info.manufacturer_data or {}).get(
            MANUFACTURER_DATA_ID
        )
        if (
            not service_data
            or len(service_data) < 2
            or service_data[0] != 0
            or not manufacturer_data
            or len(manufacturer_data) <= 6
        ):
            return None
        raw_product_id = bytes(service_data[1:])
        key = hashlib.md5(raw_product_id).digest()
        cipher = AES.new(key, AES.MODE_CBC, key)
        raw_uuid = cipher.decrypt(bytes(manufacturer_data[6:]))
        return raw_uuid.decode("utf-8")
    except Exception:  # anúncio fora do padrão não pode derrubar o fluxo
        _LOGGER.debug("Anúncio BLE sem uuid decodificável", exc_info=True)
        return None


class _StoreTokenListener(SharingTokenListener):
    """Persiste tokens renovados de volta no Store."""

    def __init__(self, sharing: "SharingCloud") -> None:
        self._sharing = sharing

    def update_token(self, token_info: dict[str, Any]) -> None:
        self._sharing.schedule_token_update(token_info)


class SharingCloud:
    """Sessão cloud via sharing SDK, com token persistido."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._login_control = LoginControl()
        self._auth: dict[str, Any] | None = None
        self._qr_code: str | None = None
        self.user_code: str | None = None
        self._manager: Manager | None = None

    async def async_restore(self) -> bool:
        """Tenta restaurar a sessão persistida."""
        data = await self._store.async_load()
        if not data or not data.get("token_info"):
            return False
        self._auth = data
        self.user_code = data.get("user_code")
        return True

    async def async_get_qr_code(self, user_code: str) -> str | None:
        response = await self._hass.async_add_executor_job(
            self._login_control.qr_code, TUYA_CLIENT_ID, TUYA_SCHEMA, user_code
        )
        if response.get("success", False):
            self.user_code = user_code
            self._qr_code = response["result"]["qrcode"]
            return self._qr_code
        _LOGGER.warning("Falha ao gerar QR code: %s", response)
        return None

    async def async_login(self) -> bool:
        if not self._qr_code or not self.user_code:
            return False
        success, info = await self._hass.async_add_executor_job(
            self._login_control.login_result,
            self._qr_code,
            TUYA_CLIENT_ID,
            self.user_code,
        )
        if not success:
            _LOGGER.warning("Login por QR falhou: %s", info)
            return False
        self._auth = {
            "user_code": self.user_code,
            "terminal_id": info["terminal_id"],
            "endpoint": info["endpoint"],
            "token_info": {
                "t": info["t"],
                "uid": info["uid"],
                "expire_time": info["expire_time"],
                "access_token": info["access_token"],
                "refresh_token": info["refresh_token"],
            },
        }
        await self._store.async_save(self._auth)
        return True

    def schedule_token_update(self, token_info: dict[str, Any]) -> None:
        if not self._auth:
            return
        self._auth["token_info"] = token_info

        async def _save() -> None:
            await self._store.async_save(self._auth)

        self._hass.add_job(_save)

    async def async_get_ble_credentials(self) -> dict[str, dict[str, Any]] | None:
        """Lista dispositivos da conta e devolve credenciais BLE por uuid."""
        if not self._auth:
            return None
        if self._manager is None:
            self._manager = Manager(
                TUYA_CLIENT_ID,
                self._auth["user_code"],
                self._auth["terminal_id"],
                self._auth["endpoint"],
                self._auth["token_info"],
                _StoreTokenListener(self),
            )
        try:
            await self._hass.async_add_executor_job(self._manager.update_device_cache)
        except Exception as err:
            _LOGGER.warning("Sessão do QR expirada/inválida: %s", err)
            return None

        result: dict[str, dict[str, Any]] = {}
        for device in self._manager.device_map.values():
            uuid = getattr(device, "uuid", None)
            local_key = getattr(device, "local_key", None)
            if not uuid or not local_key:
                continue
            functions = [
                {"code": f.code, "type": f.type, "values": f.values}
                for f in (device.function or {}).values()
            ]
            status_range = [
                {"code": r.code, "type": r.type, "values": r.values}
                for r in (device.status_range or {}).values()
            ]
            result[uuid] = {
                CONF_UUID: uuid,
                CONF_LOCAL_KEY: local_key,
                CONF_DEVICE_ID: device.id,
                CONF_CATEGORY: device.category,
                CONF_PRODUCT_ID: device.product_id,
                CONF_DEVICE_NAME: device.name,
                CONF_PRODUCT_MODEL: getattr(device, "model", "") or "",
                CONF_PRODUCT_NAME: device.product_name,
                CONF_FUNCTIONS: functions,
                CONF_STATUS_RANGE: status_range,
            }
        return result
