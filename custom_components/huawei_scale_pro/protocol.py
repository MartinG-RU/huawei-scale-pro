"""Huawei "Hagrid" / WSP wire protocol (Scale 3, Scale 3 Pro, Scale 2 Pro, Honor).

Pure Python, no Home Assistant imports, so it can be unit-tested on its own.

The protocol handling is a port of the openScale project
(``HuaweiHagridWspLib.kt`` / ``HuaweiHagridWspHandler.kt``, GPL-3.0,
https://github.com/oliexdev/openScale), extended for the Scale 3 Pro:

* the product-marker → model table is ported from openScale's
  ``productProfileForMarker()``, so a Scale 3 Pro is recognised as such (its
  advertisement contains ``HUAWEI SCALE 3 PRO``, which also contains the Scale 3
  marker — longest match wins here);
* the per-model user-profile gender encoding is respected (only the plain
  Scale 3 uses ``male = 1``; every other Hagrid family uses ``male = 0``,
  ``female = 1``);
* measurement payloads may be longer than the Scale 3's 26/38 bytes on other
  families, so parsing can run in a length-tolerant mode and keeps the tail of
  the packet as hex for diagnostics;
* the dual-frequency Scale 3 Pro packet carries both a low- and a
  high-frequency impedance block, which are decoded and exposed separately;
* the Bluetooth SIG standard characteristics (``0x2A9D`` Weight Measurement,
  ``0x2A9C`` Body Composition Measurement) are decoded too, as a fallback for
  firmwares that answer on those instead of the vendor characteristics.

The CAK / C1 / C2 secrets are passed in by the caller (see ``const.py`` for the
built-in defaults); nothing in this module extracts them from anywhere.

Framing
-------
Every GATT write/notification carries one WSP frame::

    +--------+--------+-----------+----------------+-----------+
    | prefix | length | total/idx | payload (<=15) | CRC16 LE  |
    +--------+--------+-----------+----------------+-----------+

* prefix: ``DB`` plain write, ``DC`` encrypted write,
  ``BD`` plain notification, ``CD`` encrypted notification
* length: payload length + 3
* total/idx: high nibble = frame count - 1, low nibble = frame index
* CRC: CRC-16/XMODEM over the write frame (CRC bytes zeroed), CRC-16/MODBUS
  over the notification frame (without the CRC bytes)

Encrypted payloads are ``IV(16) || AES-128-CTR(plaintext)``.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# --------------------------------------------------------------------------
# GATT layout
# --------------------------------------------------------------------------


def uuid16(value: int) -> str:
    """Expand a 16-bit Bluetooth SIG UUID to its 128-bit string form."""
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


SVC_USER_DATA = uuid16(0x181C)
SVC_CURRENT_TIME = uuid16(0x1805)
SVC_BODY_COMPOSITION = uuid16(0x181B)
SVC_WEIGHT_SCALE = uuid16(0x181D)

#: The four vendor services the Hagrid family spreads its protocol over. Used
#: both for the "subscribe to everything interesting" probe mode and to decide
#: whether a connected device really speaks this protocol.
HAGRID_SERVICES: tuple[str, ...] = (
    SVC_USER_DATA,
    SVC_CURRENT_TIME,
    SVC_BODY_COMPOSITION,
    SVC_WEIGHT_SCALE,
)

CHR_REQUEST_AUTH = "02b2a08e-f8b0-4047-b1fd-f4e0efeee679"
CHR_AUTH_TOKEN = "32330a04-15d9-421a-91c5-2a2d5c7525c9"
CHR_SEND_WORK_KEY = "a3d330f8-b84f-4f48-a78c-f8d1e33b597a"
CHR_BIND_REQUEST = "42596cbe-d291-4da3-8ca6-d1ae5d1c9174"
CHR_SET_USER_INFO = "8cc61d7d-66c0-4802-89c3-38c5a163592e"
CHR_GET_MANAGER_INFO = "4338c65e-ed8e-4085-bbea-a25e33ca6b54"
CHR_CURRENT_TIME = uuid16(0x2A2B)
CHR_PRODUCT_INFO = "75143e79-f878-4a00-a628-edc40509de7e"
CHR_SCALE_VERSION = "1f5d3d5c-496d-4290-af03-c7a8d5419741"
CHR_GET_WEIGHT_UNIT = "7e6dbc73-42e7-45b9-a6ec-6aa2d7834695"
CHR_SET_WEIGHT_UNIT = "11872f15-a91d-49da-ac89-5107284f3425"
CHR_STATUS_POLL = "bfc36f6e-4150-4a4b-9052-3d359e52962e"
CHR_STATUS_RESULT = "ba216311-1787-472b-bef6-3eb29e62293e"
CHR_WEIGHT_MEASUREMENT = uuid16(0x2A9D)
CHR_BODY_COMPOSITION_MEASUREMENT = uuid16(0x2A9C)
CHR_REALTIME_WEIGHT = "46797c17-d639-488d-9476-4789e8472878"
CHR_HISTORY_WEIGHT = "0212f42a-5f19-4bc1-ba52-d7ec7ccb71a4"

#: Standard-looking User Data Service characteristics (service 181c) that the
#: app also writes individually, alongside the encrypted SET_USER_INFO blob we
#: send. Declared for completeness; not written. The app's own payload encoding
#: for these three isn't known (only the UUIDs are), while SET_USER_INFO's
#: 69-byte layout is the format that has been validated against a real capture,
#: so it already covers the same profile data.
CHR_SET_AGE = uuid16(0x2A80)
CHR_SET_GENDER = uuid16(0x2A8C)
CHR_SET_HEIGHT = uuid16(0x2A8E)

#: (service, characteristic, kind) for everything we subscribe to. ``kind`` is
#: how a payload received on that characteristic is routed by the session.
NOTIFY_CHARACTERISTICS: tuple[tuple[str, str, str], ...] = (
    (SVC_USER_DATA, CHR_REQUEST_AUTH, "request_auth"),
    (SVC_USER_DATA, CHR_AUTH_TOKEN, "auth_token"),
    (SVC_USER_DATA, CHR_SEND_WORK_KEY, "send_work_key"),
    (SVC_USER_DATA, CHR_BIND_REQUEST, "realtime"),
    (SVC_USER_DATA, CHR_SET_USER_INFO, "set_user_info"),
    (SVC_USER_DATA, CHR_GET_MANAGER_INFO, "manager_info"),
    (SVC_CURRENT_TIME, CHR_CURRENT_TIME, "time"),
    (SVC_CURRENT_TIME, CHR_PRODUCT_INFO, "product_info"),
    (SVC_CURRENT_TIME, CHR_SCALE_VERSION, "scale_version"),
    (SVC_CURRENT_TIME, CHR_GET_WEIGHT_UNIT, "weight_unit"),
    (SVC_CURRENT_TIME, CHR_STATUS_POLL, "status"),
    (SVC_CURRENT_TIME, CHR_STATUS_RESULT, "status"),
    (SVC_BODY_COMPOSITION, CHR_BODY_COMPOSITION_MEASUREMENT, "standard_body"),
    (SVC_WEIGHT_SCALE, CHR_WEIGHT_MEASUREMENT, "standard_weight"),
    (SVC_BODY_COMPOSITION, CHR_REALTIME_WEIGHT, "realtime"),
    (SVC_BODY_COMPOSITION, CHR_HISTORY_WEIGHT, "history"),
)

#: Characteristics that carry a *complete* measurement (as opposed to live
#: progress). The Standard ones use the Bluetooth SIG payload layout.
STANDARD_KINDS = frozenset({"standard_weight", "standard_body"})

# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

FRAME_WRITE_PLAIN = 0xDB
FRAME_WRITE_ENCRYPTED = 0xDC
FRAME_NOTIFY_PLAIN = 0xBD
FRAME_NOTIFY_ENCRYPTED = 0xCD

#: The app's own ceiling for a plain chunk before BLE-frame splitting is 238
#: bytes (222 for an encrypted one, which needs room for the 16-byte IV); we
#: never send more than 69 bytes (SET_USER_INFO) so this is never reached.
MAX_SIMPLE_PAYLOAD_BYTES = 238
SIMPLE_CHUNK_BYTES = 15
USER_INFO_PAYLOAD_BYTES = 69
MANAGER_HUID_BYTES = 30
MANAGER_DEVICE_ID_BYTES = 40

AUTH_TOKEN_SUFFIX_TO_SCALE = b"1123"
AUTH_TOKEN_SUFFIX_FROM_SCALE = b"9856"


def _build_crc16_xmodem_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return tuple(table)


_CRC16_TABLE = _build_crc16_xmodem_table()


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM, used for frames written to the scale."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _CRC16_TABLE[(byte ^ (crc >> 8)) & 0xFF]) & 0xFFFF
    return crc


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS, used for frames notified by the scale."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_write_frames(payload: bytes, encrypted: bool = False) -> list[bytes]:
    """Split ``payload`` into WSP write frames (15 payload bytes per frame)."""
    if len(payload) > MAX_SIMPLE_PAYLOAD_BYTES:
        raise ValueError(
            f"WSP payloads > {MAX_SIMPLE_PAYLOAD_BYTES} bytes are not supported"
        )
    total = max(1, -(-len(payload) // SIMPLE_CHUNK_BYTES))
    frames: list[bytes] = []
    offset = 0
    for index in range(total):
        chunk = payload[offset : offset + SIMPLE_CHUNK_BYTES]
        offset += len(chunk)
        frame = bytearray(len(chunk) + 5)
        frame[0] = FRAME_WRITE_ENCRYPTED if encrypted else FRAME_WRITE_PLAIN
        frame[1] = len(chunk) + 3
        frame[2] = (((total - 1) & 0x0F) << 4) | (index & 0x0F)
        frame[3 : 3 + len(chunk)] = chunk
        crc = crc16_xmodem(bytes(frame))  # CRC bytes are still zero here
        frame[-2] = crc & 0xFF
        frame[-1] = (crc >> 8) & 0xFF
        frames.append(bytes(frame))
    return frames


@dataclass(frozen=True)
class WspFrame:
    encrypted: bool
    total_frames: int
    frame_index: int
    payload: bytes
    crc_valid: bool


def has_notification_prefix(frame: bytes) -> bool:
    return bool(frame) and frame[0] in (FRAME_NOTIFY_PLAIN, FRAME_NOTIFY_ENCRYPTED)


def has_valid_notification_crc(frame: bytes) -> bool:
    if len(frame) < 5:
        return False
    expected = frame[-2] | (frame[-1] << 8)
    return crc16_modbus(frame[:-2]) == expected


def parse_notification_frame(
    frame: bytes, require_valid_crc: bool = True
) -> WspFrame | None:
    if len(frame) < 5 or not has_notification_prefix(frame):
        return None
    crc_valid = has_valid_notification_crc(frame)
    if require_valid_crc and not crc_valid:
        return None
    payload_len = frame[1] - 3
    if payload_len < 0 or len(frame) < payload_len + 5:
        return None
    total = ((frame[2] >> 4) & 0x0F) + 1
    index = frame[2] & 0x0F
    if index >= total:
        return None
    return WspFrame(
        encrypted=frame[0] == FRAME_NOTIFY_ENCRYPTED,
        total_frames=total,
        frame_index=index,
        payload=bytes(frame[3 : 3 + payload_len]),
        crc_valid=crc_valid,
    )


class FrameAccumulator:
    """Reassemble multi-frame notifications for a single characteristic."""

    def __init__(self, require_valid_crc: bool = True) -> None:
        self._require_valid_crc = require_valid_crc
        self._expected = 0
        self._encrypted = False
        self._chunks: dict[int, bytes] = {}
        self.last_completed_encrypted = False

    def _reset(self) -> None:
        self._expected = 0
        self._encrypted = False
        self._chunks.clear()

    def reset(self) -> None:
        self._reset()

    def ingest(self, raw_frame: bytes) -> bytes | None:
        """Feed one notification; return the full payload once complete."""
        frame = parse_notification_frame(raw_frame, self._require_valid_crc)
        if frame is None:
            return None
        if frame.total_frames <= 1:
            self.last_completed_encrypted = frame.encrypted
            self._reset()
            return frame.payload

        if frame.frame_index == 0:  # always starts a new logical packet
            self._reset()
            self._expected = frame.total_frames
            self._encrypted = frame.encrypted
        else:
            if self._expected == 0:  # orphan continuation frame
                return None
            if self._expected != frame.total_frames or self._encrypted != frame.encrypted:
                self._reset()
                return None

        self._chunks[frame.frame_index] = frame.payload
        if len(self._chunks) < self._expected:
            return None

        out = b"".join(self._chunks.get(i, b"") for i in range(self._expected))
        if any(i not in self._chunks for i in range(self._expected)):
            self._reset()
            return None
        self.last_completed_encrypted = self._encrypted
        self._reset()
        return out


# --------------------------------------------------------------------------
# Authentication and encryption
# --------------------------------------------------------------------------


def _hmac_sha256(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def _auth_token(rand_a: bytes, rand_b: bytes, cak: bytes, suffix: bytes) -> bytes:
    rand = rand_a + rand_b
    inner_key = _hmac_sha256(cak + suffix, rand)
    return _hmac_sha256(inner_key, rand)


def build_auth_token_payload(rand_a: bytes, rand_b: bytes, cak: bytes) -> bytes:
    """Payload for the auth-token characteristic: ``randB || HMAC token``."""
    _require_len("randA", rand_a, 16)
    _require_len("randB", rand_b, 16)
    _require_len("CAK", cak, 16)
    return rand_b + _auth_token(rand_a, rand_b, cak, AUTH_TOKEN_SUFFIX_TO_SCALE)


def expected_auth_response(rand_a: bytes, rand_b: bytes, cak: bytes) -> bytes:
    return _auth_token(rand_a, rand_b, cak, AUTH_TOKEN_SUFFIX_FROM_SCALE)


def is_valid_auth_response(
    response: bytes, rand_a: bytes, rand_b: bytes, cak: bytes
) -> bool:
    expected = expected_auth_response(rand_a, rand_b, cak)
    # Some firmwares append a status byte, so only compare the prefix.
    return len(response) >= len(expected) and hmac.compare_digest(
        expected, bytes(response[: len(expected)])
    )


def c3_from_bluetooth_address(address: str) -> bytes:
    """C3 is the ASCII of the upper-case MAC (no separators) plus ``0000``."""
    compact = address.replace(":", "").replace("-", "").upper()
    if len(compact) != 12 or any(c not in "0123456789ABCDEF" for c in compact):
        raise ValueError("Bluetooth address must contain 12 hex digits")
    return (compact + "0000").encode("ascii")


def _sha256_first16(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()[:16]


def derive_root_key(c1: bytes, c2: bytes, c3: bytes) -> bytes:
    if not (len(c1) == len(c2) == len(c3)) or not c1:
        raise ValueError("C1, C2 and C3 must be non-empty and the same length")
    mixed = bytes(((c1[i] << 4) ^ c2[i]) & 0xFF for i in range(len(c1)))
    key_data = _sha256_first16(mixed)
    folded = bytes(((key_data[i] >> 6) ^ c3[i]) & 0xFF for i in range(len(key_data)))
    return _sha256_first16(folded)


def _aes_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    _require_len("AES key", key, 16)
    _require_len("AES IV", iv, 16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def encrypt_payload(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """Return ``IV || ciphertext``."""
    return iv + _aes_ctr(plaintext, key, iv)


def decrypt_payload(iv_and_ciphertext: bytes, key: bytes) -> bytes:
    if len(iv_and_ciphertext) < 16:
        raise ValueError("Encrypted payload must include a 16-byte IV")
    return _aes_ctr(iv_and_ciphertext[16:], key, iv_and_ciphertext[:16])


def build_encrypted_write_frames(plaintext: bytes, key: bytes, iv: bytes) -> list[bytes]:
    return build_write_frames(encrypt_payload(plaintext, key, iv), encrypted=True)


def _require_len(name: str, value: bytes, length: int) -> None:
    if len(value) != length:
        raise ValueError(f"{name} must be {length} bytes, got {len(value)}")


# --------------------------------------------------------------------------
# Outgoing payloads
# --------------------------------------------------------------------------


def _put_fixed(target: bytearray, text: str, offset: int, length: int) -> None:
    raw = text.encode("utf-8")[:length]
    target[offset : offset + len(raw)] = raw


def build_user_info_payload(
    huid: str,
    uid: str,
    gender: int,
    age_years: int,
    height_cm: int,
    weight_kg: float,
    user_type: int = 0,
) -> bytes:
    """69-byte user profile sent (encrypted) before a measurement."""
    payload = bytearray(USER_INFO_PAYLOAD_BYTES)
    _put_fixed(payload, huid, 0, 30)
    _put_fixed(payload, uid, 30, 32)
    payload[62] = max(0, min(255, gender))
    payload[63] = max(0, min(255, age_years))
    payload[64:66] = max(0, min(0xFFFF, height_cm)).to_bytes(2, "little")
    centi_kg = int(round(weight_kg * 100)) if weight_kg and weight_kg > 0 else 0
    payload[66:68] = max(0, min(0xFFFF, centi_kg)).to_bytes(2, "little")
    payload[68] = max(0, min(255, user_type))
    return bytes(payload)


def weight_unit_payload(metric: bool) -> bytes:
    """Payload for SET_WEIGHT_UNIT: 1=kg, 2=lb (mirrors ``parse_weight_unit``)."""
    return bytes([1 if metric else 2])


def current_time_payload(now: datetime) -> bytes:
    """8-byte current-time value: year(LE16) month day hour min sec weekday."""
    return bytes(
        [
            now.year & 0xFF,
            (now.year >> 8) & 0xFF,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.isoweekday(),
        ]
    )


# --------------------------------------------------------------------------
# Impedance
# --------------------------------------------------------------------------

#: A whole-body BIA impedance is roughly 200–900 ohm. The Scale 3 sends it in
#: 0.1-ohm units (a real 350 ohm arrives as 3500); openScale's generic
#: heuristic ("below 4000 is already ohm") misreads that, which is why its Scale
#: 3 branch divides explicitly. Other families are unmeasured, so the default is
#: a heuristic that also copes with a firmware that reports plain ohm:
#: < 1000 → ohm, 1000..39999 → 0.1-ohm units, anything else is implausible.
IMPEDANCE_MODE_AUTO = "auto"
IMPEDANCE_MODE_TENTHS = "tenths"
IMPEDANCE_MODE_OHMS = "ohms"


def impedance_to_ohm(raw: int, mode: str = IMPEDANCE_MODE_AUTO) -> float | None:
    """Convert one raw impedance word to ohm, or ``None`` if implausible."""
    if raw is None or raw <= 0:
        return None
    if mode == IMPEDANCE_MODE_TENTHS:
        return raw / 10.0
    if mode == IMPEDANCE_MODE_OHMS:
        return float(raw)
    if raw < 1000:
        return float(raw)
    if raw <= 39999:
        return raw / 10.0
    return None


# --------------------------------------------------------------------------
# Incoming payloads — vendor (Hagrid) measurement
# --------------------------------------------------------------------------


def _u16(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _read_timestamp(data: bytes, offset: int) -> datetime | None:
    if offset + 6 >= len(data):
        return None
    year = _u16(data, offset)
    month, day, hour, minute, second = data[offset + 2 : offset + 7]
    try:
        if year < 2000:
            return None
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


#: Lengths the Scale 3 is known to send: 26 bytes without, 38 bytes with the
#: high-frequency impedance block. The Scale 3 Pro advertises dual-frequency
#: measurement, so its packet is expected to be the 38-byte variant — but it is
#: not guaranteed, hence the tolerant mode below.
REALTIME_LENGTHS = frozenset({26, 38})
HISTORY_LENGTHS = frozenset({27, 28, 39, 40})
#: Tolerant mode: any length from the smallest known packet up to a sane cap is
#: accepted, so an unknown firmware variant still yields weight/fat/impedance
#: instead of nothing (the tail is kept as hex for diagnostics).
MIN_MEASUREMENT_BYTES = 26
MAX_MEASUREMENT_BYTES = 128


@dataclass(frozen=True)
class WeightMeasurement:
    raw_length: int
    weight_kg: float
    fat_percent: float
    #: Scale-local wall clock time; ``None`` if the bytes did not decode.
    timestamp: datetime | None
    #: Raw impedance words as sent (unit conversion happens in the accessors).
    low_impedance: tuple[int, ...]
    high_impedance: tuple[int, ...]
    heart_rate: int | None
    history_suspected_flag: int | None = None
    history_complete_flag: int | None = None
    #: Bytes past the known layout (Scale 3 Pro: extra segmental data?), hex.
    trailing_hex: str | None = None
    #: The complete payload as received, for capture/debugging.
    raw_payload: bytes = b""
    #: Set when the packet came from a Bluetooth SIG standard characteristic.
    source: str = "hagrid"

    def _first_ohm(self, values: tuple[int, ...], mode: str) -> float | None:
        for raw in values:
            ohm = impedance_to_ohm(raw, mode)
            if ohm is not None:
                return ohm
        return None

    def low_ohm_all(self, mode: str = IMPEDANCE_MODE_AUTO) -> tuple[float | None, ...]:
        return tuple(impedance_to_ohm(raw, mode) for raw in self.low_impedance)

    def high_ohm_all(self, mode: str = IMPEDANCE_MODE_AUTO) -> tuple[float | None, ...]:
        return tuple(impedance_to_ohm(raw, mode) for raw in self.high_impedance)

    def impedance_ohm(self, mode: str = IMPEDANCE_MODE_AUTO) -> float | None:
        """Representative low-frequency (whole-body) impedance in ohm."""
        return self._first_ohm(self.low_impedance, mode)

    def impedance_high_ohm(self, mode: str = IMPEDANCE_MODE_AUTO) -> float | None:
        """Representative high-frequency impedance in ohm (dual-frequency only)."""
        return self._first_ohm(self.high_impedance, mode)

    @property
    def has_high_frequency(self) -> bool:
        return bool(self.high_impedance)

    @property
    def is_history_complete(self) -> bool | None:
        if self.history_complete_flag is None:
            return None
        return self.history_complete_flag == 0

    @property
    def dedupe_key(self) -> str:
        stamp = self.timestamp.isoformat() if self.timestamp else ""
        return "|".join(
            (
                stamp,
                str(round(self.weight_kg * 100)),
                str(round(self.fat_percent * 10)),
                str(self.heart_rate or ""),
                ",".join(map(str, self.low_impedance)),
                ",".join(map(str, self.high_impedance)),
            )
        )


def _parse_weight_measurement(
    payload: bytes,
    allowed_lengths: frozenset[int],
    timestamp_required: bool,
    *,
    tolerant: bool = False,
) -> WeightMeasurement | None:
    if tolerant:
        if not MIN_MEASUREMENT_BYTES <= len(payload) <= MAX_MEASUREMENT_BYTES:
            return None
    elif len(payload) not in allowed_lengths:
        return None

    high_present = len(payload) >= 38
    base = 38 if high_present else 26

    timestamp = _read_timestamp(payload, 4)
    if timestamp is None and timestamp_required:
        return None

    low = tuple(_u16(payload, 12 + i * 2) for i in range(6))
    high = tuple(_u16(payload, 26 + i * 2) for i in range(6)) if high_present else ()
    hr_raw = _u16(payload, 24)
    trailing = payload[base:].hex().upper() if len(payload) > base else None
    return WeightMeasurement(
        raw_length=len(payload),
        weight_kg=_u16(payload, 0) / 100.0,
        fat_percent=_u16(payload, 2) / 10.0,
        timestamp=timestamp,
        low_impedance=low,
        high_impedance=high,
        heart_rate=hr_raw if 1 <= hr_raw <= 240 else None,
        history_suspected_flag=payload[base] if len(payload) > base else None,
        history_complete_flag=payload[base + 1] if len(payload) > base + 1 else None,
        trailing_hex=trailing,
        raw_payload=bytes(payload),
    )


def parse_realtime_measurement(
    payload: bytes, *, tolerant: bool = False
) -> WeightMeasurement | None:
    """Live measurement. A bad timestamp does not reject the packet."""
    return _parse_weight_measurement(
        payload, REALTIME_LENGTHS, False, tolerant=tolerant
    )


def parse_history_measurement(
    payload: bytes, *, tolerant: bool = False
) -> WeightMeasurement | None:
    """Stored record. A valid timestamp is mandatory."""
    return _parse_weight_measurement(
        payload, HISTORY_LENGTHS, True, tolerant=tolerant
    )


def _decode_text(raw: bytes) -> str:
    nul = raw.find(0)
    if nul >= 0:
        raw = raw[:nul]
    raw = raw.rstrip(b"\x00 ")
    if not raw:
        return ""
    if all(0x20 <= b <= 0x7E for b in raw):
        return raw.decode("ascii")
    return "hex:" + raw.hex().upper()


@dataclass(frozen=True)
class ManagerInfo:
    huid: str
    device_id: str
    account_info: str | None


def parse_manager_info(payload: bytes) -> ManagerInfo | None:
    if not payload:
        return ManagerInfo("", "", None)
    if len(payload) <= MANAGER_HUID_BYTES:
        return None
    huid = _decode_text(payload[:MANAGER_HUID_BYTES])
    id_end_base = MANAGER_HUID_BYTES + MANAGER_DEVICE_ID_BYTES
    extended = len(payload) > id_end_base
    device_id = _decode_text(
        payload[MANAGER_HUID_BYTES : id_end_base if extended else len(payload)]
    )
    account: str | None = None
    if extended:
        declared = payload[id_end_base]
        end = min(len(payload), id_end_base + 1 + declared)
        if end > id_end_base + 1:
            account = _decode_text(payload[id_end_base + 1 : end]) or None
    return ManagerInfo(huid, device_id, account)


@dataclass(frozen=True)
class ScaleVersion:
    version: str
    serial: str


def parse_scale_version(payload: bytes) -> ScaleVersion | None:
    if len(payload) < 5 or len(payload) > 20:
        return None
    bits = "".join(f"{b:08b}" for b in payload[1:4])
    major = int(bits[0:4], 2)
    minor = int(bits[4:8], 2)
    patch = int(bits[8:12], 2)
    build = int(bits[12:22], 2)
    return ScaleVersion(f"{major}.{minor}.{patch}.{build}", _decode_text(payload[4:]))


@dataclass(frozen=True)
class WeightUnit:
    raw: int
    label: str  # "kg", "lb", "g", or "unknown"


def parse_weight_unit(payload: bytes) -> WeightUnit | None:
    if not payload:
        return None
    raw = payload[0]
    return WeightUnit(raw=raw, label={1: "kg", 2: "lb", 3: "g"}.get(raw, "unknown"))


def parse_product_id(payload: bytes) -> str | None:
    """First four ASCII characters of the product-info payload (e.g. ``M00F``)."""
    text = _decode_text(payload)
    if not text or text.startswith("hex:"):
        return None
    return text[:4]


# --------------------------------------------------------------------------
# Incoming payloads — Bluetooth SIG standard characteristics
# --------------------------------------------------------------------------

_KG_PER_UNIT = 0.005  # 0x2A9D / 0x2A9C weight resolution in SI units
_LB_PER_UNIT = 0.01
LB_TO_KG = 0.45359237


def parse_standard_weight_measurement(payload: bytes) -> WeightMeasurement | None:
    """Decode a SIG ``0x2A9D`` Weight Measurement (weight only).

    Flags (byte 0): bit0 imperial units, bit1 timestamp present, bit2 user id
    present, bit3 BMI and height present. Used as a fallback for firmwares that
    answer on the standard characteristic instead of the vendor one.
    """
    if len(payload) < 3:
        return None
    flags = payload[0]
    imperial = bool(flags & 0x01)
    raw = _u16(payload, 1)
    weight_kg = raw * (_LB_PER_UNIT * LB_TO_KG if imperial else _KG_PER_UNIT)
    offset = 3
    timestamp = None
    if flags & 0x02:
        timestamp = _read_timestamp(payload, offset)
        offset += 7
    if flags & 0x04:
        offset += 1  # user id
    if flags & 0x08:
        offset += 2  # BMI
        offset += 2  # height
    if weight_kg <= 0:
        return None
    return WeightMeasurement(
        raw_length=len(payload),
        weight_kg=weight_kg,
        fat_percent=0.0,
        timestamp=timestamp,
        low_impedance=(),
        high_impedance=(),
        heart_rate=None,
        raw_payload=bytes(payload),
        source="standard_weight",
    )


def parse_standard_body_composition_measurement(
    payload: bytes,
) -> WeightMeasurement | None:
    """Decode a SIG ``0x2A9C`` Body Composition Measurement.

    Only the fields this integration can use are returned (body fat, weight,
    impedance, timestamp). Everything else is left in the payload for the
    capture log.
    """
    if len(payload) < 4:
        return None
    flags = _u16(payload, 0)
    imperial = bool(flags & 0x0001)
    offset = 2
    fat_raw = _u16(payload, offset)
    offset += 2
    timestamp = None
    if flags & 0x0002:
        timestamp = _read_timestamp(payload, offset)
        offset += 7
    if flags & 0x0004:
        offset += 1  # user id
    # Optional fields in the order defined by the spec.
    for bit, size in ((3, 2), (4, 2), (5, 2), (6, 2), (7, 2), (8, 2)):
        if flags & (1 << bit):
            offset += size
    impedance = None
    if flags & (1 << 9):
        if offset + 2 <= len(payload):
            impedance = _u16(payload, offset)
        offset += 2
    weight_kg = 0.0
    if flags & (1 << 10) and offset + 2 <= len(payload):
        raw = _u16(payload, offset)
        weight_kg = raw * (_LB_PER_UNIT * LB_TO_KG if imperial else _KG_PER_UNIT)
        offset += 2
    if weight_kg <= 0 and impedance is None and fat_raw == 0:
        return None
    return WeightMeasurement(
        raw_length=len(payload),
        weight_kg=weight_kg,
        fat_percent=fat_raw / 10.0,
        timestamp=timestamp,
        low_impedance=(impedance,) if impedance else (),
        high_impedance=(),
        heart_rate=None,
        raw_payload=bytes(payload),
        source="standard_body",
    )


def parse_any_measurement(
    kind: str, payload: bytes, *, tolerant: bool = False
) -> WeightMeasurement | None:
    """Route a payload by the characteristic it arrived on."""
    if kind == "standard_weight":
        return parse_standard_weight_measurement(payload)
    if kind == "standard_body":
        return parse_standard_body_composition_measurement(payload)
    return parse_realtime_measurement(payload, tolerant=tolerant)


# --------------------------------------------------------------------------
# Device identification
# --------------------------------------------------------------------------


class Family:
    """Hagrid product families, as in openScale's ``HagridProductFamily``."""

    SCALE_2_PRO = "scale2_pro"
    SCALE_3_PRO = "scale3_pro"
    SCALE_3 = "scale3"
    HONOR_SCALE_2 = "honor_scale2"
    DOBBY = "dobby"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ProductProfile:
    """What a matched advertisement tells us about the scale."""

    family: str
    display_name: str
    smart_product_id: str | None
    #: ``True`` for dual-frequency families (Scale 3 Pro), ``False`` for the
    #: single-frequency ones, ``None`` when unknown.
    expected_high_frequency_impedance: bool | None
    #: Value of byte 62 in the encrypted user profile, per openScale: the plain
    #: Scale 3 uses male=1/female=0, every other family the other way round.
    gender_male: int
    gender_female: int

    @property
    def is_scale3(self) -> bool:
        return self.family == Family.SCALE_3

    @property
    def is_scale3_pro(self) -> bool:
        return self.family == Family.SCALE_3_PRO

    @property
    def has_handle(self) -> bool:
        """8-electrode device (the Scale 3 Pro has a handle with electrodes)."""
        return self.is_scale3_pro

    @property
    def needs_tolerant_parsing(self) -> bool:
        """Families other than the plain Scale 3 may send unfamiliar lengths."""
        return self.family != Family.SCALE_3


