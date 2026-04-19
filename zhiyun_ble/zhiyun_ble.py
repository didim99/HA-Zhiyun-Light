"""BLE client for Zhiyun lights.

Connection lifecycle is on-demand: the client connects when it has a command to
send, keeps the link warm for a short idle window so bursts of commands (HA's
brightness slider, scripts) don't pay the reconnect cost, then disconnects.
This lets BLE adapters be shared with other integrations and ESPHome proxies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Final

from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.client import BaseBleakClient  # type: ignore[import-not-found]
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    close_stale_connections,
    establish_connection,
)

from . import protocol as p
from .protocol import Command, ProtocolError

_LOGGER = logging.getLogger(__name__)

_CONNECT_TIMEOUT: Final = 20.0
_WRITE_TIMEOUT: Final = 10.0
_DEFAULT_IDLE_DISCONNECT: Final = 20.0
_POWER_SETTLE_DELAY: Final = 0.1
_INIT_STEP_DELAY: Final = 0.1


class ZhiyunError(Exception):
    """Base for all Zhiyun BLE errors."""


class ZhiyunConnectionError(ZhiyunError):
    """Failed to establish or maintain a GATT connection."""


class ZhiyunServiceError(ZhiyunError):
    """Required GATT service or characteristic is missing."""


@dataclass(frozen=True, slots=True)
class ZhiyunState:
    is_on: bool = False
    brightness: float = 0.0  # 0–100
    color_temp_kelvin: int = 5600
    firmware: str = ""
    model_code: str | None = None
    model_name: str | None = None


StateCallback = Callable[[ZhiyunState], None]


class ZhiyunDevice:
    """Async client for a single Zhiyun mesh light.

    Thread-safety: all public methods serialise through an internal lock, so
    they can be invoked concurrently from independent HA service calls without
    interleaving GATT writes.
    """

    def __init__(
        self,
        ble_device: BLEDevice,
        name: str,
        *,
        idle_disconnect_after: float = _DEFAULT_IDLE_DISCONNECT,
    ) -> None:
        self._ble_device = ble_device
        self._name = name
        self._idle_disconnect_after = idle_disconnect_after

        self._client: BaseBleakClient | None = None
        self._write_char: BleakGATTCharacteristic | None = None
        self._notify_char: BleakGATTCharacteristic | None = None

        self._lock = asyncio.Lock()
        self._sequence = 0
        self._disconnect_timer: asyncio.Task[None] | None = None
        self._callbacks: list[StateCallback] = []

        self._last_nonzero_brightness: float = 50.0
        code, friendly = p.resolve_model(name)
        self._state = ZhiyunState(model_code=code, model_name=friendly)

    # -----------------------------------------------------------------------------
    # Public surface
    # -----------------------------------------------------------------------------

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> ZhiyunState:
        return self._state

    def update_ble_device(self, ble_device: BLEDevice) -> None:
        """Swap the underlying `BLEDevice` when a fresh advert arrives.

        Home Assistant's Bluetooth layer surfaces new `BLEDevice` objects as
        ads are received; feeding the latest one into the retry-connector
        improves reconnect reliability when the adapter roams between proxies.
        """
        self._ble_device = ble_device

    def register_callback(self, callback: StateCallback) -> Callable[[], None]:
        self._callbacks.append(callback)

        def _unsubscribe() -> None:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

        return _unsubscribe

    async def async_turn_on(
        self,
        *,
        brightness: float | None = None,
        color_temp_kelvin: int | None = None,
    ) -> None:
        async with self._lock:
            await self._ensure_connected()
            if not self._state.is_on:
                await self._power(on=True)
                await asyncio.sleep(_POWER_SETTLE_DELAY)
            if color_temp_kelvin is not None:
                await self._color_temp(color_temp_kelvin)
            target = brightness if brightness is not None else self._last_nonzero_brightness
            if target > 0:
                await self._brightness(target)
        self._arm_idle_disconnect()

    async def async_turn_off(self) -> None:
        async with self._lock:
            await self._ensure_connected()
            await self._power(on=False)
        self._arm_idle_disconnect()

    async def async_set_brightness(self, value: float) -> None:
        async with self._lock:
            await self._ensure_connected()
            if value <= 0:
                await self._power(on=False)
            else:
                if not self._state.is_on:
                    await self._power(on=True)
                    await asyncio.sleep(_POWER_SETTLE_DELAY)
                await self._brightness(value)
        self._arm_idle_disconnect()

    async def async_set_color_temp_kelvin(self, kelvin: int) -> None:
        async with self._lock:
            await self._ensure_connected()
            await self._color_temp(kelvin)
        self._arm_idle_disconnect()

    async def async_disconnect(self) -> None:
        self._cancel_idle_disconnect()
        async with self._lock:
            await self._disconnect_locked()

    # -----------------------------------------------------------------------------
    # Command primitives — callers must hold `_lock` and be connected
    # -----------------------------------------------------------------------------

    async def _power(self, *, on: bool) -> None:
        await self._write(Command.POWER_STATE, p.encode_power(on))
        self._update_state(is_on=on)

    async def _brightness(self, value: float) -> None:
        clamped = max(p.BRIGHTNESS_MIN, min(p.BRIGHTNESS_MAX, value))
        await self._write(Command.SET_BRIGHTNESS, p.encode_brightness(clamped))
        if clamped > 0:
            self._last_nonzero_brightness = clamped
        self._update_state(brightness=clamped, is_on=clamped > 0)

    async def _color_temp(self, kelvin: int) -> None:
        clamped = max(p.COLOR_TEMP_MIN_KELVIN, min(p.COLOR_TEMP_MAX_KELVIN, int(kelvin)))
        await self._write(Command.SET_COLOR_TEMP, p.encode_color_temp(clamped))
        self._update_state(color_temp_kelvin=clamped)

    async def _write(self, command: Command, payload: bytes) -> None:
        if self._client is None or self._write_char is None:
            raise ZhiyunConnectionError("not connected")
        frame = p.build_packet(command, payload, self._next_sequence())
        try:
            async with asyncio.timeout(_WRITE_TIMEOUT):
                await self._client.write_gatt_char(self._write_char, frame, response=False)
        except TimeoutError as err:
            raise ZhiyunConnectionError(f"write timeout on {self._name}") from err

    def _next_sequence(self) -> int:
        seq = self._sequence
        self._sequence = (self._sequence + 1) & 0xFFFF
        return seq

    # -----------------------------------------------------------------------------
    # Connection management
    # -----------------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._client is not None and self._client.is_connected:
            return

        self._cancel_idle_disconnect()
        _LOGGER.debug("%s: establishing GATT connection", self._name)
        await close_stale_connections(self._ble_device)

        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._ble_device,
                    self._name,
                    disconnected_callback=self._on_unexpected_disconnect,
                    ble_device_callback=lambda: self._ble_device,
                )
        except BleakNotFoundError as err:
            raise ZhiyunConnectionError(f"{self._name} not in range") from err
        except TimeoutError as err:
            raise ZhiyunConnectionError(f"{self._name} connect timeout") from err

        service = client.services.get_service(p.SERVICE_UUID)
        if service is None:
            await client.disconnect()
            raise ZhiyunServiceError(f"service {p.SERVICE_UUID} missing on {self._name}")

        write_char = service.get_characteristic(p.WRITE_CHAR_UUID)
        notify_char = service.get_characteristic(p.NOTIFY_CHAR_UUID)
        if write_char is None or notify_char is None:
            await client.disconnect()
            raise ZhiyunServiceError(f"characteristics missing on {self._name}")

        await client.start_notify(notify_char, self._on_notify)

        self._client = client
        self._write_char = write_char
        self._notify_char = notify_char

        await self._run_init_handshake()

    async def _run_init_handshake(self) -> None:
        """Query device identity and current state on connect.

        Order and pacing mirror the iOS app; the device will drop subsequent
        commands if these queries aren't issued first.
        """
        handshake = (
            (Command.GET_DEVICE_INFO, b""),
            (Command.GET_DEVICE_NAME, b""),
            (Command.GET_FIRMWARE, b""),
            (Command.READ_DEVICE_STATE, p.encode_query()),
            (Command.QUERY_BRIGHTNESS, p.encode_query()),
        )
        for command, payload in handshake:
            try:
                await self._write(command, payload)
            except ZhiyunConnectionError:
                _LOGGER.warning("%s: init step %s failed", self._name, command.name)
                return
            await asyncio.sleep(_INIT_STEP_DELAY)

    def _on_unexpected_disconnect(self, _client: BaseBleakClient) -> None:
        _LOGGER.debug("%s: GATT link dropped", self._name)
        self._client = None
        self._write_char = None
        self._notify_char = None

    def _arm_idle_disconnect(self) -> None:
        self._cancel_idle_disconnect()
        self._disconnect_timer = asyncio.create_task(self._idle_disconnect())

    def _cancel_idle_disconnect(self) -> None:
        if self._disconnect_timer is not None and not self._disconnect_timer.done():
            self._disconnect_timer.cancel()
        self._disconnect_timer = None

    async def _idle_disconnect(self) -> None:
        try:
            await asyncio.sleep(self._idle_disconnect_after)
        except asyncio.CancelledError:
            return
        async with self._lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        client = self._client
        self._client = None
        self._write_char = None
        self._notify_char = None
        if client is None or not client.is_connected:
            return
        try:
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                await client.disconnect()
        except Exception as err:  # noqa: BLE001 — bleak raises a wide variety
            _LOGGER.debug("%s: disconnect error: %s", self._name, err)

    # -----------------------------------------------------------------------------
    # Notification handling
    # -----------------------------------------------------------------------------

    def _on_notify(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        try:
            response = p.parse_frame(bytes(data))
        except ProtocolError as err:
            _LOGGER.debug("%s: dropping frame: %s", self._name, err)
            return
        self._apply_response(response.command, response.payload)

    def _apply_response(self, command: int, payload: bytes) -> None:
        match command:
            case Command.SET_BRIGHTNESS:
                if (value := p.decode_brightness_response(payload)) is not None:
                    self._update_state(brightness=value, is_on=value > 0)
            case Command.SET_COLOR_TEMP:
                if (kelvin := p.decode_color_temp_response(payload)) is not None:
                    self._update_state(color_temp_kelvin=kelvin)
            case Command.POWER_STATE:
                if (is_on := p.decode_power_response(payload)) is not None:
                    self._update_state(is_on=is_on)
            case Command.GET_FIRMWARE:
                self._update_state(firmware=p.decode_firmware_response(payload))

    # -----------------------------------------------------------------------------
    # State propagation
    # -----------------------------------------------------------------------------

    def _update_state(self, **changes: object) -> None:
        new_state = replace(self._state, **changes)  # type: ignore[arg-type]
        if new_state == self._state:
            return
        self._state = new_state
        for callback in tuple(self._callbacks):
            try:
                callback(new_state)
            except Exception:  # noqa: BLE001 — callbacks are user code
                _LOGGER.exception("%s: state callback raised", self._name)
