"""Config flow for the Huawei Scale 3 Pro."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BIRTH_DATE,
    CONF_GUEST_AFTERMATH,
    CONF_GUEST_PROFILE,
    DEFAULT_GUEST_AFTERMATH,
    GUEST_AFTERMATH_OPTIONS,
    CONF_C1,
    CONF_C2,
    CONF_CAK,
    CONF_CAPTURE,
    CONF_COOLDOWN,
    CONF_GENDER_ENCODING,
    CONF_HEIGHT,
    CONF_IMPEDANCE_UNITS,
    CONF_INITIAL_WEIGHT,
    CONF_MODEL,
    CONF_NAME,
    CONF_PREFER_SCALE_BODY_FAT,
    CONF_PROFILES,
    CONF_USER_MATCH,
    CONF_USER_MATCH_TOLERANCE,
    CONF_READ_HISTORY,
    CONF_SEX,
    DEFAULT_CAPTURE,
    DEFAULT_COOLDOWN,
    DEFAULT_GENDER_ENCODING,
    DEFAULT_IMPEDANCE_UNITS,
    DEFAULT_MODEL,
    DEFAULT_PREFER_SCALE_BODY_FAT,
    DEFAULT_READ_HISTORY,
    DEFAULT_USER_MATCH,
    DEFAULT_USER_MATCH_TOLERANCE,
    DOMAIN,
    GENDER_ENCODING_OPTIONS,
    IMPEDANCE_UNIT_OPTIONS,
    MAX_USER_MATCH_TOLERANCE,
    MIN_USER_MATCH_TOLERANCE,
    USER_MATCH_OPTIONS,
    MAX_COOLDOWN,
    MIN_COOLDOWN,
    MODEL_OPTIONS,
    SEX_FEMALE,
    SEX_MALE,
)
from .protocol import identify_hagrid
from .session import is_hex_key

_KEY_FIELDS = (CONF_CAK, CONF_C1, CONF_C2)

_MAC_RE = re.compile(r"^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$")

#: Optional override, only offered when re-authenticating: CAK, C1 and C2
#: concatenated (32 hex chars each, 96 total). Left blank, the built-in keys
#: from const.py are used.
AUTH_KEY_FIELD = "auth_key"

_DOB_ISO_RE = re.compile(r"^(\d{4})\D?(\d{1,2})\D?(\d{1,2})$")
_DOB_DMY_RE = re.compile(r"^(\d{1,2})\D(\d{1,2})\D(\d{4})$")


def parse_birth_date(raw: object) -> date | None:
    """Parse a typed date of birth, or None if it is not a valid past date.

    Accepts year-first (``1985-03-27``, ``1985/3/27``, ``19850327``) and
    day-first (``27/03/1985``, ``27.3.1985``) with any single non-digit
    separator. Day-first is the only reading of a date that ends in the year.
    """
    text = str(raw or "").strip()
    try:
        if match := _DOB_ISO_RE.match(text):
            year, month, day = (int(g) for g in match.groups())
        elif match := _DOB_DMY_RE.match(text):
            day, month, year = (int(g) for g in match.groups())
        else:
            return None
        born = date(year, month, day)
    except ValueError:
        return None
    return born if date(1900, 1, 1) <= born < date.today() else None


def _normalise_mac(raw: str) -> str | None:
    value = raw.strip().upper()
    return value.replace("-", ":") if _MAC_RE.match(value) else None


def _clean_key(raw: str) -> str:
    return re.sub(r"[\s:\-]", "", raw).upper()


def _auth_key_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(AUTH_KEY_FIELD, default=""): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


def _validate_auth_key(user_input: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return ({CAK, C1, C2}, errors) split from the combined field.

    A blank field returns no keys and no errors, meaning "use the built-in keys".
    """
    cleaned = _clean_key(user_input.get(AUTH_KEY_FIELD, ""))
    if not cleaned:
        return {}, {}
    if not is_hex_key(cleaned, byte_length=48):
        return {}, {AUTH_KEY_FIELD: "invalid_key"}
    return {CONF_CAK: cleaned[0:32], CONF_C1: cleaned[32:64], CONF_C2: cleaned[64:96]}, {}