#: (marker, family) — order does not matter, the longest match wins, so that
#: ``HUAWEI SCALE 3 PRO`` is not mistaken for ``HUAWEI SCALE 3``.
PRODUCT_MARKERS: tuple[tuple[str, str], ...] = (
    ("HUAWEI SCALE 2 PRO", Family.SCALE_2_PRO),
    ("HAG-B19", Family.SCALE_2_PRO),
    ("HAGRID-B19", Family.SCALE_2_PRO),
    ("007B", Family.SCALE_2_PRO),
    ("HUAWEI SCALE 3 PRO", Family.SCALE_3_PRO),
    ("HAGRID-B29", Family.SCALE_3_PRO),
    ("HAGRID2021-B19", Family.SCALE_3_PRO),
    ("M00F", Family.SCALE_3_PRO),
    ("HUAWEI SCALE 3", Family.SCALE_3),
    ("HEM-B19", Family.SCALE_3),
    ("HERM-B19", Family.SCALE_3),
    ("M00D", Family.SCALE_3),
    ("HONOR SCALE 2", Family.HONOR_SCALE_2),
    ("LUP-B19", Family.HONOR_SCALE_2),
    ("LUPIN-B19HN", Family.HONOR_SCALE_2),
    ("N001", Family.HONOR_SCALE_2),
    ("DOBBY-B19", Family.DOBBY),
    ("M0CJ", Family.DOBBY),
)

