"""Constants for the Huawei Scale 3 Pro (Hagrid WSP) integration."""
from __future__ import annotations

DOMAIN = "huawei_scale_pro"

# Config entry data. These are optional overrides: when an entry has no keys
# the built-in defaults below are used.
CONF_CAK = "cak"
CONF_C1 = "c1"
CONF_C2 = "c2"

# Built-in Huawei Health secrets (recovered from the app's native library: the
# library calls them CAK (id 33), A (id 1033) and B (id 2033); C1/C2 here are
# A/B). Each is 16 bytes, hex-encoded. They are family-wide, i.e. shared by the
# Scale 3, Scale 3 Pro, Scale 2 Pro and the Honor/Hagrid scales.
DEFAULT_CAK = "90B96ECA297EF78717E66E491084D3F8"
DEFAULT_C1 = "CA4946D061C9FE534F6044F930EBB69B"
DEFAULT_C2 = "FBCE6E2B4BAF80ED969BA26B4A4B9325"

# Config entry options
CONF_PROFILES = "profiles"
CONF_READ_HISTORY = "read_history"
CONF_COOLDOWN = "cooldown"
#: Store every BLE frame of a session in a ring buffer (diagnostics/probe mode).
CONF_CAPTURE = "capture_frames"
#: Which model to assume when the advertisement does not identify one.
CONF_MODEL = "model"
#: How the 7th user-profile byte is encoded: auto | scale3 | inverted.
CONF_GENDER_ENCODING = "gender_encoding"
#: How raw impedance words are converted to ohm: auto | tenths | ohms.
CONF_IMPEDANCE_UNITS = "impedance_units"
#: Prefer the scale's own body-fat figure over the computed model, when present.
CONF_PREFER_SCALE_BODY_FAT = "prefer_scale_body_fat"
CONF_USER_MATCH = "user_match"
CONF_USER_MATCH_TOLERANCE = "user_match_tolerance_kg"
#: Short answer to a guest weigh-in: keep the reading for a while, or stop
#: measuring entirely until a household profile is selected again.
CONF_GUEST_AFTERMATH = "guest_aftermath"

DEFAULT_READ_HISTORY = False
DEFAULT_COOLDOWN = 60  # seconds to ignore the scale after a successful weigh-in
MIN_COOLDOWN = 10
MAX_COOLDOWN = 600
DEFAULT_CAPTURE = True  # cheap, and the only way to iterate on a new firmware
DEFAULT_MODEL = "auto"
DEFAULT_GENDER_ENCODING = "auto"
DEFAULT_IMPEDANCE_UNITS = "auto"
DEFAULT_PREFER_SCALE_BODY_FAT = True  # app-compatible composition out of the box
DEFAULT_USER_MATCH = "history"  # off | history | history_and_initial
DEFAULT_USER_MATCH_TOLERANCE = 2.0
#: What happens right after a guest weigh-in. ``keep`` leaves everything as it
#: is (the guest reading stays on its own sensors); ``pause`` stops opening
#: sessions until a household profile is selected again, which is the only way
#: to keep a guest's repeat weigh-ins from overwriting the guest sensors.
GUEST_AFTERMATH_OPTIONS = ["keep", "pause"]
DEFAULT_GUEST_AFTERMATH = "keep"

GENDER_ENCODING_OPTIONS = ["auto", "scale3", "inverted"]
IMPEDANCE_UNIT_OPTIONS = ["auto", "tenths", "ohms"]
USER_MATCH_OPTIONS = ["history", "history_and_initial", "off"]
MIN_USER_MATCH_TOLERANCE = 0.5
MAX_USER_MATCH_TOLERANCE = 15.0
MODEL_OPTIONS = [
    "auto",
    "scale3_pro",
    "scale3",
    "scale2_pro",
    "honor_scale2",
    "generic",
]

# Profile fields (stored in options)
CONF_NAME = "name"
CONF_SEX = "sex"
CONF_BIRTH_DATE = "birth_date"
CONF_HEIGHT = "height_cm"
CONF_INITIAL_WEIGHT = "initial_weight_kg"

SEX_MALE = "male"
SEX_FEMALE = "female"

EVENT_MEASUREMENT = f"{DOMAIN}_measurement"
#: Fired when a weight fits nobody: a one-off "guest" weigh-in. Nothing is
#: recorded as anybody's data (see the user-match option).
EVENT_GUEST_MEASUREMENT = f"{DOMAIN}_guest_measurement"

# --- Guest mode -------------------------------------------------------------
# A guest weigh-in used to be detected only after the fact, from the weight. It
# can also be chosen in advance — the select gets a ``__guest__`` option next to
# the profiles, like the "Who is measuring" select of the Omron fork — and then
# the scale needs a profile before it measures, so it gets one derived from the
# guest's own first weigh-in.
#: Select option value for guest mode. Profile names are used verbatim as their
#: option values, so this sentinel cannot clash with a real person.
GUEST_OPTION = "__guest__"
#: The profile name a guest reading is published under.
GUEST_NAME = "Гость"
#: Registered statuses of the auto-detected guest weigh-in: one-off, replaced by
#: the next guest weigh-in, and reset as soon as somebody weighs in properly.
GUEST_STATUS_AUTO = "Гость — последнее взвешивание"
#: Registered status of a weigh-in made in guest mode: kept like the household's
#: own history (see ``keep_guest_measurements``), but never assigned to a person.
GUEST_STATUS_MODE = "Гость — режим выбран вручную"
#: Statuses that mark a registered guest measurement.
GUEST_STATUSES = (GUEST_STATUS_AUTO, GUEST_STATUS_MODE)
#: Where the remembered guest weigh-in came from in the reading's ``assigned_by``.
ASSIGNED_BY_GUEST_MODE = "guest_mode"
ASSIGNED_BY_GUEST_AUTO = "guest_auto"
#: When guest mode was chosen by hand with no fresh reading to start from.
GUEST_DEFAULT_HEIGHT_CM = 170.0
GUEST_DEFAULT_MALE = True
GUEST_DEFAULT_AGE = 40
#: Everything a guest profile needs but the household does not know, typed in the
#: options and stored under this key (never as a person).
CONF_GUEST_PROFILE = "guest_profile"
#: Guest data older than this is dropped: a guest is a one-off, and stale
#: numbers about a visitor are of no use to anyone.
GUEST_KEEP_DAYS = 7
#: Name of the child device that carries a guest's sensors. The integration's UI
#: is Russian-only, so the name is not translated.
GUEST_DEVICE_NAME_RU = "Гостевые данные"

# Session outcome after an error, before we try again on the next advertisement
RETRY_DELAY = 15
AUTH_RETRY_DELAY = 120
AUTH_FAILURES_BEFORE_REAUTH = 3
SESSION_TIMEOUT = 150

#: How many frames a capture keeps per device (≈ two weigh-ins).
CAPTURE_LIMIT = 600

STATUS_OPTIONS = [
    "idle",
    "guest",
    "paused",
    "connecting",
    "authenticating",
    "syncing",
    "measuring",
    "reading_history",
    "error",
    "auth_error",
]
