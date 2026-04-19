"""Zhiyun BLE integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .zhiyun_ble import ZhiyunDevice

PLATFORMS: tuple[Platform, ...] = (Platform.LIGHT,)


@dataclass(slots=True)
class ZhiyunRuntimeData:
    device: ZhiyunDevice


type ZhiyunConfigEntry = ConfigEntry[ZhiyunRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: ZhiyunConfigEntry) -> bool:
    address: str = entry.data[CONF_ADDRESS]
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(f"Zhiyun device {address} not in range")

    device = ZhiyunDevice(ble_device, entry.title)
    entry.runtime_data = ZhiyunRuntimeData(device=device)

    @callback
    def _refresh_ble_device(
        service_info: BluetoothServiceInfoBleak,
        _change: BluetoothChange,
    ) -> None:
        device.update_ble_device(service_info.device)

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _refresh_ble_device,
            BluetoothCallbackMatcher(address=address, connectable=True),
            BluetoothScanningMode.PASSIVE,
        )
    )
    entry.async_on_unload(device.async_disconnect)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ZhiyunConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