def _profile_schema(defaults: Mapping[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=d.get(CONF_NAME, "")): TextSelector(),
            vol.Required(CONF_SEX, default=d.get(CONF_SEX, SEX_MALE)): SelectSelector(
                SelectSelectorConfig(
                    options=[SEX_MALE, SEX_FEMALE],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="sex",
                )
            ),
            vol.Required(CONF_BIRTH_DATE, default=d.get(CONF_BIRTH_DATE, "")): TextSelector(),
            vol.Required(CONF_HEIGHT, default=d.get(CONF_HEIGHT, 175)): NumberSelector(
                NumberSelectorConfig(
                    min=120, max=230, step=1, unit_of_measurement="cm",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(
                CONF_INITIAL_WEIGHT, default=d.get(CONF_INITIAL_WEIGHT, 70)
            ): NumberSelector(
                NumberSelectorConfig(
                    min=20, max=250, step=0.1, unit_of_measurement="kg",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


def _validate_profile(
    user_input: Mapping[str, Any], existing_names: list[str]
) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    name = str(user_input.get(CONF_NAME, "")).strip()
    if not name:
        errors[CONF_NAME] = "name_required"
    elif name.casefold() in (n.casefold() for n in existing_names):
        errors[CONF_NAME] = "name_exists"
    born = parse_birth_date(user_input.get(CONF_BIRTH_DATE))
    if born is None:
        errors[CONF_BIRTH_DATE] = "invalid_birth_date"
    profile = {
        CONF_NAME: name,
        CONF_SEX: user_input.get(CONF_SEX, SEX_MALE),
        CONF_BIRTH_DATE: born.isoformat() if born else None,
        CONF_HEIGHT: float(user_input.get(CONF_HEIGHT, 175)),
        CONF_INITIAL_WEIGHT: float(user_input.get(CONF_INITIAL_WEIGHT, 70)),
    }
    return profile, errors


class HuaweiScaleProConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up a Huawei Scale 3 Pro (or another Hagrid-family scale)."""

    VERSION = 1

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str = "Huawei Scale 3 Pro"
        self._model_hint: str = "Huawei Scale 3 Pro"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HuaweiScaleProOptionsFlow()

    # -------------------------------------------------------------- discovery

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        profile = identify_hagrid(
            discovery_info.name,
            discovery_info.manufacturer_data,
            {str(k): v for k, v in discovery_info.service_data.items()},
        )
        if profile is None:
            return self.async_abort(reason="not_supported")
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        self._address = discovery_info.address.upper()
        self._name = discovery_info.name or profile.display_name
        self._model_hint = profile.display_name
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return await self.async_step_profile()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._name,
                "address": self._address or "",
                "model": self._model_hint,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        configured = self._async_current_ids()
        candidates: dict[str, str] = {}
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address.upper() in configured:
                continue
            profile = identify_hagrid(
                info.name,
                info.manufacturer_data,
                {str(k): v for k, v in info.service_data.items()},
            )
            if profile is not None:
                candidates[info.address.upper()] = (
                    f"{info.name or profile.display_name} — {profile.display_name} "
                    f"({info.address})"
                )

        if user_input is not None:
            address = _normalise_mac(user_input[CONF_ADDRESS])
            if address is None:
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                self._address = address
                return await self.async_step_profile()

        if candidates:
            schema = vol.Schema({vol.Required(CONF_ADDRESS): vol.In(candidates)})
        else:
            # The scale only advertises while someone stands on it, so it is
            # often not visible: allow typing the address.
            schema = vol.Schema({vol.Required(CONF_ADDRESS): TextSelector()})
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"recognised": str(len(candidates))},
        )

    # ---------------------------------------------------------------- profile

    async def async_step_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            profile, errors = _validate_profile(user_input, [])
            if not errors:
                assert self._address is not None
                short = self._address.replace(":", "")[-4:]
                return self.async_create_entry(
                    title=f"{self._model_hint} {short}",
                    data={CONF_ADDRESS: self._address},
                    options={
                        CONF_PROFILES: [profile],
                        CONF_READ_HISTORY: DEFAULT_READ_HISTORY,
                        CONF_COOLDOWN: DEFAULT_COOLDOWN,
                        CONF_CAPTURE: DEFAULT_CAPTURE,
                        CONF_MODEL: DEFAULT_MODEL,
                        CONF_GENDER_ENCODING: DEFAULT_GENDER_ENCODING,
                        CONF_IMPEDANCE_UNITS: DEFAULT_IMPEDANCE_UNITS,
                        CONF_PREFER_SCALE_BODY_FAT: DEFAULT_PREFER_SCALE_BODY_FAT,
                    },
                )
        return self.async_show_form(
            step_id="profile",
            data_schema=_profile_schema(user_input),
            errors=errors,
        )

    # ----------------------------------------------------------------- reauth

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            keys, errors = _validate_auth_key(user_input)
            if not errors:
                if hasattr(entry, "runtime_data") and entry.runtime_data:
                    entry.runtime_data.reset_auth_state()
                # Blank means "back to the built-in keys": drop any override.
                data = {k: v for k, v in entry.data.items() if k not in _KEY_FIELDS}
                return self.async_update_reload_and_abort(entry, data={**data, **keys})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_auth_key_schema(),
            errors=errors,
            description_placeholders={"name": entry.title},
        )


class HuaweiScaleProOptionsFlow(OptionsFlowWithReload):
    """Manage people, model handling and behaviour."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        menu = ["settings", "add_profile", "guest"]
        if len(self.config_entry.options.get(CONF_PROFILES, [])) > 1:
            menu.append("remove_profile")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(
                data={
                    **options,
                    CONF_READ_HISTORY: user_input[CONF_READ_HISTORY],
                    CONF_COOLDOWN: int(user_input[CONF_COOLDOWN]),
                    CONF_CAPTURE: user_input[CONF_CAPTURE],
                    CONF_MODEL: user_input[CONF_MODEL],
                    CONF_GENDER_ENCODING: user_input[CONF_GENDER_ENCODING],
                    CONF_IMPEDANCE_UNITS: user_input[CONF_IMPEDANCE_UNITS],
                    CONF_PREFER_SCALE_BODY_FAT: user_input[CONF_PREFER_SCALE_BODY_FAT],
                    CONF_USER_MATCH: user_input[CONF_USER_MATCH],
                    CONF_USER_MATCH_TOLERANCE: float(
                        user_input[CONF_USER_MATCH_TOLERANCE]
                    ),
                }
            )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_READ_HISTORY,
                    default=options.get(CONF_READ_HISTORY, DEFAULT_READ_HISTORY),
                ): BooleanSelector(),
                vol.Required(
                    CONF_COOLDOWN, default=options.get(CONF_COOLDOWN, DEFAULT_COOLDOWN)
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_COOLDOWN, max=MAX_COOLDOWN, step=5,
                        unit_of_measurement="s", mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_CAPTURE, default=options.get(CONF_CAPTURE, DEFAULT_CAPTURE)
                ): BooleanSelector(),
                vol.Required(
                    CONF_MODEL, default=options.get(CONF_MODEL, DEFAULT_MODEL)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=MODEL_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="model",
                    )
                ),
                vol.Required(
                    CONF_GENDER_ENCODING,
                    default=options.get(CONF_GENDER_ENCODING, DEFAULT_GENDER_ENCODING),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=GENDER_ENCODING_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="gender_encoding",
                    )
                ),
                vol.Required(
                    CONF_IMPEDANCE_UNITS,
                    default=options.get(CONF_IMPEDANCE_UNITS, DEFAULT_IMPEDANCE_UNITS),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=IMPEDANCE_UNIT_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="impedance_units",
                    )
                ),
                vol.Required(
                    CONF_PREFER_SCALE_BODY_FAT,
                    default=options.get(
                        CONF_PREFER_SCALE_BODY_FAT, DEFAULT_PREFER_SCALE_BODY_FAT
                    ),
                ): BooleanSelector(),
                vol.Required(
                    CONF_USER_MATCH, default=options.get(CONF_USER_MATCH, DEFAULT_USER_MATCH)
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=USER_MATCH_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="user_match",
                    )
                ),
                vol.Required(
                    CONF_USER_MATCH_TOLERANCE,
                    default=options.get(
                        CONF_USER_MATCH_TOLERANCE, DEFAULT_USER_MATCH_TOLERANCE
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_USER_MATCH_TOLERANCE,
                        max=MAX_USER_MATCH_TOLERANCE,
                        step=0.5,
                        unit_of_measurement="kg",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        coordinator = getattr(self.config_entry, "runtime_data", None)
        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            description_placeholders={
                "model": coordinator.model.display_name if coordinator else "unknown",
                "source": coordinator.model_source if coordinator else "unknown",
            },
        )

    async def async_step_add_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        profiles = list(self.config_entry.options.get(CONF_PROFILES, []))
        if user_input is not None:
            profile, errors = _validate_profile(
                user_input, [p[CONF_NAME] for p in profiles]
            )
            if not errors:
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_PROFILES: [*profiles, profile]}
                )
        return self.async_show_form(
            step_id="add_profile", data_schema=_profile_schema(user_input), errors=errors
        )

    async def async_step_remove_profile(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        profiles = list(self.config_entry.options.get(CONF_PROFILES, []))
        if user_input is not None:
            remaining = [p for p in profiles if p[CONF_NAME] != user_input[CONF_NAME]]
            if remaining:  # never remove the last person
                return self.async_create_entry(
                    data={**self.config_entry.options, CONF_PROFILES: remaining}
                )
        return self.async_show_form(
            step_id="remove_profile",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): SelectSelector(
                        SelectSelectorConfig(
                            options=[p[CONF_NAME] for p in profiles],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_guest(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What the scale may assume about a visitor before it measures.

        Nothing here is required: with both fields blank the height is guessed from
        the guest's weight and the age from the resulting BMI, which is enough for
        the scale to produce a composition. Everything typed here wins.
        """
        options = self.config_entry.options
        if user_input is not None:
            height = user_input.get("guest_height_cm")
            age = user_input.get("guest_age")
            sex = user_input.get("guest_sex")
            guest = {
                key: value
                for key, value in (
                    ("height_cm", height),
                    ("age", age),
                    ("male", None if sex in (None, "") else sex == "male"),
                )
                if value not in (None, "")
            }
            return self.async_create_entry(
                data={
                    **options,
                    CONF_GUEST_AFTERMATH: user_input.get(
                        CONF_GUEST_AFTERMATH, DEFAULT_GUEST_AFTERMATH
                    ),
                    CONF_GUEST_PROFILE: guest,
                }
            )

        guest = options.get(CONF_GUEST_PROFILE)
        guest = guest if isinstance(guest, dict) else {}
        sex = guest.get("male")
        sex = "male" if sex is True else "female" if sex is False else None

        def suggested(value: Any) -> dict[str, Any]:
            """Prefill a field without making it required (it may stay empty)."""
            return {"suggested_value": value} if value is not None else {}

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_GUEST_AFTERMATH,
                    default=options.get(CONF_GUEST_AFTERMATH, DEFAULT_GUEST_AFTERMATH),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=GUEST_AFTERMATH_OPTIONS,
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="guest_aftermath",
                    )
                ),
                vol.Optional(
                    "guest_height_cm", description=suggested(guest.get("height_cm"))
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=120, max=230, step=1, unit_of_measurement="см",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional(
                    "guest_age", description=suggested(guest.get("age"))
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=5, max=110, step=1, unit_of_measurement="лет",
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Optional("guest_sex", description=suggested(sex)): SelectSelector(
                    SelectSelectorConfig(
                        options=["male", "female"],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="guest_sex",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="guest", data_schema=schema)