_PROFILES: dict[str, ProductProfile] = {
    Family.SCALE_3_PRO: ProductProfile(
        family=Family.SCALE_3_PRO,
        display_name="HUAWEI Scale 3 Pro",
        smart_product_id="M00F",
        expected_high_frequency_impedance=True,
        gender_male=0,
        gender_female=1,
    ),
    Family.SCALE_3: ProductProfile(
        family=Family.SCALE_3,
        display_name="HUAWEI Scale 3",
        smart_product_id="M00D",
        expected_high_frequency_impedance=False,
        gender_male=1,
        gender_female=0,
    ),
    Family.SCALE_2_PRO: ProductProfile(
        family=Family.SCALE_2_PRO,
        display_name="HUAWEI Scale 2 Pro",
        smart_product_id="007B",
        expected_high_frequency_impedance=False,
        gender_male=0,
        gender_female=1,
    ),
    Family.HONOR_SCALE_2: ProductProfile(
        family=Family.HONOR_SCALE_2,
        display_name="HONOR Scale 2",
        smart_product_id="N001",
        expected_high_frequency_impedance=False,
        gender_male=0,
        gender_female=1,
    ),
    Family.DOBBY: ProductProfile(
        family=Family.DOBBY,
        display_name="Huawei/Honor WSP Scale",
        smart_product_id="M0CJ",
        expected_high_frequency_impedance=None,
        gender_male=0,
        gender_female=1,
    ),
    Family.UNKNOWN: ProductProfile(
        family=Family.UNKNOWN,
        display_name="Huawei/Honor WSP Scale",
        smart_product_id=None,
        expected_high_frequency_impedance=None,
        gender_male=0,
        gender_female=1,
    ),
}

