"""A simulated Huawei Hagrid scale for exercising ``ScaleSession`` without hardware.

It implements the *scale side* of the protocol as documented by the openScale
driver, built on the same primitives as the production code. It therefore
proves the session logic is self-consistent and handles framing, encryption and
ordering; it cannot prove the real firmware behaves the same way.

The fake can be told to behave like a Scale 3 Pro: send the 38-byte
dual-frequency packet, use the inverted gender encoding (checked by the tests
via the recorded user profile), answer only on an unfamiliar characteristic, or
answer only through the Bluetooth SIG standard characteristic.
"""
from __future__ import annotations

import asyncio

from custom_components.huawei_scale_pro import protocol as p
from custom_components.huawei_scale_pro.session import Secrets

SCALE_RAND_A = bytes(range(0xA0, 0xB0))

#: A characteristic the integration knows nothing about, used to prove that
#: probe mode discovers and decodes an unfamiliar firmware's channel.
PROBE_UUID = "11112222-3333-4444-5555-666677778888"


class FakeChar:
    def __init__(self, uuid: str, properties: tuple[str, ...] = ("notify",)) -> None:
        self.uuid = uuid
        self.properties = list(properties)


class FakeService:
    def __init__(self, uuids: list[str], extra: tuple[FakeChar, ...] = ()) -> None:
        chars = [FakeChar(u) for u in uuids]
        chars.extend(extra)
        self.characteristics = chars
        self._chars = {c.uuid: c for c in chars}

    def get_characteristic(self, uuid: str) -> FakeChar | None:
        return self._chars.get(uuid)


#: Write-only characteristics (no notification) that the simulated device
#: also needs to expose, so writes to them aren't silently dropped as absent.
_WRITE_ONLY_CHARACTERISTICS: tuple[tuple[str, str], ...] = (
    (p.SVC_CURRENT_TIME, p.CHR_SET_WEIGHT_UNIT),
)


class FakeServices:
    def __init__(self, *, probe: bool = False, standard_only: bool = False) -> None:
        layout: dict[str, list[str]] = {}
        for service, characteristic, _kind in p.NOTIFY_CHARACTERISTICS:
            if standard_only and characteristic == p.CHR_REALTIME_WEIGHT:
                continue
            layout.setdefault(service, []).append(characteristic)
        for service, characteristic in _WRITE_ONLY_CHARACTERISTICS:
            layout.setdefault(service, []).append(characteristic)
        extras: dict[str, tuple[FakeChar, ...]] = {}
        if probe:
            extras[p.SVC_BODY_COMPOSITION] = (
                FakeChar(PROBE_UUID, ("notify", "indicate")),
            )
        self._services = {
            s: FakeService(c, extras.get(s, ())) for s, c in layout.items()
        }

    def get_service(self, uuid: str) -> FakeService | None:
        return self._services.get(uuid)


def realtime_payload(
    weight_kg: float = 86.25,
    fat: float = 30.1,
    impedance_raw: int = 3625,
    heart_rate: int = 61,
    flags: bytes = b"",
    high_impedance_raw: tuple[int, ...] | None = None,
    extra: bytes = b"",
) -> bytes:
    """A realtime payload: 26 bytes, or 38 with the high-frequency block."""
    out = bytearray(38 if high_impedance_raw is not None else 26)
    out[0:2] = round(weight_kg * 100).to_bytes(2, "little")
    out[2:4] = round(fat * 10).to_bytes(2, "little")
    out[4:11] = bytes([0xEA, 0x07, 0x08, 0x09, 0x17, 0x27, 0x1D])  # 2026-08-09 23:39:29
    out[12:14] = impedance_raw.to_bytes(2, "little")
    out[24:26] = heart_rate.to_bytes(2, "little")
    if high_impedance_raw is not None:
        for index, raw in enumerate(high_impedance_raw[:6]):
            out[26 + index * 2 : 28 + index * 2] = raw.to_bytes(2, "little")
    return bytes(out) + flags + extra


