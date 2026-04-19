"""Config flow for Zhiyun BLE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN
from .protocol import is_supported_name, resolve_model


class ZhiyunConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zhiyun BLE lights."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()

        if not is_supported_name(discovery_info.name):
            return self.async_abort(reason="not_supported")

        self._discovery = discovery_info
        _, friendly = resolve_model(discovery_info.name)
        self.context["title_placeholders"] = {"name": friendly or discovery_info.name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None

        if user_input is not None:
            return self._create_entry(self._discovery)

        _, friendly = resolve_model(self._discovery.name)
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": friendly or self._discovery.name},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._create_entry(self._discovered[address])

        current = self._async_current_ids()
        self._discovered = {
            info.address: info
            for info in async_discovered_service_info(self.hass)
            if info.address not in current and is_supported_name(info.name)
        }
        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: self._label_for(info)
                            for address, info in self._discovered.items()
                        }
                    )
                }
            ),
        )

    def _create_entry(self, info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        _, friendly = resolve_model(info.name)
        title = friendly or info.name or info.address
        return self.async_create_entry(
            title=f"{title} ({info.address})",
            data={CONF_ADDRESS: info.address},
        )

    @staticmethod
    def _label_for(info: BluetoothServiceInfoBleak) -> str:
        _, friendly = resolve_model(info.name)
        label = friendly or info.name or "Zhiyun Light"
        return f"{label} ({info.address})"