#: Family keys that can be forced in the options flow (``generic`` → UNKNOWN).
MODEL_FAMILY_ALIASES: dict[str, str] = {
    "scale3_pro": Family.SCALE_3_PRO,
    "scale3": Family.SCALE_3,
    "scale2_pro": Family.SCALE_2_PRO,
    "honor_scale2": Family.HONOR_SCALE_2,
    "generic": Family.UNKNOWN,
}


def profile_for_family(family: str) -> ProductProfile:
    return _PROFILES.get(family, _PROFILES[Family.UNKNOWN])


def product_profile_for_marker(marker: str | None) -> ProductProfile:
    """Map a product marker (name fragment / product id) to a profile."""
    text = (marker or "").strip().upper()
    if not text:
        return profile_for_family(Family.UNKNOWN)
    best: tuple[int, str] | None = None
    for needle, family in PRODUCT_MARKERS:
        if needle in text and (best is None or len(needle) > best[0]):
            best = (len(needle), family)
    return profile_for_family(best[1] if best else Family.UNKNOWN)


def _ascii_view(raw: bytes) -> str:
    return "".join(chr(b) if 0x20 <= b <= 0x7E else " " for b in raw)


def advertisement_texts(
    name: str | None,
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
) -> list[str]:
    texts = [name or ""]
    texts += [_ascii_view(v) for v in (manufacturer_data or {}).values()]
    texts += [_ascii_view(v) for v in (service_data or {}).values()]
    return texts