def standard_weight_payload(weight_kg: float = 86.25) -> bytes:
    """Bluetooth SIG 0x2A9D Weight Measurement, SI units, no timestamp."""
    return bytes([0x00]) + round(weight_kg / 0.005).to_bytes(2, "little")


class FakeScale:
    """Stands in for a connected ``BleakClient``."""

    def __init__(
        self,
        address: str,
        secrets: Secrets,
        *,
        realtime: bytes | None = None,
        history: tuple[bytes, ...] = (),
        status_ready_after: int = 1,
        reject_work_key: bool = False,
        answer_auth: bool = True,
        work_key_reply_encrypted: bool = True,
        with_gatt: bool = True,
        probe: bool = False,
        standard_only: bool = False,
        deliver_from_probe: bool = False,
        product_id: bytes = b"M00F1",
    ) -> None:
        self.address = address
        self.secrets = secrets
        self.realtime = realtime if realtime is not None else realtime_payload()
        self.history = list(history)
        self.status_ready_after = status_ready_after
        self.reject_work_key = reject_work_key
        self.answer_auth = answer_auth
        self.work_key_reply_encrypted = work_key_reply_encrypted
        self.services = (
            FakeServices(probe=probe, standard_only=standard_only)
            if with_gatt
            else _EmptyServices()
        )
        self.deliver_from_probe = deliver_from_probe
        self.product_id = product_id

        self.work_key: bytes | None = None
        self.user_info: bytes | None = None
        self.clock: bytes | None = None
        self.weight_unit: bytes | None = None
        self.polls = 0
        self.history_requests = 0
        self.authenticated = False
        self.disconnected = False
        self._callbacks: dict[str, object] = {}
        self.peak_subscriptions = 0
        self._rx: dict[str, dict[int, bytes]] = {}
        self._rand_a = SCALE_RAND_A
        self._rand_b: bytes | None = None
        self.disconnect_on_poll: object | None = None  # callable invoked on first poll

    # --- bleak client surface -------------------------------------------------

    async def disconnect(self) -> None:
        self.disconnected = True

    async def start_notify(self, char: FakeChar, callback) -> None:
        self._callbacks[char.uuid] = callback
        self.peak_subscriptions = max(self.peak_subscriptions, len(self._callbacks))

    async def stop_notify(self, char: FakeChar) -> None:
        self._callbacks.pop(char.uuid, None)

    async def write_gatt_char(self, char: FakeChar, data: bytes, response: bool = True) -> None:
        payload = self._receive_frame(char.uuid, bytes(data))
        if payload is not None:
            self._handle(char.uuid, payload)

    # --- plumbing --------------------------------------------------------------

    def _receive_frame(self, uuid: str, frame: bytes) -> bytes | None:
        assert frame[0] in (p.FRAME_WRITE_PLAIN, p.FRAME_WRITE_ENCRYPTED), "bad prefix"
        zeroed = frame[:-2] + b"\x00\x00"
        crc = p.crc16_xmodem(zeroed)
        assert frame[-2:] == bytes([crc & 0xFF, crc >> 8]), "bad write CRC"
        chunk = frame[3:-2]
        assert frame[1] == len(chunk) + 3
        total, index = ((frame[2] >> 4) & 0xF) + 1, frame[2] & 0xF
        parts = self._rx.setdefault(uuid, {})
        parts[index] = chunk
        if len(parts) < total:
            return None
        payload = b"".join(parts[i] for i in range(total))
        self._rx[uuid] = {}
        if frame[0] == p.FRAME_WRITE_ENCRYPTED:
            assert self.work_key is not None or uuid == p.CHR_SEND_WORK_KEY
            key = self._root_key() if uuid == p.CHR_SEND_WORK_KEY else self.work_key
            payload = p.decrypt_payload(payload, key)
        return payload

    def _root_key(self) -> bytes:
        s = self.secrets
        return p.derive_root_key(s.c1, s.c2, p.c3_from_bluetooth_address(self.address))

    def _notify(self, uuid: str, payload: bytes, encrypted: bool = False) -> None:
        if encrypted:
            payload = p.encrypt_payload(payload, self.work_key, bytes(range(16)))
        loop = asyncio.get_running_loop()
        callback = self._callbacks.get(uuid)
        if callback is None:  # not subscribed (any more): the host never hears it
            return
        for wf in p.build_write_frames(payload, encrypted=encrypted):
            frame = bytearray(wf)
            frame[0] = p.FRAME_NOTIFY_ENCRYPTED if encrypted else p.FRAME_NOTIFY_PLAIN
            crc = p.crc16_modbus(bytes(frame[:-2]))
            frame[-2], frame[-1] = crc & 0xFF, crc >> 8
            loop.call_soon(callback, FakeChar(uuid), bytearray(frame))

    # --- the scale's behaviour -------------------------------------------------

    def _handle(self, uuid: str, payload: bytes) -> None:
        s = self.secrets
        if uuid == p.CHR_REQUEST_AUTH:
            # Scale 3 fw 2.0.0.70 appends a status byte to the challenge.
            self._notify(uuid, self._rand_a + b"\x00")
        elif uuid == p.CHR_AUTH_TOKEN:
            rand_b, token = payload[:16], payload[16:]
            expected = p.build_auth_token_payload(self._rand_a, rand_b, s.cak)[16:]
            if token == expected and self.answer_auth:
                self._notify(uuid, p.expected_auth_response(self._rand_a, rand_b, s.cak))
            elif self.answer_auth:
                self._notify(uuid, bytes(32))  # garbage: token did not verify
            # else: stay silent
        elif uuid == p.CHR_SEND_WORK_KEY:
            self.work_key = payload
            status = b"\x01" if self.reject_work_key else b"\x00"
            self.authenticated = not self.reject_work_key
            self._notify(uuid, status, encrypted=self.work_key_reply_encrypted)
        elif uuid == p.CHR_CURRENT_TIME:
            self.clock = payload
            self._notify(uuid, b"\x00")
        elif uuid == p.CHR_SET_WEIGHT_UNIT:
            self.weight_unit = payload
        elif uuid == p.CHR_GET_MANAGER_INFO:
            self._notify(
                uuid, b"HUID-FROM-SCALE".ljust(30, b"\x00") + b"DEVICE-1".ljust(40, b"\x00")
            )
        elif uuid == p.CHR_SET_USER_INFO:
            self.user_info = payload
            self._notify(uuid, b"\x00", encrypted=True)
        elif uuid == p.CHR_REALTIME_WEIGHT:
            if self.status_ready_after == 0 or self.polls >= self.status_ready_after:
                self._notify(uuid, self.realtime, encrypted=True)
        elif uuid == p.CHR_STATUS_POLL:
            self.polls += 1
            if self.disconnect_on_poll and self.polls == 1:
                self.disconnect_on_poll()
                return
            ready = self.polls >= self.status_ready_after
            self._notify(uuid, b"\x00" if ready else b"\x01")
            if ready and self.polls == self.status_ready_after:
                self._deliver_measurement()
        elif uuid == p.CHR_PRODUCT_INFO:
            self._notify(uuid, self.product_id)
        elif uuid == p.CHR_SCALE_VERSION:
            bits = "0010" "0000" "0000" + format(70, "010b") + "00"
            self._notify(uuid, b"\x00" + int(bits, 2).to_bytes(3, "big") + b"SN1")
        elif uuid == p.CHR_HISTORY_WEIGHT:
            self.history_requests += 1
            if self.history:
                self._notify(uuid, self.history.pop(0), encrypted=True)
            else:
                self._notify(uuid, b"", encrypted=True)

    def _deliver_measurement(self) -> None:
        """Send the ready payload on whichever channel this fake was told to use."""
        if self.deliver_from_probe:
            self._notify(PROBE_UUID, self.realtime, encrypted=True)
        elif p.CHR_REALTIME_WEIGHT in self._callbacks:
            self._notify(p.CHR_REALTIME_WEIGHT, self.realtime, encrypted=True)
        elif p.CHR_WEIGHT_MEASUREMENT in self._callbacks:
            self._notify(p.CHR_WEIGHT_MEASUREMENT, standard_weight_payload(86.25))


class _EmptyServices:
    def get_service(self, uuid: str):
        return None
