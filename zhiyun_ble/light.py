"""Light platform for Zhiyun BLE."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ZhiyunConfigEntry
from .const import DOMAIN
from .protocol import COLOR_TEMP_MAX_KELVIN, COLOR_TEMP_MIN_KELVIN
from .zhiyun_ble import ZhiyunDevice, ZhiyunError, ZhiyunState

_HA_BRIGHTNESS_MAX = 255


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZhiyunConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ZhiyunLight(entry.runtime_data.device, entry.entry_id)])


class ZhiyunLight(LightEntity):
    """Home Assistant light entity for a Zhiyun BLE fixture."""

    _attr_has_entity_name = True
    _attr_name = None  # Use the device's own name
    _attr_should_poll = False
    _attr_color_mode = ColorMode.COLOR_TEMP
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = COLOR_TEMP_MIN_KELVIN
    _attr_max_color_temp_kelvin = COLOR_TEMP_MAX_KELVIN

    def __init__(self, device: ZhiyunDevice, entry_id: str) -> None:
        self._device = device
        self._attr_unique_id = device.address
        state = device.state
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.address)},
            connections={(CONNECTION_BLUETOOTH, device.address)},
            name=device.name,
            manufacturer="Zhiyun",
            model=state.model_name or state.model_code,
            sw_version=state.firmware or None,
        )
        self._apply_state(state)

    # ---------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.register_callback(self._on_state_update))
        self.async_on_remove(
            self._device.register_availability_callback(self._on_availability_changed)
        )
        self._attr_available = self._device.available

    @callback
    def _on_state_update(self, state: ZhiyunState) -> None:
        self._apply_state(state)
        self._sync_device_registry(state)
        self.async_write_ha_state()

    @callback
    def _on_availability_changed(self, available: bool) -> None:
        self._attr_available = available
        self.async_write_ha_state()

    @callback
    def _apply_state(self, state: ZhiyunState) -> None:
        self._attr_is_on = state.is_on
        self._attr_brightness = _pct_to_ha(state.brightness)
        self._attr_color_temp_kelvin = state.color_temp_kelvin

    @callback
    def _sync_device_registry(self, state: ZhiyunState) -> None:
        """Propagate firmware/model data learned post-setup into the device registry."""
        if not state.firmware and not state.model_name:
            return
        registry = dr.async_get(self.hass)
        entry = registry.async_get_device(identifiers={(DOMAIN, self._device.address)})
        if entry is None:
            return
        changes: dict[str, str] = {}
        if state.firmware and entry.sw_version != state.firmware:
            changes["sw_version"] = state.firmware
        if state.model_name and entry.model != state.model_name:
            changes["model"] = state.model_name
        if changes:
            registry.async_update_device(entry.id, **changes)

    # ---------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        brightness_pct: float | None = None
        if (ha_brightness := kwargs.get(ATTR_BRIGHTNESS)) is not None:
            brightness_pct = _ha_to_pct(ha_brightness)

        color_temp_kelvin: int | None = kwargs.get(ATTR_COLOR_TEMP_KELVIN)

        try:
            await self._device.async_turn_on(
                brightness=brightness_pct,
                color_temp_kelvin=color_temp_kelvin,
            )
        except ZhiyunError as err:
            raise _to_ha_error(err) from err

    async def async_turn_off(self, **_: Any) -> None:
        try:
            await self._device.async_turn_off()
        except ZhiyunError as err:
            raise _to_ha_error(err) from err


def _ha_to_pct(ha_brightness: int) -> float:
    return max(0.0, min(100.0, ha_brightness / _HA_BRIGHTNESS_MAX * 100.0))


def _pct_to_ha(pct: float) -> int:
    return int(round(max(0.0, min(100.0, pct)) / 100.0 * _HA_BRIGHTNESS_MAX))


def _to_ha_error(err: ZhiyunError) -> HomeAssistantError:
    """Map library exceptions to HA-facing errors at a single choke point."""
    return HomeAssistantError(str(err))