def identify_hagrid(
    name: str | None,
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
) -> ProductProfile | None:
    """Return the product profile if the advertisement is a Hagrid scale.

    Mirrors the openScale driver: look for a product marker in the local name
    or as ASCII inside the manufacturer / service data; the longest matching
    marker wins. A Scale 3 Pro is therefore reported as SCALE_3_PRO even though
    its name also contains the Scale 3 marker.
    """
    best: tuple[int, str] | None = None
    for text in advertisement_texts(name, manufacturer_data, service_data):
        upper = text.upper()
        for needle, family in PRODUCT_MARKERS:
            if needle in upper and (best is None or len(needle) > best[0]):
                best = (len(needle), family)
    if best is None:
        return None
    return profile_for_family(best[1])


def identify_scale_3(
    name: str | None,
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
) -> bool:
    """True only for the plain Scale 3 (kept for API compatibility)."""
    profile = identify_hagrid(name, manufacturer_data, service_data)
    return profile is not None and profile.is_scale3


# --------------------------------------------------------------------------
# Advertised capability bits
# --------------------------------------------------------------------------

#: openScale: bit 18 of the advertised capability bits means "high-frequency
#: (dual-frequency) impedance", i.e. a Scale 3 Pro class device.
CAPABILITY_BIT_HIGH_FREQUENCY_IMPEDANCE = 18

_CAPABILITY_RE = re.compile(
    r"(?:capacity|capability|caps?)[:=]([0-9a-f]+)", re.IGNORECASE
)


def parse_capability_bits(hex_text: str | None) -> set[int]:
    """Decode the ``capacity=…`` hex blob from an advertisement into bit numbers."""
    cleaned = "".join(
        c for c in (hex_text or "") if c.isdigit() or c.lower() in "abcdef"
    ).upper()
    if not cleaned or len(cleaned) % 2:
        return set()
    bits: set[int] = set()
    for byte_index in range(len(cleaned) // 2):
        value = int(cleaned[byte_index * 2 : byte_index * 2 + 2], 16)
        for bit in range(8):
            if value & (1 << bit):
                bits.add(byte_index * 8 + bit)
    return bits


def find_capability_bits(
    name: str | None,
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
) -> set[int]:
    """Search an advertisement for the capability blob and decode it."""
    for text in advertisement_texts(name, manufacturer_data, service_data):
        match = _CAPABILITY_RE.search(text)
        if match:
            return parse_capability_bits(match.group(1))
    return set()
