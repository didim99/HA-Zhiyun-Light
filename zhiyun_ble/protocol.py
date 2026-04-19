"""Zhiyun BLE wire protocol — pure, side-effect-free.

Decoded from Bluetooth sniff of the official Zyvega iOS app. Covers
Molus / FIVERAY / CINEPEER COB lights built on the PL1xx mesh firmware.

Frame layout (all multi-byte fields little-endian):

    +--------+--------+----------+----------+----------+----------+--------+
    | magic  | length | direction|   seq    | command  | payload  |  crc   |
    | 24 3C  |   u16  |   u16    |   u16    |   u16    |   bytes  |  u16   |
    +--------+--------+----------+----------+----------+----------+--------+

`length` = size of (direction + seq + command + payload), excluding CRC.
`crc` = CRC-16/XMODEM over (direction + seq + command + payload).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

# --- Service / characteristic UUIDs ------------------------------------------------

SERVICE_UUID: Final = "0000fee9-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID: Final = "d44bc439-abfd-45a2-b575-925416129600"
NOTIFY_CHAR_UUID: Final = "d44bc439-abfd-45a2-b575-925416129601"

# --- Frame constants ---------------------------------------------------------------

_MAGIC_HEADER: Final = b"\x24\x3c"
_DIRECTION_REQUEST: Final = b"\x00\x01"
_SUBCMD_CONTROL: Final = b"\x03\x80"
_HEADER_LEN: Final = 4  # magic (2) + length (2)
_FRAME_OVERHEAD: Final = _HEADER_LEN + 2  # + crc


class Command(IntEnum):
    SET_BRIGHTNESS = 0x1001
    SET_COLOR_TEMP = 0x1002
    POWER_STATE = 0x1008
    QUERY_BRIGHTNESS = 0x1009
    READ_DEVICE_STATE = 0x0006
    GET_DEVICE_INFO = 0x2005
    GET_DEVICE_NAME = 0x2003
    GET_FIRMWARE = 0x8001


# --- Value ranges ------------------------------------------------------------------

BRIGHTNESS_MIN: Final[float] = 0.0
BRIGHTNESS_MAX: Final[float] = 100.0
COLOR_TEMP_MIN_KELVIN: Final[int] = 2700
COLOR_TEMP_MAX_KELVIN: Final[int] = 6500


# --- Device catalogue --------------------------------------------------------------

MODEL_NAMES: Final[dict[str, str]] = {
    "PL103": "MOLUS G60", "PL105": "MOLUS X100", "PL107": "MOLUS G100",
    "PL109": "MOLUS G200", "PLG105": "MOLUS G300", "PLG106": "MOLUS G200D",
    "PLB101": "MOLUS B100D", "PLB102": "MOLUS Z1", "PLB103": "MOLUS B200D",
    "PLB104": "MOLUS Z2", "PLB105": "MOLUS B300D", "PLB106": "MOLUS Z3",
    "PLB107": "MOLUS B500D", "PLB108": "MOLUS Z5",
    "PL0102": "MOLUS B100", "PL0104": "MOLUS B200",
    "PL0106": "MOLUS B300", "PL0108": "MOLUS B500",
    "PLX104": "MOLUS X60RGB", "PLX105": "MOLUS X60",
    "PLX110": "MOLUS X100RGB", "PLX113": "MOLUS X200RGB", "PLX114": "MOLUS X200",
    "PLM103": "FIVERAY M20C", "PLM110": "FIVERAY M60 Ultra",
    "PL113": "CINEPEER C100", "PLX108": "CINEPEER CX50", "PLX109": "CINEPEER CX50RGB",
}

# Sorted longest-first so PL0102 wins over PL010 hypothetically, and PLG105 over PL105 etc.
_PREFIXES_LONGEST_FIRST: Final[tuple[str, ...]] = tuple(
    sorted(MODEL_NAMES.keys(), key=len, reverse=True)
)


def resolve_model(advertised_name: str | None) -> tuple[str | None, str | None]:
    """Return `(model_code, friendly_name)` or `(None, None)` for unsupported names."""
    if not advertised_name:
        return None, None
    for prefix in _PREFIXES_LONGEST_FIRST:
        if advertised_name.startswith(prefix):
            return prefix, MODEL_NAMES[prefix]
    return None, None


def is_supported_name(advertised_name: str | None) -> bool:
    code, _ = resolve_model(advertised_name)
    return code is not None


# --- Exceptions --------------------------------------------------------------------

class ProtocolError(ValueError):
    """Raised when a frame cannot be decoded."""


# --- CRC ---------------------------------------------------------------------------

def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM — poly 0x1021, init 0x0000, no reflection, no xor-out."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


# --- Frame encoding ----------------------------------------------------------------

def build_packet(command: Command | int, payload: bytes, sequence: int) -> bytes:
    body = (
        _DIRECTION_REQUEST
        + struct.pack("<H", sequence & 0xFFFF)
        + struct.pack("<H", int(command))
        + payload
    )
    return (
        _MAGIC_HEADER
        + struct.pack("<H", len(body))
        + body
        + struct.pack("<H", crc16_xmodem(body))
    )


def encode_power(on: bool) -> bytes:
    return _SUBCMD_CONTROL + bytes([0x01, 0x01 if on else 0x00])


def encode_brightness(value: float) -> bytes:
    return _SUBCMD_CONTROL + b"\x01" + struct.pack("<f", float(value))


def encode_color_temp(kelvin: int) -> bytes:
    return _SUBCMD_CONTROL + b"\x01" + struct.pack("<H", int(kelvin))


def encode_query() -> bytes:
    return _SUBCMD_CONTROL + b"\x00\x00"


# --- Frame decoding ----------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Response:
    command: int
    sequence: int
    payload: bytes


def parse_frame(data: bytes) -> Response:
    """Decode an incoming notification frame.

    Raises `ProtocolError` if the frame is malformed or CRC fails.
    """
    if len(data) < _HEADER_LEN + 6 + 2:  # header + (dir+seq+cmd) + crc
        raise ProtocolError(f"frame too short: {len(data)} bytes")
    if data[:2] != _MAGIC_HEADER:
        raise ProtocolError("invalid magic header")

    declared_body_len = int.from_bytes(data[2:4], "little")
    expected_total = _HEADER_LEN + declared_body_len + 2
    if len(data) != expected_total:
        raise ProtocolError(
            f"length mismatch: header says {declared_body_len}, got {len(data) - _FRAME_OVERHEAD}"
        )

    body = data[_HEADER_LEN:-2]
    received_crc = int.from_bytes(data[-2:], "little")
    if received_crc != crc16_xmodem(body):
        raise ProtocolError("CRC mismatch")

    sequence = int.from_bytes(body[2:4], "little")
    command = int.from_bytes(body[4:6], "little")
    payload = body[6:]
    return Response(command=command, sequence=sequence, payload=payload)


def decode_brightness_response(payload: bytes) -> float | None:
    if len(payload) < 7:
        return None
    return struct.unpack("<f", payload[3:7])[0]


def decode_color_temp_response(payload: bytes) -> int | None:
    if len(payload) < 5:
        return None
    return int.from_bytes(payload[3:5], "little")


def decode_power_response(payload: bytes) -> bool | None:
    if len(payload) < 3:
        return None
    return payload[2] == 0x01


def decode_firmware_response(payload: bytes) -> str:
    return payload.decode("utf-8", errors="replace").strip("\x00").strip()
