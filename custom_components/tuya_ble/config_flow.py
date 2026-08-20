"""Config flow for Tuya BLE integration."""

from __future__ import annotations

import logging
from functools import partial
import pycountry
from typing import Any

import voluptuous as vol
from tuya_iot import AuthType

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import (
    CONF_ADDRESS, 
    CONF_DEVICE_ID,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers.selector import (
    QrCodeSelector,
    QrCodeSelectorConfig,
    QrErrorCorrectionLevel,
)

from .sharing import SharingCloud, decode_uuid_from_advertisement
from .tuya_ble import SERVICE_UUID, TuyaBLEDeviceCredentials

from .const import (
    TUYA_COUNTRIES,
    TUYA_SMART_APP,
    SMARTLIFE_APP,
    TUYA_RESPONSE_SUCCESS,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_APP_TYPE,
    CONF_AUTH_TYPE,
    CONF_ENDPOINT,
    DOMAIN,
)
from .devices import TuyaBLEData, get_device_readable_name
from .cloud import HASSTuyaBLEDeviceManager

_LOGGER = logging.getLogger(__name__)


async def _try_login(
    manager: HASSTuyaBLEDeviceManager,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> dict[str, Any] | None:
    response: dict[Any, Any] | None
    data: dict[str, Any]

    country = [
        country
        for country in TUYA_COUNTRIES
        if country.name == user_input[CONF_COUNTRY_CODE]
    ][0]

    data = {
        CONF_ENDPOINT: country.endpoint,
        CONF_AUTH_TYPE: AuthType.CUSTOM,
        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID],
        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET],
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_COUNTRY_CODE: country.country_code,
    }

    for app_type in (TUYA_SMART_APP, SMARTLIFE_APP, ""):
        data[CONF_APP_TYPE] = app_type
        if app_type == "":
            data[CONF_AUTH_TYPE] = AuthType.CUSTOM
        else:
            data[CONF_AUTH_TYPE] = AuthType.SMART_HOME

        response = await manager._login(data, True)

        if response.get(TUYA_RESPONSE_SUCCESS, False):
            return data

    errors["base"] = "login_error"
    if response:
        placeholders.update(
            {
                TUYA_RESPONSE_CODE: response.get(TUYA_RESPONSE_CODE),
                TUYA_RESPONSE_MSG: response.get(TUYA_RESPONSE_MSG),
            }
        )

    return None


def _show_login_form(
    flow: FlowHandler,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
    def_country_name: str | None = None,
) -> FlowResult:
    """Shows the Tuya IOT platform login form."""
    if user_input is not None and user_input.get(CONF_COUNTRY_CODE) is not None:
        for country in TUYA_COUNTRIES:
            if country.country_code == user_input[CONF_COUNTRY_CODE]:
                user_input[CONF_COUNTRY_CODE] = country.name
                break

    return flow.async_show_form(
        step_id="login",
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=user_input.get(CONF_COUNTRY_CODE, def_country_name),
                ): vol.In(
                    # We don't pass a dict {code:name} because country codes can be duplicate.
                    [country.name for country in TUYA_COUNTRIES]
                ),
                vol.Required(
                    CONF_ACCESS_ID, default=user_input.get(CONF_ACCESS_ID, "")
                ): str,
                vol.Required(
                    CONF_ACCESS_SECRET,
                    default=user_input.get(CONF_ACCESS_SECRET, ""),
                ): str,
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }
        ),
        errors=errors,
        description_placeholders=placeholders,
    )


async def _async_get_default_country_name(hass) -> str | None:
    """Resolve the system country name without blocking the event loop."""
    if not hass.config.country:
        return None

    def_country = await hass.async_add_executor_job(
        partial(pycountry.countries.get, alpha_2=hass.config.country)
    )
    return def_country.name if def_country else None


class TuyaBLEOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle a Tuya BLE options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return await self.async_step_login(user_input)

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}
        credentials: TuyaBLEDeviceCredentials | None = None
        address: str | None = self.config_entry.data.get(CONF_ADDRESS)

        if user_input is not None:
            entry: TuyaBLEData | None = None
            domain_data = self.hass.data.get(DOMAIN)
            if domain_data:
                entry = domain_data.get(self.config_entry.entry_id)
            if entry:
                login_data = await _try_login(
                    entry.manager,
                    user_input,
                    errors,
                    placeholders,
                )
                if login_data:
                    credentials = await entry.manager.get_device_credentials(
                        address, True, True
                    )
                    if credentials:
                        return self.async_create_entry(
                            title=self.config_entry.title,
                            data=entry.manager.data,
                        )
                    else:
                        errors["base"] = "device_not_registered"

        if user_input is None:
            user_input = {}
            user_input.update(self.config_entry.options)

        def_country_name = None
        if not user_input.get(CONF_COUNTRY_CODE):
            def_country_name = await _async_get_default_country_name(self.hass)

        return _show_login_form(
            self,
            user_input,
            errors,
            placeholders,
            def_country_name,
        )


class TuyaBLEConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Tuya BLE."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        super().__init__()
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._data: dict[str, Any] = {}
        self._manager: HASSTuyaBLEDeviceManager | None = None
        self._get_device_info_error = False
        self._sharing: SharingCloud | None = None
        self._sharing_creds: dict[str, dict[str, Any]] | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle the bluetooth discovery step."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        await self._manager.build_cache()
        self.context["title_placeholders"] = {
            "name": await get_device_readable_name(
                discovery_info,
                self._manager,
            )
        }
        return await self.async_step_auth_method()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step."""
        if self._manager is None:
            self._manager = HASSTuyaBLEDeviceManager(self.hass, self._data)
        await self._manager.build_cache()
        return await self.async_step_auth_method()

    # --- Caminho novo: login por QR do Smart Life (sharing SDK) ---

    async def async_step_auth_method(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Sessão salva entra direto; senão oferece QR ou login legado."""
        if self._sharing is None:
            self._sharing = SharingCloud(self.hass)
            if await self._sharing.async_restore():
                creds = await self._sharing.async_get_ble_credentials()
                if creds:
                    self._sharing_creds = creds
                    return await self.async_step_sharing_device()
        return self.async_show_menu(
            step_id="auth_method",
            menu_options=["qr", "login"],
        )

    async def async_step_qr(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Pede o código de usuário do app Smart Life e gera o QR."""
        errors: dict[str, str] = {}
        if user_input is not None:
            qr = await self._sharing.async_get_qr_code(
                user_input["user_code"].strip()
            )
            if qr:
                return await self.async_step_scan()
            errors["base"] = "qr_error"
        return self.async_show_form(
            step_id="qr",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "user_code",
                        default=(self._sharing.user_code or ""),
                    ): str,
                }
            ),
            errors=errors,
        )

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Mostra o QR; no submit confere se o app autorizou."""
        errors: dict[str, str] = {}
        if user_input is not None:
            ok = await self._sharing.async_login()
            if not ok:
                # o app pode demorar a propagar a autorização
                import asyncio as _asyncio
                await _asyncio.sleep(2)
                ok = await self._sharing.async_login()
            if ok:
                creds = await self._sharing.async_get_ble_credentials()
                if creds:
                    self._sharing_creds = creds
                    return await self.async_step_sharing_device()
                errors["base"] = "no_devices"
            else:
                errors["base"] = "qr_login_error"
        # mantém o MESMO QR entre renders; só gera novo se ainda não existe
        qr = self._sharing.qr_code or await self._sharing.async_get_qr_code(
            self._sharing.user_code
        )
        if not qr:
            return await self.async_step_qr()
        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr"): QrCodeSelector(
                        config=QrCodeSelectorConfig(
                            data=f"tuyaSmart--qrLogin?token={qr}",
                            scale=5,
                            error_correction_level=QrErrorCorrectionLevel.QUARTILE,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_sharing_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Casa o BLE descoberto com o dispositivo da conta e cria a entry."""
        errors: dict[str, str] = {}
        creds_map = self._sharing_creds or {}

        if user_input is not None:
            address = user_input.get(CONF_ADDRESS) or (
                self._discovery_info.address if self._discovery_info else None
            )
            creds = creds_map.get(user_input["cloud_device"])
            if address and creds:
                await self.async_set_unique_id(address, raise_on_progress=False)
                self._abort_if_unique_id_configured()
                options = {CONF_ADDRESS: address, **creds}
                return self.async_create_entry(
                    title=creds["device_name"],
                    data={CONF_ADDRESS: address},
                    options=options,
                )
            errors["base"] = "device_not_registered"

        # dispositivos BLE visíveis agora
        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery.address in current_addresses
                    or discovery.service_data is None
                    or SERVICE_UUID not in discovery.service_data.keys()
                ):
                    continue
                self._discovered_devices[discovery.address] = discovery
        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")

        # pré-seleção: casa o uuid do anúncio com a conta
        matched: str | None = None
        target = self._discovery_info or next(iter(self._discovered_devices.values()))
        adv_uuid = decode_uuid_from_advertisement(target)
        if adv_uuid and adv_uuid in creds_map:
            matched = adv_uuid

        schema: dict[Any, Any] = {}
        if not self._discovery_info:
            schema[vol.Required(CONF_ADDRESS)] = vol.In(
                {
                    si.address: f"{si.address} ({si.name or 'BLE'})"
                    for si in self._discovered_devices.values()
                }
            )
        configured_ids = {
            e.data.get("device_id") or e.options.get("device_id")
            for e in self.hass.config_entries.async_entries("tuya_selfhost")
        } | {
            e.options.get("device_id")
            for e in self.hass.config_entries.async_entries(DOMAIN)
        }
        cloud_options = {
            uuid: f"{c['device_name']} ({c['product_name'] or c['category']})"
            for uuid, c in sorted(
                creds_map.items(), key=lambda kv: kv[1]["device_name"]
            )
            if c.get("device_id") not in configured_ids
        }
        if not cloud_options:
            return self.async_abort(reason="no_devices")
        schema[
            vol.Required("cloud_device", default=matched or list(cloud_options)[0])
        ] = vol.In(cloud_options)

        return self.async_show_form(
            step_id="sharing_device",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_login(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the Tuya IOT platform login step."""
        data: dict[str, Any] | None = None
        errors: dict[str, str] = {}
        placeholders: dict[str, Any] = {}

        if user_input is not None:
            data = await _try_login(
                self._manager,
                user_input,
                errors,
                placeholders,
            )
            if data:
                self._data.update(data)
                return await self.async_step_device()

        if user_input is None:
            user_input = {}
            if self._discovery_info:
                await self._manager.get_device_credentials(
                    self._discovery_info.address,
                    False,
                    True,
                )
            if self._data is None or len(self._data) == 0:
                self._manager.get_login_from_cache()
            if self._data is not None and len(self._data) > 0:
                user_input.update(self._data)

        def_country_name = None
        if not user_input.get(CONF_COUNTRY_CODE):
            def_country_name = await _async_get_default_country_name(self.hass)

        return _show_login_form(
            self,
            user_input,
            errors,
            placeholders,
            def_country_name,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the user step to pick discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery_info = self._discovered_devices[address]
            local_name = await get_device_readable_name(discovery_info, self._manager)
            await self.async_set_unique_id(
                discovery_info.address, raise_on_progress=False
            )
            self._abort_if_unique_id_configured()
            credentials = await self._manager.get_device_credentials(
                discovery_info.address, self._get_device_info_error, True
            )
            self._data[CONF_ADDRESS] = discovery_info.address
            if credentials is None:
                self._get_device_info_error = True
                errors["base"] = "device_not_registered"
            else:
                return self.async_create_entry(
                    title=local_name,
                    data={CONF_ADDRESS: discovery_info.address},
                    options=self._data,
                )

        if discovery := self._discovery_info:
            self._discovered_devices[discovery.address] = discovery
        else:
            current_addresses = self._async_current_ids()
            for discovery in async_discovered_service_info(self.hass):
                if (
                    discovery.address in current_addresses
                    or discovery.address in self._discovered_devices
                    or discovery.service_data is None
                    or not SERVICE_UUID in discovery.service_data.keys()
                ):
                    continue
                self._discovered_devices[discovery.address] = discovery

        if not self._discovered_devices:
            return self.async_abort(reason="no_unconfigured_devices")

        def_address: str
        if user_input:
            def_address = user_input.get(CONF_ADDRESS)
        else:
            def_address = list(self._discovered_devices)[0]

        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS,
                        default=def_address,
                    ): vol.In(
                        {
                            service_info.address: await get_device_readable_name(
                                service_info,
                                self._manager,
                            )
                            for service_info in self._discovered_devices.values()
                        }
                    ),
                },
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> TuyaBLEOptionsFlow:
        """Get the options flow for this handler."""
        return TuyaBLEOptionsFlow(config_entry)
