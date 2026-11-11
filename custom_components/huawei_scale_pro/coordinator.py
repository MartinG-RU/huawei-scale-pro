"""Connects to the scale when it wakes up and publishes what it measures.

The scale only advertises while someone is standing on it. We register an
active Bluetooth callback for its address; the first advertisement starts a
session (connect -> authenticate -> sync profile -> measure), after which we
disconnect and ignore the scale for a cool-down period.

Everything the session does is also recorded into a frame ring buffer (unless
the user turned it off), so a firmware that behaves unexpectedly can be
diagnosed from Home Assistant alone.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace
from datetime import timedelta
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM

from . import protocol as p
from .const import (
    AUTH_FAILURES_BEFORE_REAUTH,
    AUTH_RETRY_DELAY,
    CAPTURE_LIMIT,
    CONF_C1,
    CONF_C2,
    CONF_CAK,
    CONF_CAPTURE,
    CONF_COOLDOWN,
    CONF_GENDER_ENCODING,
    CONF_IMPEDANCE_UNITS,
    CONF_MODEL,
    CONF_PREFER_SCALE_BODY_FAT,
    CONF_PROFILES,
    CONF_READ_HISTORY,
    CONF_USER_MATCH,
    CONF_USER_MATCH_TOLERANCE,
    DEFAULT_C1,
    DEFAULT_C2,
    DEFAULT_CAK,
    DEFAULT_CAPTURE,
    DEFAULT_COOLDOWN,
    DEFAULT_GENDER_ENCODING,
    DEFAULT_IMPEDANCE_UNITS,
    DEFAULT_MODEL,
    DEFAULT_PREFER_SCALE_BODY_FAT,
    DEFAULT_READ_HISTORY,
    DEFAULT_USER_MATCH,
    DEFAULT_USER_MATCH_TOLERANCE,
    ASSIGNED_BY_GUEST_MODE,
    CONF_GUEST_AFTERMATH,
    DEFAULT_GUEST_AFTERMATH,
    DOMAIN,
    EVENT_MEASUREMENT,
    CONF_GUEST_PROFILE,
    EVENT_GUEST_MEASUREMENT,
    GUEST_DEVICE_NAME_RU,
    GUEST_NAME,
    GUEST_KEEP_DAYS,
    GUEST_OPTION,
    GUEST_STATUS_AUTO,
    GUEST_STATUS_MODE,
    RETRY_DELAY,
    SESSION_TIMEOUT,
)
from . import user_matching as um
from .models import (
    Profile,
    ScaleReading,
    build_guest_profile,
    build_reading,
    relabel_reading,
)
from .session import (
    AuthenticationError,
    FrameCapture,
    ScaleSession,
    ScaleUser,
    Secrets,
    SessionError,
)

if TYPE_CHECKING:
    from . import ScaleConfigEntry

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_HISTORY_KEYS = 256


class HuaweiScaleProCoordinator:
    """Event-driven coordinator (the scale pushes, nothing is polled)."""

    def __init__(self, hass: HomeAssistant, entry: ScaleConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.signal = f"{DOMAIN}_update_{entry.entry_id}"

        self.reading: ScaleReading | None = None
        self.status = "idle"
        self.last_error: str | None = None
        self.sw_version: str | None = None
        self.product_id: str | None = None
        self.active_profile_name: str | None = None
        #: Last product profile seen in an advertisement (may be UNKNOWN until
        #: the scale is woken up once).
        self.model: p.ProductProfile = p.profile_for_family(p.Family.UNKNOWN)
        self.model_source = "unknown"
        self.capability_bits: list[int] = []
        self.capture = FrameCapture(limit=CAPTURE_LIMIT)
        self.last_raw_hex: str | None = None
        # Advertisement bookkeeping: without these numbers a "nothing happens"
        # report is impossible to interpret — either the scale never reaches the
        # Bluetooth stack, or a session gate swallowed the advertisement.
        self.advertisements_seen = 0
        self.advertisements_ignored_busy = 0
        self.advertisements_ignored_backoff = 0
        self.sessions_started = 0
        self.last_advertisement_at: Any = None

        self._last_weights: dict[str, float] = {}
        #: Recent weights per profile, used to recognise who is on the scale.
        self._learned_weights: dict[str, list[float]] = {}
        #: A weight that fitted nobody, remembered so that the *same* strange
        #: weight on a later day is recognised as a real change, not a guest.
        self._outlier: um.Outlier | None = None
        #: Last guest weigh-in (one-off, not recorded as anybody's data).
        self.guest_weight_kg: float | None = None
        self.guest_at = None
        self.guest_count = 0
        #: Guest mode is live. Either chosen in the select, or switched on after a
        #: weight fitted nobody, when the household asked for a pause after guests.
        self.guest_mode = False
        #: True when guest mode was picked in the select, False when it was switched
        #: on automatically after an unattributable weigh-in.
        self._guest_mode_manual = False
        #: Set by the sensor platform so the guest's entities can be added lazily.
        self._guest_entity_hook: Callable[[], None] | None = None
        self._guest_entities_added = False
        #: The guest profile last sent to the scale, reused by the next guest
        #: weigh-in so consecutive ones stay comparable.
        self.guest_profile: Profile | None = None
        self.guest_reading: ScaleReading | None = None
        #: Reading of the last guest weigh-in started in guest mode: it was
        #: measured with a throwaway profile, so it may be replaced by the next.
        self.guest_mode_reading: ScaleReading | None = None
        self._history_keys: list[str] = []
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._task: asyncio.Task[None] | None = None
        self._next_allowed = 0.0
        self._auth_failures = 0
        self._failures = 0  # consecutive failed sessions
        self._reauth_started = False
        self._last_live_key: str | None = None

    # ---------------------------------------------------------------- config

    @property
    def profiles(self) -> list[Profile]:
        return [Profile.from_dict(raw) for raw in self.entry.options.get(CONF_PROFILES, [])]

    @property
    def active_profile(self) -> Profile | None:
        profiles = self.profiles
        for profile in profiles:
            if profile.name == self.active_profile_name:
                return profile
        return profiles[0] if profiles else None

    # -------------------------------------------------------------- selection

    @property
    def selection(self) -> str:
        """What the "who is weighing" select shows: a person's name or Guest.

        Guest is a mode, not a person: it is never stored as the active profile, so
        the person selected before it comes back by itself afterwards.
        """
        if self.guest_mode:
            return self.guest_option
        profile = self.active_profile
        return profile.name if profile else ""

    @property
    def guest_option(self) -> str:
        """The label the select shows next to the profiles (see const.GUEST_NAME)."""
        return GUEST_NAME

    async def async_set_selection(self, option: str) -> None:
        """Apply a select option: a household profile, or guest mode."""
        if option in (self.guest_option, GUEST_OPTION):
            await self.async_enter_guest_mode(manual=True)
            return
        await self.async_set_active_profile(option)

    @property
    def guest_aftermath(self) -> str:
        return self.entry.options.get(CONF_GUEST_AFTERMATH, DEFAULT_GUEST_AFTERMATH)

    async def async_enter_guest_mode(self, *, manual: bool = True) -> None:
        """Switch to (or stay in) guest mode and tell the entities."""
        self.async_provision_guest_device()
        if not self.guest_mode:
            _LOGGER.info(
                "Guest mode on (%s): weigh-ins are not assigned to anyone",
                "selected by hand" if manual else "after a weigh-in fitted nobody",
            )
        self.guest_mode = True
        self._guest_mode_manual = manual
        self._save()
        async_dispatcher_send(self.hass, self.signal)

    async def async_leave_guest_mode(self) -> None:
        if self.guest_mode:
            _LOGGER.info("Guest mode off: weigh-ins go to the selected profile again")
        self.guest_mode = False
        self._guest_mode_manual = False
        self._save()
        async_dispatcher_send(self.hass, self.signal)

    @property
    def secrets(self) -> Secrets:
        data = self.entry.data
        return Secrets.from_hex(
            data.get(CONF_CAK, DEFAULT_CAK),
            data.get(CONF_C1, DEFAULT_C1),
            data.get(CONF_C2, DEFAULT_C2),
        )

    @property
    def _options(self) -> dict[str, Any]:
        return dict(self.entry.options)

    def _forced_model(self) -> p.ProductProfile | None:
        """A model the user pinned in the options, if any."""
        value = self.entry.options.get(CONF_MODEL, DEFAULT_MODEL)
        if value in p.MODEL_FAMILY_ALIASES:
            return p.profile_for_family(p.MODEL_FAMILY_ALIASES[value])
        return None

    def _set_model(self, profile: p.ProductProfile, source: str) -> None:
        if profile.family != self.model.family or source != self.model_source:
            _LOGGER.debug(
                "Scale model: %s (family %s, source %s)",
                profile.display_name, profile.family, source,
            )
        self.model = profile
        self.model_source = source
        self._update_device_model(profile.display_name)
        async_dispatcher_send(self.hass, self.signal)

    def _devices(self) -> list[Any]:
        """The scale's own device entry (never the guest child device)."""
        registry = dr.async_get(self.hass)
        return [
            device
            for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id)
            if (DOMAIN, self.address) in device.identifiers
        ]

    def _update_device_model(self, display_name: str) -> None:
        """Keep the device registry entry in sync with the detected model."""
        registry = dr.async_get(self.hass)
        for device in self._devices():
            if device.model != display_name:
                registry.async_update_device(device.id, model=display_name)

    def _update_model_from_advertisement(self, service_info: BluetoothServiceInfoBleak) -> None:
        forced = self._forced_model()
        if forced is not None:
            self._set_model(forced, "options")
            return
        profile = p.identify_hagrid(
            service_info.name,
            service_info.manufacturer_data,
            {str(k): v for k, v in service_info.service_data.items()},
        )
        if profile is not None:
            self._set_model(profile, "advertisement")
        bits = p.find_capability_bits(
            service_info.name,
            service_info.manufacturer_data,
            {str(k): v for k, v in service_info.service_data.items()},
        )
        if bits:
            self.capability_bits = sorted(bits)
            if p.CAPABILITY_BIT_HIGH_FREQUENCY_IMPEDANCE in bits:
                _LOGGER.debug("Advertisement advertises dual-frequency impedance")

    async def async_set_active_profile(self, name: str) -> None:
        self.active_profile_name = name
        # Picking a person is also the way out of guest mode (and out of a pause
        # after a guest), so a household never keeps weighing itself into the guest
        # sensors by accident.
        self.guest_mode = False
        self._guest_mode_manual = False
        self._save()
        self._refresh_idle_status()
        async_dispatcher_send(self.hass, self.signal)

    # ------------------------------------------------------------- lifecycle

    @callback
    def async_set_guest_entity_hook(self, add_entities: Callable[[], None]) -> None:
        """Let the sensor platform hand over a way to add the guest's entities.

        The guest sensors are created with the first guest weigh-in rather than at
        setup, so a household that never has visitors gets no empty device.
        """
        self._guest_entity_hook = add_entities

    @callback
    def mark_guest_entities_added(self) -> None:
        self._guest_entities_added = True

    @callback
    def async_add_guest_entities(self) -> None:
        """Create the guest's sensors and their device, once."""
        if self._guest_entities_added or self._guest_entity_hook is None:
            return
        self._guest_entities_added = True
        self._guest_entity_hook()

    def async_provision_guest_device(self) -> None:
        """Create the Guest child device once a guest weigh-in exists.

        Guest data gets its own device so it never mixes with the household's
        sensors — the same grouping the Omron fork uses. It is not created until
        there is something to show, so an unused guest leaves no empty device.
        The guest reading itself is handed to the entities by ``async_guest_reading``.
        """
        if not self.guest_has_data:
            return
        registry = dr.async_get(self.hass)
        registry.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, f"{self.address}_guest")},
            name=GUEST_DEVICE_NAME_RU,
            manufacturer="Huawei",
            model=self.model.display_name,
            via_device=(DOMAIN, self.address),
        )

    @property
    def guest_has_data(self) -> bool:
        return (
            self.guest_reading is not None
            or self.guest_weight_kg is not None
            or self.guest_mode
        )

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.active_profile_name = data.get("active_profile")
        self._last_weights = dict(data.get("last_weights", {}))
        self._learned_weights = {
            str(name): [float(w) for w in weights]
            for name, weights in (data.get("learned_weights") or {}).items()
        }
        if raw_outlier := data.get("outlier"):
            try:
                at = dt_util.parse_datetime(raw_outlier[2]) if len(raw_outlier) > 2 else None
                self._outlier = um.Outlier(
                    str(raw_outlier[0]),
                    float(raw_outlier[1]),
                    at or dt_util.utcnow() - um.REANCHOR_MIN_GAP * 2,
                )
            except (IndexError, TypeError, ValueError):
                self._outlier = None
        guest = data.get("guest") or {}
        self.guest_weight_kg = guest.get("weight_kg")
        self.guest_count = int(guest.get("count", 0))
        if guest.get("at"):
            self.guest_at = dt_util.parse_datetime(guest["at"])
        self.guest_mode = bool(data.get("guest_mode"))
        self._guest_mode_manual = bool(data.get("guest_mode_manual"))
        if raw_guest_profile := data.get("guest_profile"):
            try:
                self.guest_profile = Profile.from_dict(raw_guest_profile)
            except (KeyError, TypeError, ValueError):
                self.guest_profile = None
        for attr, key in (
            ("guest_reading", "guest_reading"),
            ("guest_mode_reading", "guest_mode_reading"),
        ):
            if raw := data.get(key):
                try:
                    setattr(self, attr, ScaleReading.from_dict(raw))
                except (KeyError, TypeError, ValueError):
                    setattr(self, attr, None)
        # A guest is a one-off: numbers about a visitor nobody has seen for a week
        # are of no use, and a stale guest record must not linger in the entities.
        if self.guest_at is not None and dt_util.utcnow() - self.guest_at > timedelta(
            days=GUEST_KEEP_DAYS
        ):
            _LOGGER.debug("Dropping a guest weigh-in older than %d days", GUEST_KEEP_DAYS)
            self._clear_guest()
        self._history_keys = list(data.get("history_keys", []))
        self.sw_version = data.get("sw_version")
        self.product_id = data.get("product_id")
        self.capability_bits = list(data.get("capability_bits", []))
        family = data.get("model_family")
        if family:
            self.model = p.profile_for_family(family)
            self.model_source = data.get("model_source", "storage")
        if raw := data.get("reading"):
            try:
                self.reading = ScaleReading.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Ignoring unreadable stored reading")
        self.async_provision_guest_device()

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start listening for the scale; returns the stop callback."""
        cancel = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            BluetoothCallbackMatcher(address=self.address, connectable=True),
            BluetoothScanningMode.ACTIVE,
        )

        @callback
        def _stop() -> None:
            cancel()
            if self._task and not self._task.done():
                self._task.cancel()

        return _stop

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 2)

    async def async_flush(self) -> None:
        """Write pending state now (a delayed save is lost if the entry reloads)."""
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "active_profile": self.active_profile_name,
            "last_weights": self._last_weights,
            "learned_weights": self._learned_weights,
            "outlier": (
                [self._outlier.name, self._outlier.weight_kg, self._outlier.at.isoformat()]
                if self._outlier
                else None
            ),
            "guest": {
                "weight_kg": self.guest_weight_kg,
                "count": self.guest_count,
                "at": self.guest_at.isoformat() if self.guest_at else None,
            },
            "guest_mode": self.guest_mode,
            "guest_mode_manual": self._guest_mode_manual,
            "guest_profile": self.guest_profile.to_dict() if self.guest_profile else None,
            "guest_reading": self.guest_reading.to_dict() if self.guest_reading else None,
            "guest_mode_reading": (
                self.guest_mode_reading.to_dict() if self.guest_mode_reading else None
            ),
            "history_keys": self._history_keys[-MAX_HISTORY_KEYS:],
            "sw_version": self.sw_version,
            "product_id": self.product_id,
            "model_family": self.model.family,
            "model_source": self.model_source,
            "capability_bits": self.capability_bits,
            "reading": self.reading.to_dict() if self.reading else None,
        }

    def _set_status(self, status: str, error: str | None = None) -> None:
        if status == "idle":
            status, error = self._idle_status()
        self.status = status
        if status in ("idle", "guest"):
            self.last_error = None
        elif status == "paused":
            self.last_error = error or self.paused_reason
        elif error is not None:
            self.last_error = error
        async_dispatcher_send(self.hass, self.signal)

    def _idle_status(self) -> tuple[str, str | None]:
        """The status to show when nothing is running.

        A paused scale is not idle: it looks fine until somebody stands on it and
        nothing happens, so the reason has to be visible in the entity itself.
        """
        if self.paused:
            return "paused", self.paused_reason
        if self.guest_mode:
            return "guest", None
        return "idle", None

    def _refresh_idle_status(self) -> None:
        """Re-evaluate the "nothing is running" status after a mode change.

        Guest and paused are states of the coordinator, not outcomes of a session,
        so they have to be re-rendered when the selection changes — otherwise the
        status entity keeps saying "paused" after a person has been picked.
        """
        if self.status in ("idle", "guest", "paused"):
            self._set_status("idle")

    @property
    def guest_mode_manual(self) -> bool:
        """Guest mode was picked in the select (rather than switched on after a guest)."""
        return self._guest_mode_manual

    @property
    def paused(self) -> bool:
        """Sessions are refused until a household profile is selected again."""
        return (
            self.guest_aftermath == "pause"
            and self.guest_mode
            and not self._guest_mode_manual
        )

    @property
    def paused_reason(self) -> str | None:
        if not self.paused:
            return None
        return (
            "Взвешивание не принадлежит никому. Выберите человека в списке "
            "«Кто взвешивается», чтобы снова принимать измерения."
        )

    # ----------------------------------------------------------- BLE session

    @callback
    def _async_on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        self.advertisements_seen += 1
        self.last_advertisement_at = dt_util.utcnow()
        self._update_model_from_advertisement(service_info)
        if self._task and not self._task.done():
            self.advertisements_ignored_busy += 1
            _LOGGER.debug("Advertisement while a session is running; ignored")
            return
        if time.monotonic() < self._next_allowed:
            self.advertisements_ignored_backoff += 1
            _LOGGER.debug(
                "Advertisement during cool-down (%.0f s left); ignored",
                self._next_allowed - time.monotonic(),
            )
            return
        self._start_session()

    @callback
    def _start_session(self) -> None:
        self.sessions_started += 1
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._async_run_session(),
            name=f"{DOMAIN} session {self.address}",
        )

    async def async_trigger_session(self, force: bool = True) -> bool:
        """Start a session right now (button / service).

        The scale only advertises while it is being stood on, so a manual
        trigger is the reliable way to be listening *before* stepping on: the
        session waits for the measurement for up to ~150 s.
        """
        if self._task and not self._task.done():
            if not force:
                return False
            _LOGGER.debug("Pre-empting the running session for a manual trigger")
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._next_allowed = 0.0
        self._start_session()
        return True

    def _backoff(self, seconds: float) -> None:
        self._next_allowed = time.monotonic() + seconds

    async def _async_run_session(self) -> None:
        if self.paused:
            # Guest sensors hold a guest's numbers and nobody told the scale who is
            # next: measuring now would either overwrite them with somebody's data
            # or start a second guest. Wait for a selection instead.
            self._backoff(RETRY_DELAY)
            _LOGGER.debug("Session skipped: paused after a guest weigh-in")
            return
        profile = self.guest_session_profile() or self.active_profile
        if profile is None:
            _LOGGER.warning("No user profile configured for %s", self.entry.title)
            self._backoff(RETRY_DELAY)
            return

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            # It may still be known to a passive-only scanner; that is not
            # enough to connect, but the message tells the two cases apart.
            passive = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=False
            )
            if passive is not None:
                _LOGGER.warning(
                    "%s is only seen by a scanner that cannot hold a connection. "
                    "Home Assistant needs a local adapter or an ESPHome Bluetooth "
                    "proxy with active connections.",
                    self.address,
                )
            else:
                _LOGGER.debug("%s is not currently visible to Bluetooth", self.address)
            self._backoff(5)
            return

        options = self._options
        today = dt_util.now().date()
        scale_user = ScaleUser(
            user_id=profile.user_id,
            male=profile.male,
            age=max(0, profile.age_on(today)),
            height_cm=profile.height_cm,
            weight_kg=(
                profile.initial_weight_kg
                if profile.is_guest
                else self._last_weights.get(profile.name, profile.initial_weight_kg)
            ),
        )
        capture_enabled = options.get(CONF_CAPTURE, DEFAULT_CAPTURE)
        model = self._forced_model() or self.model
        client: BleakClient | None = None
        session: ScaleSession | None = None
        self._set_status("connecting")
        try:
            async with asyncio.timeout(SESSION_TIMEOUT):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.entry.title,
                    disconnected_callback=lambda _c: session and session.notify_disconnected(),
                    ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                        self.hass, self.address, connectable=True
                    )
                    or ble_device,
                )
                session = ScaleSession(
                    client,
                    self.address,
                    self.secrets,
                    scale_user,
                    model=model,
                    read_history=options.get(CONF_READ_HISTORY, DEFAULT_READ_HISTORY),
                    metric=self.hass.config.units is METRIC_SYSTEM,
                    gender_encoding=options.get(
                        CONF_GENDER_ENCODING, DEFAULT_GENDER_ENCODING
                    ),
                    impedance_units=options.get(
                        CONF_IMPEDANCE_UNITS, DEFAULT_IMPEDANCE_UNITS
                    ),
                    capture=self.capture if capture_enabled else None,
                    probe_unknown=capture_enabled,
                    now=dt_util.now,
                    on_state=self._set_status,
                    on_measurement=lambda m, live: self._on_measurement(m, live, profile),
                    on_info=self._on_info,
                )
                await session.run()
        except AuthenticationError as err:
            await self._async_handle_auth_failure(err)
            return
        except (SessionError, BleakError, TimeoutError, OSError) as err:
            # A failed attempt is routine (stepping off early, out of range), so
            # only the first failure in a row is a warning.
            self._failures += 1
            _LOGGER.log(
                logging.WARNING if self._failures == 1 else logging.DEBUG,
                "Could not read the scale (%s): %s", self.address, err or type(err).__name__,
            )
            self._set_status("error", str(err) or type(err).__name__)
            self._log_capture()
            self._backoff(RETRY_DELAY)
            return
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except (BleakError, OSError, TimeoutError):
                    pass

        self._auth_failures = 0
        self._failures = 0
        self._set_status("idle")
        self._backoff(options.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))

    def _log_capture(self) -> None:
        """Log the tail of the frame buffer after a failed session."""
        if not self.capture:
            return
        _LOGGER.debug("Last BLE frames:")
        for record in list(self.capture.records)[-40:]:
            _LOGGER.debug("  %s", record.as_line())

    async def _async_handle_auth_failure(self, err: AuthenticationError) -> None:
        self._auth_failures += 1
        _LOGGER.warning(
            "The scale rejected the authentication (%d/%d): %s",
            self._auth_failures, AUTH_FAILURES_BEFORE_REAUTH, err,
        )
        self._set_status("auth_error", str(err))
        self._backoff(AUTH_RETRY_DELAY)
        if self._auth_failures >= AUTH_FAILURES_BEFORE_REAUTH and not self._reauth_started:
            self._reauth_started = True
            self.entry.async_start_reauth(self.hass)

    def reset_auth_state(self) -> None:
        self._auth_failures = 0
        self._reauth_started = False

    # ------------------------------------------------------------ data in

    def _on_info(self, key: str, value: str) -> None:
        if key == "sw_version" and value != self.sw_version:
            self.sw_version = value
            registry = dr.async_get(self.hass)
            for device in self._devices():
                registry.async_update_device(device.id, sw_version=value)
            self._save()
        elif key == "product_id" and value != self.product_id:
            self.product_id = value
            # The product-info id is a second opinion on the model, but it can be
            # coarser than the scan marker (a Scale 3 fw reports "M0CJ"), so it
            # only refines an UNKNOWN family.
            if self.model.family == p.Family.UNKNOWN:
                profile = p.product_profile_for_marker(value)
                if profile.family != p.Family.UNKNOWN:
                    self._set_model(profile, "product-info")
            self._save()

    def _on_measurement(
        self, measurement: p.WeightMeasurement, live: bool, profile: Profile
    ) -> None:
        key = measurement.dedupe_key
        if live:
            if key == self._last_live_key:
                _LOGGER.debug("Ignoring repeat of the previous measurement")
                return
            self._last_live_key = key
            if measurement.timestamp:
                self._remember_history_key(key)  # so the same record is not replayed
            timestamp = dt_util.utcnow()
            self.last_raw_hex = (
                measurement.raw_payload.hex().upper() if measurement.raw_payload else None
            )
        else:
            if key in self._history_keys:
                return
            self._remember_history_key(key)
            if measurement.timestamp is None:
                return
            if profile.is_guest:
                # A guest session is about the person standing on the scale right
                # now; the scale's memory holds the household's earlier weigh-ins.
                _LOGGER.debug("Ignoring a stored record during guest mode")
                return
            timestamp = dt_util.as_utc(
                measurement.timestamp.replace(tzinfo=dt_util.get_default_time_zone())
            )

        # Who is actually on the scale? The profile sent at the start of the
        # session may be the wrong one; the weight decides.
        attribution = self._attribute(measurement.weight_kg, profile, live=live)
        if attribution.guest:
            if profile.is_guest:
                # Guest mode: measured for a visitor on purpose. The composition is
                # kept, but on the guest's own sensors and under nobody's name.
                self._record_guest_mode(measurement, timestamp, profile)
            else:
                self._record_guest(measurement, timestamp)
            return
        target = attribution.profile
        decision = attribution.decision
        if attribution.reanchored:
            # The same unusual weight twice: this profile's history is stale.
            self._learned_weights.pop(target.name, None)
            _LOGGER.info(
                "Weight %.2f kg repeated for %s — restarting its weight history",
                measurement.weight_kg, target.name,
            )

        # The scale's own body-fat percentage was computed with the profile it
        # was given before measuring, so it only means something when that
        # profile is the person who stepped on. Otherwise use the impedance.
        prefer_scale_body_fat = bool(
            self.entry.options.get(CONF_PREFER_SCALE_BODY_FAT, DEFAULT_PREFER_SCALE_BODY_FAT)
        ) and target.name == profile.name

        reading = build_reading(
            measurement,
            target,
            timestamp=timestamp,
            today=dt_util.now().date(),
            history=not live,
            impedance_units=self.entry.options.get(
                CONF_IMPEDANCE_UNITS, DEFAULT_IMPEDANCE_UNITS
            ),
            prefer_scale_body_fat=prefer_scale_body_fat,
        )
        if decision.matched:
            reading = replace(
                reading,
                assigned_by="weight",
                match_reference_kg=decision.reference_kg,
                match_distance_kg=decision.distance_kg,
                match_basis=decision.basis,
                match_ambiguous=decision.ambiguous,
            )
        self.hass.bus.async_fire(EVENT_MEASUREMENT, self._event_data(reading))
        if live:
            self.reading = reading
            self._last_weights[target.name] = reading.weight_kg
            self._learned_weights = um.add_sample(
                self._learned_weights, target.name, reading.weight_kg
            )
            self._save()
            async_dispatcher_send(self.hass, self.signal)
            _LOGGER.debug(
                "New measurement for %s (assigned by %s, ref %s kg): %.2f kg "
                "(composition: %s, source: %s, len: %s, z_raw_low: %s, "
                "z_raw_high: %s, payload: %s)",
                target.name, reading.assigned_by, reading.match_reference_kg,
                reading.weight_kg, reading.composition, reading.source,
                reading.payload_length, reading.impedance_raw_low,
                reading.impedance_raw_high, reading.payload_hex,
            )

    # --------------------------------------------------------- user matching

    def learned_weights(self) -> dict[str, list[float]]:
        """Recent weights per profile — what user matching works on."""
        return {name: list(weights) for name, weights in self._learned_weights.items()}

    def _user_match_mode(self) -> str:
        return self.entry.options.get(CONF_USER_MATCH, DEFAULT_USER_MATCH)

    def _user_match_tolerance(self) -> float:
        return float(
            self.entry.options.get(CONF_USER_MATCH_TOLERANCE, DEFAULT_USER_MATCH_TOLERANCE)
        )

    def _attribute(
        self, weight_kg: float, session_profile: Profile, *, live: bool
    ) -> um.Attribution:
        """Decide who this weigh-in belongs to (possibly nobody: a guest)."""
        mode = self._user_match_mode()
        tolerance = self._user_match_tolerance()

        if not live:
            # Stored measurements from the scale's memory: attribute them like
            # any other weighing, but never treat them as guest weigh-ins.
            decision = um.resolve(
                weight_kg,
                self.profiles,
                self._learned_weights,
                mode=mode,
                tolerance_kg=tolerance,
                active_name=session_profile.name,
            )
            target = decision.profile or session_profile
            return um.Attribution(profile=target, decision=decision)

        attribution = um.attribute(
            weight_kg,
            self.profiles,
            self._learned_weights,
            session_profile=session_profile,
            mode=mode,
            tolerance_kg=tolerance,
            previous_outlier=self._outlier,
            now=dt_util.utcnow(),
        )
        if attribution.guest and not session_profile.is_guest:
            # Remembered so that the same unusual weight on a later day counts as a
            # real change of the selected person's weight, not a second guest.
            self._outlier = um.Outlier(session_profile.name, weight_kg, dt_util.utcnow())
        else:
            self._outlier = None
        if attribution.decision.matched and attribution.profile.name != session_profile.name:
            _LOGGER.info(
                "Weight %.2f kg belongs to %s (%.2f kg, off by %.2f kg), not the "
                "selected %s — composition recomputed for %s",
                weight_kg, attribution.profile.name,
                attribution.decision.reference_kg or 0.0,
                attribution.decision.distance_kg or 0.0,
                session_profile.name, attribution.profile.name,
            )
        if not attribution.guest and attribution.profile.name != self.active_profile_name:
            # Sticky selection: the next session starts with the right profile,
            # so from then on the scale's own body-fat figure is correct too.
            self.active_profile_name = attribution.profile.name
            self._save()
            async_dispatcher_send(self.hass, self.signal)
        return attribution

    def _record_guest_mode(
        self,
        measurement: p.WeightMeasurement,
        timestamp: Any,
        profile: Profile,
    ) -> None:
        """A weigh-in in guest mode: composition kept, assignment refused.

        Nothing here touches a person's sensors, their learned weight or their
        history: the guest is measured with a throwaway profile, and the reading is
        published only on the guest's own device (and as an event).
        """
        prefer_scale_body_fat = bool(
            self.entry.options.get(CONF_PREFER_SCALE_BODY_FAT, DEFAULT_PREFER_SCALE_BODY_FAT)
        )
        reading = build_reading(
            measurement,
            profile,
            timestamp=timestamp,
            today=dt_util.now().date(),
            history=False,
            impedance_units=self.entry.options.get(
                CONF_IMPEDANCE_UNITS, DEFAULT_IMPEDANCE_UNITS
            ),
            prefer_scale_body_fat=prefer_scale_body_fat,
        )
        reading = relabel_reading(
            reading, guest=True, assigned_by=ASSIGNED_BY_GUEST_MODE
        )
        self.guest_mode_reading = reading
        self._store_guest_reading(reading)
        self.hass.bus.async_fire(EVENT_MEASUREMENT, self._event_data(reading))
        self.hass.bus.async_fire(
            EVENT_GUEST_MEASUREMENT, self._guest_event_data(reading)
        )
        _LOGGER.info(
            "Guest weigh-in (guest mode, %s): %.2f kg, not assigned to anyone",
            reading.composition, reading.weight_kg,
        )

    def _record_guest(self, measurement: p.WeightMeasurement, timestamp: Any) -> None:
        """A weight that fits nobody: publish it, but keep it out of the data."""
        self.guest_weight_kg = round(measurement.weight_kg, 2)
        self.guest_at = timestamp
        self.guest_count += 1
        # The guest's own weight is the starting point for the profile sent at the
        # next guest weigh-in (see build_guest_profile).
        if self.guest_profile is None:
            self.guest_profile = build_guest_profile(
                None, measurement, today=dt_util.now().date(), status=GUEST_STATUS_AUTO
            )
        self.async_add_guest_entities()
        self._save()
        async_dispatcher_send(self.hass, self.signal)
        if self.guest_aftermath == "pause":
            # Ask the household who this was: the scale stays silent until the
            # select names a person, so a visiting guest cannot weigh themselves
            # into the guest sensors over and over and nobody's data is touched.
            self.hass.async_create_task(self._async_pause_after_guest())
        known = self.learned_weights()
        _LOGGER.info(
            "Guest weigh-in: %.2f kg fits nobody within %.1f kg — not recorded as "
            "anyone's measurement. Known weights: %s",
            measurement.weight_kg,
            self._user_match_tolerance(),
            ", ".join(
                f"{name}: {sum(w) / len(w):.1f} kg" if w else f"{name}: no data"
                for name, w in known.items()
            )
            or "none",
        )
        self.hass.bus.async_fire(
            EVENT_GUEST_MEASUREMENT,
            {
                "address": self.address,
                "entry_id": self.entry.entry_id,
                "weight_kg": round(measurement.weight_kg, 2),
                # ``impedance_ohm`` is a method (the unit convention is an
                # option), not an attribute.
                "impedance_ohm": measurement.impedance_ohm(
                    self.entry.options.get(CONF_IMPEDANCE_UNITS, DEFAULT_IMPEDANCE_UNITS)
                ),
                "impedance_high_ohm": measurement.impedance_high_ohm(
                    self.entry.options.get(CONF_IMPEDANCE_UNITS, DEFAULT_IMPEDANCE_UNITS)
                ),
                "payload_hex": (
                    measurement.raw_payload.hex().upper()
                    if measurement.raw_payload
                    else None
                ),
                "assigned_to": None,
                "known_weights": known,
            },
        )

    async def _async_pause_after_guest(self) -> None:
        """Switch guest mode on so the scale waits for a selection."""
        await self.async_enter_guest_mode(manual=False)
        _LOGGER.info(
            "Paused after a guest weigh-in: select a person to start measuring again"
        )

    # ------------------------------------------------------------ guest mode

    def guest_options(self) -> dict[str, Any]:
        """What the household typed about guests in the options."""
        raw = self.entry.options.get(CONF_GUEST_PROFILE)
        return dict(raw) if isinstance(raw, dict) else {}

    def guest_session_profile(self) -> Profile | None:
        """The profile a weigh-in in guest mode is measured with.

        Guest mode measures for a visitor whose height, age and sex nobody knows, so
        the scale gets a throwaway profile: typed in the options, or guessed from
        the previous guest weigh-in. It is not a person and is never selected.
        """
        if not self.guest_mode:
            return None
        stored = self.guest_options()
        previous = {
            key: value
            for key, value in (self.guest_profile.to_dict() if self.guest_profile else {}).items()
            if key in ("height_cm", "birth_date", "male")
        }
        # Anything typed in the options replaces the guess it corresponds to.
        for typed, guessed in (
            ("height_cm", "height_cm"),
            ("age", "birth_date"),
            ("birth_date", "birth_date"),
            ("male", "male"),
        ):
            if typed in stored:
                previous.pop(guessed, None)
        merged = {**previous, **stored}
        measurement = None
        if self.guest_weight_kg:
            measurement = p.WeightMeasurement(
                raw_length=0,
                weight_kg=float(self.guest_weight_kg),
                fat_percent=0.0,
                timestamp=None,
                low_impedance=(),
                high_impedance=(),
                heart_rate=None,
            )
        self.guest_profile = build_guest_profile(
            merged, measurement, today=dt_util.now().date(), status=GUEST_STATUS_MODE
        )
        return self.guest_profile

    def _store_guest_reading(self, reading: ScaleReading) -> None:
        """Keep a guest's reading: its own sensors, and only while it is fresh.

        Guest data does not accumulate and is not attributed to anyone: the last
        guest reading replaces the previous one, and everything expires after
        ``GUEST_KEEP_DAYS``.
        """
        self.guest_reading = reading
        self.guest_weight_kg = round(reading.weight_kg, 2)
        self.guest_at = reading.timestamp
        self.guest_count += 1
        # Guest mode stays on until a person is selected again: a household that is
        # hosting someone wants every weigh-in of the visit to stay a guest's, and
        # the select keeps saying so. Nothing of it is filed under anybody.
        self._guest_mode_manual = False
        self.async_add_guest_entities()
        self._save()
        async_dispatcher_send(self.hass, self.signal)

    def _clear_guest(self) -> None:
        """Forget everything about the last guest."""
        self.guest_reading = None
        self.guest_mode_reading = None
        self.guest_weight_kg = None
        self.guest_at = None

    async def async_forget_guest(self) -> None:
        """The button: drop the guest data and leave guest mode."""
        if not self.guest_has_data:
            return
        self._clear_guest()
        self.guest_mode = False
        self._guest_mode_manual = False
        self._save()
        self._refresh_idle_status()
        async_dispatcher_send(self.hass, self.signal)
        _LOGGER.info("Guest data cleared")

    def _guest_event_data(self, reading: ScaleReading) -> dict[str, Any]:
        """Event payload for a guest weigh-in measured in guest mode."""
        data = self._event_data(reading)
        data["assigned_to"] = None
        data["guest"] = True
        data["known_weights"] = self.learned_weights()
        return data

    def _remember_history_key(self, key: str) -> None:
        if key not in self._history_keys:
            self._history_keys.append(key)
            del self._history_keys[:-MAX_HISTORY_KEYS]
            self._save()

    def _event_data(self, reading: ScaleReading) -> dict[str, Any]:
        data = reading.to_dict()
        data["address"] = self.address
        data["entry_id"] = self.entry.entry_id
        data["model"] = self.model.display_name
        data["user"] = data.pop("profile")
        return data

    # ------------------------------------------------------------ diagnostics

    def frames_text(self) -> str:
        """Human-readable dump of the captured frames."""
        header = (
            f"# {DOMAIN} frame dump\n"
            f"# scale: {self.address} ({self.model.display_name}, "
            f"family {self.model.family}, source {self.model_source})\n"
            f"# sw: {self.sw_version or '?'} product: {self.product_id or '?'} "
            f"caps: {self.capability_bits}\n"
            f"# last error: {self.last_error or '-'}\n"
            f"# {len(self.capture)} frame(s), oldest first\n"
        )
        return header + self.capture.as_text()
