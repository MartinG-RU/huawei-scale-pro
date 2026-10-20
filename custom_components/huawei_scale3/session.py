"""One connected session with a Huawei Scale 3.

Drives the openScale-documented flow over an already-connected bleak client:

1. subscribe to every notify characteristic
2. request-auth -> challenge(randA); send ``randB || token``; verify the scale's token
3. derive the root key from C1/C2 and the MAC, send a random work key encrypted with it
4. sync the clock, read the manager info (HUID), send the encrypted user profile
5. request the realtime weight and poll measurement status until the scale
   delivers a complete measurement
6. optionally drain the stored history

Pure asyncio; depends on the client only through ``services``,
``start_notify`` and ``write_gatt_char`` so it can be exercised with a fake.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

SUBSCRIBE_ATTEMPTS = 3
SUBSCRIBE_RETRY_DELAY = 0.3
AUTH_TIMEOUT = 8.0
MANAGER_INFO_TIMEOUT = 2.5
USER_INFO_ACK_TIMEOUT = 3.0
STATUS_POLL_INTERVAL = 3.0
MAX_STATUS_POLLS = 30
FINAL_REQUEST_GRACE = 6.0
HISTORY_RECORD_TIMEOUT = 6.0
MAX_HISTORY_RECORDS = 64
#: Upper bound for the weighing itself (the scale needs ~20-25 s).
MEASUREMENT_TIMEOUT = MAX_STATUS_POLLS * STATUS_POLL_INTERVAL + FINAL_REQUEST_GRACE

SYNTHETIC_HUID_PREFIX = "openscale:"  # same format the openScale driver uses

_STREAM_KINDS = frozenset({"realtime", "status", "history"})

_NOTIFY_BY_CHARACTERISTIC = {
    char: (service, kind) for service, char, kind in p.NOTIFY_CHARACTERISTICS
}
#: Subscriptions per phase, most important first. The proxy limit applies to
#: what is subscribed at the same time, so each phase replaces the previous one.
_PHASE_AUTH = (p.CHR_REQUEST_AUTH, p.CHR_AUTH_TOKEN, p.CHR_SEND_WORK_KEY)
_PHASE_SYNC = (p.CHR_CURRENT_TIME, p.CHR_GET_MANAGER_INFO, p.CHR_SET_USER_INFO)
_PHASE_MEASURE = (
    p.CHR_REALTIME_WEIGHT,
    p.CHR_STATUS_POLL,
    p.CHR_STATUS_RESULT,
    p.CHR_BODY_COMPOSITION_MEASUREMENT,
    p.CHR_WEIGHT_MEASUREMENT,
    p.CHR_BIND_REQUEST,
    p.CHR_PRODUCT_INFO,
    p.CHR_SCALE_VERSION,
)
_PHASE_HISTORY = (p.CHR_HISTORY_WEIGHT,)
_DISCONNECTED = "disconnected"


class SessionError(Exception):
    """The session could not be completed (timeout, disconnect, protocol)."""


class AuthenticationError(SessionError):
    """The scale rejected our CAK / C1 / C2 secrets."""


@dataclass(frozen=True)
class Secrets:
    cak: bytes
    c1: bytes
    c2: bytes

    def __post_init__(self) -> None:
        for name in ("cak", "c1", "c2"):
            if len(getattr(self, name)) != 16:
                raise ValueError(f"{name} must be 16 bytes")

    @classmethod
    def from_hex(cls, cak: str, c1: str, c2: str) -> "Secrets":
        return cls(bytes.fromhex(cak), bytes.fromhex(c1), bytes.fromhex(c2))


@dataclass(frozen=True)
class ScaleUser:
    """Profile pushed to the scale before weighing."""

    user_id: int
    male: bool
    age: int
    height_cm: float
    weight_kg: float


def is_hex_key(value: str, byte_length: int = 16) -> bool:
    """True for a hex string encoding exactly ``byte_length`` bytes."""
    return len(value) == byte_length * 2 and all(c in "0123456789abcdefABCDEF" for c in value)


class Scale3Session:
    def __init__(
        self,
        client: Any,
        address: str,
        secrets: Secrets,
        user: ScaleUser,
        *,
        read_history: bool = False,
        metric: bool = True,
        random_bytes: Callable[[int], bytes] = os.urandom,
        now: Callable[[], datetime] = datetime.now,
        on_state: Callable[[str], None] | None = None,
        on_measurement: Callable[[p.WeightMeasurement, bool], None] | None = None,
        on_info: Callable[[str, str], None] | None = None,
    ) -> None:
        self._client = client
        self._address = address
        self._secrets = secrets
        self._user = user
        self._read_history = read_history
        self._metric = metric
        self._random = random_bytes
        self._now = now
        self._on_state = on_state or (lambda _s: None)
        self._on_measurement = on_measurement or (lambda _m, _l: None)
        self._on_info = on_info or (lambda _k, _v: None)

        self._work_key: bytes | None = None
        self._accumulators: dict[str, p.FrameAccumulator] = {}
        self._inbox: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue()
        self._backlog: deque[tuple[str, bytes]] = deque()
        self._manager_huid: str | None = None
        self._active: dict[str, Any] = {}
        self._seen: dict[str, int] = {}
        self._last_status: int | None = None
        self.measurement_published = False

    # ------------------------------------------------------------------ io

    def notify_disconnected(self) -> None:
        """Wake any waiter: the link dropped."""
        self._inbox.put_nowait((_DISCONNECTED, b""))

    def _characteristic(self, service: str, characteristic: str) -> Any:
        services = self._client.services
        try:
            svc = services.get_service(service)
            return svc.get_characteristic(characteristic) if svc else None
        except Exception:  # noqa: BLE001 - bleak raises different errors per backend
            return None

    async def _subscribe(self, wanted: tuple[str, ...]) -> None:
        """Make ``wanted`` (characteristic UUIDs) the active subscriptions.

        ESPHome Bluetooth proxies (and some adapters) only hold about a dozen
        notification subscriptions per connection and refuse the rest with
        GATT error 128, so the session subscribes in phases (see ``_PHASE_*``)
        and drops what it no longer needs instead of subscribing to everything
        up front.
        """
        for uuid in list(self._active):
            if uuid not in wanted:
                await self._stop_notify(uuid)
        for uuid in wanted:
            if uuid not in self._active:
                await self._start_notify(uuid)

    async def _start_notify(self, uuid: str) -> None:
        service, kind = _NOTIFY_BY_CHARACTERISTIC[uuid]
        char = self._characteristic(service, uuid)
        if char is None:
            return

        def _callback(_sender: Any, data: bytearray, _kind: str = kind,
                      _uuid: str = uuid) -> None:
            self._on_notify(_uuid, _kind, bytes(data))

        required = kind in ("request_auth", "auth_token", "send_work_key")
        # A generic GATT error (e.g. 133) right after connecting is usually
        # transient on proxies and adapters; the app itself retries too.
        attempts = SUBSCRIBE_ATTEMPTS if required else 1
        for attempt in range(1, attempts + 1):
            try:
                await self._client.start_notify(char, _callback)
            except Exception as err:  # noqa: BLE001
                if attempt < attempts:
                    _LOGGER.debug("Subscribing to %s failed (%s); retrying", uuid, err)
                    await asyncio.sleep(SUBSCRIBE_RETRY_DELAY)
                    continue
                if required:
                    raise SessionError(f"cannot subscribe to {kind}: {err}") from err
                # Optional characteristics must not abort the session.
                level = logging.WARNING if uuid == p.CHR_REALTIME_WEIGHT else logging.DEBUG
                _LOGGER.log(level, "Cannot subscribe to %s: %s", uuid, err)
                return
            self._active[uuid] = char
            return

    async def _stop_notify(self, uuid: str) -> None:
        char = self._active.pop(uuid, None)
        if char is None:
            return
        try:
            await self._client.stop_notify(char)
        except Exception as err:  # noqa: BLE001 - best effort, the link may be going away
            _LOGGER.debug("Could not unsubscribe from %s: %s", uuid, err)

    def _on_notify(self, uuid: str, kind: str, data: bytes) -> None:
        if not p.has_notification_prefix(data):
            _LOGGER.debug("Ignoring unframed notification on %s (%d bytes)", uuid, len(data))
            return
        acc = self._accumulators.setdefault(uuid, p.FrameAccumulator())
        payload = acc.ingest(data)
        if payload is None:
            return
        if acc.last_completed_encrypted:
            if self._work_key is None:
                _LOGGER.debug("Encrypted notification on %s before work key", uuid)
                return
            try:
                payload = p.decrypt_payload(payload, self._work_key)
            except ValueError as err:
                _LOGGER.debug("Cannot decrypt notification on %s: %s", uuid, err)
                return
        _LOGGER.debug("RX %s: %s", kind, payload.hex())
        self._seen[kind] = self._seen.get(kind, 0) + 1
        self._inbox.put_nowait((kind, payload))

    async def _write(self, service: str, characteristic: str, payload: bytes,
                     *, encrypted_frames: list[bytes] | None = None) -> bool:
        char = self._characteristic(service, characteristic)
        if char is None:
            return False
        frames = encrypted_frames or p.build_write_frames(payload)
        _LOGGER.debug("TX %s (%d frame(s))", characteristic, len(frames))
        for frame in frames:
            await self._client.write_gatt_char(char, frame, response=True)
        return True

    async def _get(self, timeout: float) -> tuple[str, bytes]:
        """Next message, from the backlog first. Raises on timeout/disconnect."""
        if self._backlog:
            return self._backlog.popleft()
        return await self._next_inbox(timeout)

    async def _expect(self, kind: str, timeout: float) -> bytes:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        stashed: list[tuple[str, bytes]] = []
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                got_kind, payload = await self._next_inbox(remaining)
                if got_kind == kind:
                    return payload
                if got_kind in _STREAM_KINDS:
                    stashed.append((got_kind, payload))
                else:
                    self._handle_info(got_kind, payload)
        finally:
            self._backlog.extend(stashed)

    async def _next_inbox(self, timeout: float) -> tuple[str, bytes]:
        item = await asyncio.wait_for(self._inbox.get(), timeout)
        if item[0] == _DISCONNECTED:
            raise SessionError("scale disconnected")
        return item

    def _handle_info(self, kind: str, payload: bytes) -> None:
        if kind == "scale_version":
            version = p.parse_scale_version(payload)
            if version:
                self._on_info("sw_version", version.version)
        elif kind == "product_info":
            product = p.parse_product_id(payload)
            if product:
                self._on_info("product_id", product)
        elif kind == "manager_info":
            info = p.parse_manager_info(payload)
            if info and info.huid:
                self._manager_huid = info.huid
        elif kind == "weight_unit":
            unit = p.parse_weight_unit(payload)
            if unit:
                _LOGGER.debug("Scale weight unit: %s", unit.label)
        else:
            _LOGGER.debug("Unhandled %s payload (%d bytes)", kind, len(payload))

    # ----------------------------------------------------------------- flow

    async def run(self) -> None:
        await self._subscribe(_PHASE_AUTH)
        if not self._active or self._characteristic(p.SVC_USER_DATA, p.CHR_REQUEST_AUTH) is None:
            raise SessionError(
                "device does not expose the Huawei Hagrid GATT layout "
                "(is this really a Huawei Scale 3?)"
            )
        self._on_state("authenticating")
        await self._authenticate()
        await self._subscribe(_PHASE_SYNC)
        self._on_state("syncing")
        await self._sync_clock_and_profile()
        await self._subscribe(_PHASE_MEASURE)
        self._on_state("measuring")
        await self._measure()
        if self._read_history:
            await self._subscribe(_PHASE_HISTORY)
            self._on_state("reading_history")
            await self._drain_history()

    async def _authenticate(self) -> None:
        await self._write(p.SVC_USER_DATA, p.CHR_REQUEST_AUTH, b"")
        try:
            challenge = await self._expect("request_auth", AUTH_TIMEOUT)
        except TimeoutError as err:
            raise SessionError("no auth challenge from the scale") from err
        if len(challenge) < 16:
            raise SessionError(f"invalid auth challenge length {len(challenge)}")
        # Some firmwares append a status byte (Scale 3 fw 2.0.0.70: 17 bytes).
        rand_a = challenge[:16]
        rand_b = self._random(16)

        await self._write(
            p.SVC_USER_DATA, p.CHR_AUTH_TOKEN,
            p.build_auth_token_payload(rand_a, rand_b, self._secrets.cak),
        )
        try:
            response = await self._expect("auth_token", AUTH_TIMEOUT)
        except TimeoutError as err:
            raise AuthenticationError(
                "scale did not answer the auth token (wrong CAK?)"
            ) from err
        if not p.is_valid_auth_response(response, rand_a, rand_b, self._secrets.cak):
            raise AuthenticationError("scale's auth response did not verify (wrong CAK?)")

        root_key = p.derive_root_key(
            self._secrets.c1, self._secrets.c2, p.c3_from_bluetooth_address(self._address)
        )
        work_key = self._random(16)
        iv = self._random(16)
        self._work_key = work_key  # needed to decrypt an encrypted ack
        await self._write(
            p.SVC_USER_DATA, p.CHR_SEND_WORK_KEY, b"",
            encrypted_frames=p.build_encrypted_write_frames(work_key, root_key, iv),
        )
        try:
            ack = await self._expect("send_work_key", AUTH_TIMEOUT)
        except TimeoutError as err:
            raise AuthenticationError("work key not acknowledged (wrong C1/C2?)") from err
        if not ack or ack[0] != 0:
            raise AuthenticationError(
                f"work key rejected (status {ack[0] if ack else -1}; wrong C1/C2?)"
            )

    async def _sync_clock_and_profile(self) -> None:
        await self._write(
            p.SVC_CURRENT_TIME, p.CHR_CURRENT_TIME, p.current_time_payload(self._now())
        )
        # Best-effort: matches our own GET_WEIGHT_UNIT decoding, so unlike
        # SET_AGE/SET_GENDER/SET_HEIGHT this one is low-risk to guess at.
        await self._write(
            p.SVC_CURRENT_TIME, p.CHR_SET_WEIGHT_UNIT, p.weight_unit_payload(self._metric)
        )
        if await self._write(p.SVC_USER_DATA, p.CHR_GET_MANAGER_INFO, b""):
            try:
                payload = await self._expect("manager_info", MANAGER_INFO_TIMEOUT)
                self._handle_info("manager_info", payload)
            except TimeoutError:
                _LOGGER.debug("Manager info timed out; using a local HUID")

        assert self._work_key is not None
        user = self._user
        user_info = p.build_user_info_payload(
            huid=self._manager_huid
            or SYNTHETIC_HUID_PREFIX + self._address.replace(":", "").upper(),
            uid=f"u:{user.user_id:08X}",
            # Scale 3 uses male=1 / female=0 (the other Hagrid scales are inverted).
            gender=1 if user.male else 0,
            age_years=user.age,
            height_cm=int(round(user.height_cm)),
            weight_kg=user.weight_kg,
        )
        frames = p.build_encrypted_write_frames(user_info, self._work_key, self._random(16))
        if await self._write(p.SVC_USER_DATA, p.CHR_SET_USER_INFO, b"", encrypted_frames=frames):
            try:
                ack = await self._expect("set_user_info", USER_INFO_ACK_TIMEOUT)
                if ack[:1] != b"\x00":
                    # A real Scale 3 (fw 2.0.0.70) answers 01 here and still measures
                    # normally, as in openScale, so this is not an error.
                    _LOGGER.debug("Scale returned status %s for user profile", ack[:1].hex())
            except TimeoutError:
                _LOGGER.debug("Set-user-info ack timed out; continuing")

    async def _measure(self) -> None:
        loop = asyncio.get_running_loop()
        await self._write(p.SVC_BODY_COMPOSITION, p.CHR_REALTIME_WEIGHT, b"")
        # Identity reads, answered asynchronously and handled in the loop below.
        for service, characteristic in (
            (p.SVC_CURRENT_TIME, p.CHR_PRODUCT_INFO),
            (p.SVC_CURRENT_TIME, p.CHR_SCALE_VERSION),
        ):
            await self._write(service, characteristic, b"")

        can_poll = self._characteristic(p.SVC_CURRENT_TIME, p.CHR_STATUS_POLL) is not None
        pending: p.WeightMeasurement | None = None
        polls = 0
        next_poll = loop.time()
        final_requested_at: float | None = None
        deadline = loop.time() + MEASUREMENT_TIMEOUT

        while True:
            now = loop.time()
            if now >= deadline:
                break
            if can_poll and polls < MAX_STATUS_POLLS and now >= next_poll:
                await self._write(p.SVC_CURRENT_TIME, p.CHR_STATUS_POLL, b"")
                polls += 1
                next_poll = now + STATUS_POLL_INTERVAL
            elif (
                can_poll
                and polls >= MAX_STATUS_POLLS
                and pending is None
                and final_requested_at is None
                and now >= next_poll
            ):
                # The scale never reported "ready": ask for the data directly once.
                await self._write(p.SVC_BODY_COMPOSITION, p.CHR_REALTIME_WEIGHT, b"")
                final_requested_at = now
            if final_requested_at is not None and now - final_requested_at > FINAL_REQUEST_GRACE:
                break

            if can_poll and (polls < MAX_STATUS_POLLS or (pending is None and final_requested_at is None)):
                wake = min(next_poll, deadline)  # next status poll / final data request
            else:
                wake = deadline
            try:
                kind, payload = await self._get(max(0.01, wake - now))
            except TimeoutError:
                continue

            if kind == "realtime":
                measurement = p.parse_realtime_measurement(payload)
                if measurement is None or measurement.weight_kg <= 0:
                    _LOGGER.debug(
                        "Realtime payload ignored (%d bytes, parsed=%s)", len(payload), measurement
                    )
                    continue
                pending = measurement
                if measurement.impedance_ohm is not None:
                    # Weight + impedance present: this is the complete reading.
                    self._publish(measurement)
                    return
            elif kind == "status":
                status = payload[0] if payload else None
                self._last_status = status
                _LOGGER.debug("Measurement status %s", status)
                if status == 0x00:
                    if pending is not None:
                        # Scale says it is done but sent no impedance
                        # (e.g. shoes on): publish weight-only data.
                        self._publish(pending)
                        return
                    if final_requested_at is None:
                        await self._write(p.SVC_BODY_COMPOSITION, p.CHR_REALTIME_WEIGHT, b"")
                        final_requested_at = loop.time()
            else:
                self._handle_info(kind, payload)

        received = ", ".join(f"{k} x{n}" for k, n in sorted(self._seen.items())) or "nothing"
        raise SessionError(
            "timed out waiting for a measurement "
            f"(received {received}; last status {self._last_status}; "
            f"weight seen: {pending is not None}; impedance seen: "
            f"{pending is not None and pending.impedance_ohm is not None})"
        )

    def _publish(self, measurement: p.WeightMeasurement) -> None:
        self.measurement_published = True
        self._on_measurement(measurement, True)

    async def _drain_history(self) -> None:
        if not await self._write(p.SVC_BODY_COMPOSITION, p.CHR_HISTORY_WEIGHT, b""):
            return
        for _ in range(MAX_HISTORY_RECORDS):
            try:
                payload = await self._expect_stream("history", HISTORY_RECORD_TIMEOUT)
            except TimeoutError:
                return
            record = p.parse_history_measurement(payload)
            if record is None or record.weight_kg <= 0:
                return  # end of history / unsupported packet
            self._on_measurement(record, False)
            if record.is_history_complete:
                return
            await self._write(p.SVC_BODY_COMPOSITION, p.CHR_HISTORY_WEIGHT, b"\x00")

    async def _expect_stream(self, kind: str, timeout: float) -> bytes:
        """Like ``_expect`` but also consumes the backlog of stashed stream items."""
        for index, (stashed_kind, payload) in enumerate(self._backlog):
            if stashed_kind == kind:
                del self._backlog[index]
                return payload
        return await self._expect(kind, timeout)
