"""Profiles and readings. No Home Assistant imports (except nothing at all)."""
from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime
from typing import Any

from . import body_composition
from .const import (
    GUEST_DEFAULT_AGE,
    GUEST_DEFAULT_HEIGHT_CM,
    GUEST_DEFAULT_MALE,
    GUEST_NAME,
    GUEST_STATUSES,
    GUEST_STATUS_AUTO,
)
from .protocol import IMPEDANCE_MODE_AUTO, WeightMeasurement


@dataclass(frozen=True)
class Profile:
    """A person who weighs themselves on the scale.

    ``status`` is only used by the stored **guest** profile (a name, a date of
    birth guessed from the reading, and one of ``GUEST_STATUSES``): a guest is a
    profile-shaped record so the protocol code needs no special case, but it is
    never part of the household list and nothing is ever filed under it.
    """

    name: str
    male: bool
    birth_date: date
    height_cm: float
    initial_weight_kg: float = 70.0
    status: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Profile":
        return cls(
            name=raw["name"],
            male=raw["sex"] == "male",
            birth_date=date.fromisoformat(raw["birth_date"]),
            height_cm=float(raw["height_cm"]),
            initial_weight_kg=float(raw.get("initial_weight_kg") or 70.0),
            status=raw.get("status") or None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sex": "male" if self.male else "female",
            "birth_date": self.birth_date.isoformat(),
            "height_cm": self.height_cm,
            "initial_weight_kg": self.initial_weight_kg,
            "status": self.status,
        }

    @property
    def is_guest(self) -> bool:
        """Whether this is the guest record rather than a household person."""
        return self.status in GUEST_STATUSES

    @property
    def user_id(self) -> int:
        """Stable positive 31-bit id sent to the scale as ``u:XXXXXXXX``."""
        return zlib.crc32(self.name.encode("utf-8")) & 0x7FFFFFFF

    def age_on(self, today: date) -> int:
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years


@dataclass(frozen=True)
class ScaleReading:
    """One processed measurement, as exposed to Home Assistant."""

    timestamp: datetime  # timezone-aware
    profile: str
    weight_kg: float
    history: bool = False
    #: "model" = computed from impedance, "scale" = fat % as reported by the
    #: scale only, "none" = weight only.
    composition: str = "none"
    #: Which characteristic the packet came from ("hagrid", "standard_weight",
    #: "standard_body") — handy when a firmware answers somewhere unexpected.
    source: str = "hagrid"
    heart_rate: int | None = None
    impedance_ohm: float | None = None
    impedance_high_ohm: float | None = None
    bmi: float | None = None
    body_fat_percent: float | None = None
    water_percent: float | None = None
    skeletal_muscle_percent: float | None = None
    bone_mass_kg: float | None = None
    lean_body_mass_kg: float | None = None
    protein_percent: float | None = None
    bmr_kcal: float | None = None
    visceral_fat_level: float | None = None
    #: Masses in kg (the app reports water and protein both in % and in kg).
    water_kg: float | None = None
    protein_kg: float | None = None
    skeletal_muscle_kg: float | None = None
    #: Appendicular skeletal muscle mass / height², kg/m² (the app's "скелетные
    #: мышцы, кг/м²").
    muscle_index_kg_m2: float | None = None
    #: Estimated biological ("metabolic") age and physique category.
    body_age_years: int | None = None
    physique: str | None = None
    #: How this reading was attributed to ``profile``: "manual" (the profile
    #: selected for the session) or "weight" (auto-matched after weighing).
    assigned_by: str = "manual"
    match_reference_kg: float | None = None
    match_distance_kg: float | None = None
    match_basis: str | None = None
    match_ambiguous: tuple[str, ...] = ()
    #: Raw impedance words as the scale sent them (no unit conversion), so a
    #: firmware that puts them somewhere else is immediately visible.
    impedance_raw_low: list[int] = field(default_factory=list)
    impedance_raw_high: list[int] = field(default_factory=list)
    #: Which impedance the composition model was fed, in ohm.
    model_impedance_ohm: float | None = None
    #: Per-segment impedances in ohm (8-electrode Pro: the words after the
    #: whole-body one). Order is not confirmed by the vendor, so they are
    #: exposed positionally.
    impedance_segments_low: list[float] = field(default_factory=list)
    impedance_segments_high: list[float] = field(default_factory=list)
    #: Where body_fat_percent came from: "model" or "scale".
    body_fat_source: str | None = None
    #: Length and hex of the payload the reading came from.
    payload_length: int | None = None
    payload_hex: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScaleReading":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}
        values["timestamp"] = datetime.fromisoformat(values["timestamp"])
        return cls(**values)


def build_reading(
    measurement: WeightMeasurement,
    profile: Profile,
    *,
    timestamp: datetime,
    today: date,
    history: bool = False,
    impedance_units: str = IMPEDANCE_MODE_AUTO,
    prefer_scale_body_fat: bool = False,
) -> ScaleReading:
    """Turn a raw scale measurement into a reading for ``profile``.

    ``prefer_scale_body_fat`` selects the composition engine: ``True`` (the
    default in the options flow) reproduces the Huawei Health app — the scale's
    own fat percentage plus the app's fixed coefficients for water, protein,
    mineral, muscle, BMR and the derived indices; ``False`` computes everything
    from the low-frequency impedance with the openScale regression. Either way
    the reading is internally consistent: the compartments add up to the weight.
    """
    weight = measurement.weight_kg
    height_m = profile.height_cm / 100.0
    bmi = weight / (height_m * height_m) if height_m > 0 else None

    impedance = measurement.impedance_ohm(impedance_units)
    impedance_high = measurement.impedance_high_ohm(impedance_units)
    # The four-electrode model wants a whole-body low-frequency value, but a
    # firmware that only sends the high-frequency block is still better used
    # than ignored.
    model_impedance = impedance or impedance_high

    common = dict(
        timestamp=timestamp,
        profile=profile.name,
        weight_kg=weight,
        history=history,
        source=measurement.source,
        heart_rate=measurement.heart_rate,
        impedance_ohm=impedance,
        impedance_high_ohm=impedance_high,
        bmi=bmi,
        impedance_raw_low=list(measurement.low_impedance),
        impedance_raw_high=list(measurement.high_impedance),
        impedance_segments_low=[
            ohm for ohm in measurement.low_ohm_all(impedance_units)[1:] if ohm
        ],
        impedance_segments_high=[
            ohm for ohm in measurement.high_ohm_all(impedance_units)[1:] if ohm
        ],
        model_impedance_ohm=model_impedance,
        payload_length=measurement.raw_length,
        payload_hex=measurement.raw_payload.hex().upper() or None,
    )

    scale_fat = measurement.fat_percent if 0.0 < measurement.fat_percent < 80.0 else None
    age_years = profile.age_on(today)

    # Two ways to get composition:
    #  * "app": the way Huawei Health itself reports it — the scale's own fat
    #    percentage, fat-free mass split with fixed coefficients, BMR by
    #    Katch-McArdle (see body_composition.from_fat). This reproduces the app
    #    to a few tens of grams, so it is the default.
    #  * "model": the openScale four-electrode regression on the impedance,
    #    used when the scale did not report a body-fat percentage.
    imp_model = (
        body_composition.calculate(
            height_cm=profile.height_cm,
            weight_kg=weight,
            age_years=age_years,
            male=profile.male,
            impedance_ohm=model_impedance,
        )
        if model_impedance
        else None
    )
    app_model = (
        body_composition.from_fat(weight, scale_fat, profile.height_cm)
        if (scale_fat is not None and prefer_scale_body_fat)
        else None
    )

    # Visceral fat only exists in the impedance regression; it is what the app
    # shows too, so take it from there whenever the impedance is available.
    visceral = imp_model.visceral_fat_level if imp_model else None

    if app_model is not None:
        bmr = app_model.bmr_kcal
        return ScaleReading(
            composition="app",
            body_fat_percent=app_model.body_fat_percent,
            body_fat_source="scale",
            water_percent=app_model.water_percent,
            water_kg=app_model.water_kg,
            protein_percent=app_model.protein_percent,
            protein_kg=app_model.protein_kg,
            skeletal_muscle_percent=app_model.skeletal_muscle_percent,
            skeletal_muscle_kg=app_model.skeletal_muscle_kg,
            bone_mass_kg=app_model.mineral_kg,
            lean_body_mass_kg=app_model.lean_body_mass_kg,
            bmr_kcal=bmr,
            visceral_fat_level=visceral,
            muscle_index_kg_m2=app_model.muscle_index_kg_m2,
            body_age_years=body_composition.metabolic_age(
                weight, profile.height_cm, profile.male, bmr
            ),
            physique=body_composition.physique_category(
                bmi or 0.0, app_model.body_fat_percent, profile.male
            ),
            **common,
        )

    if imp_model is not None:
        bmr = imp_model.bmr_kcal
        return ScaleReading(
            composition="model",
            body_fat_percent=imp_model.body_fat_percent,
            body_fat_source="model",
            water_percent=imp_model.water_percent,
            water_kg=imp_model.water_percent * weight / 100.0,
            protein_percent=imp_model.protein_percent,
            protein_kg=imp_model.protein_percent * weight / 100.0,
            skeletal_muscle_percent=imp_model.skeletal_muscle_percent,
            skeletal_muscle_kg=imp_model.skeletal_muscle_kg,
            bone_mass_kg=imp_model.bone_mineral_kg,
            lean_body_mass_kg=imp_model.lean_body_mass_kg,
            bmr_kcal=bmr,
            visceral_fat_level=imp_model.visceral_fat_level,
            muscle_index_kg_m2=(
                body_composition.APPENDICULAR_OF_MUSCLE
                * imp_model.skeletal_muscle_kg
                / (profile.height_cm / 100.0) ** 2
                if profile.height_cm
                else None
            ),
            body_age_years=body_composition.metabolic_age(
                weight, profile.height_cm, profile.male, bmr
            ),
            physique=body_composition.physique_category(
                bmi or 0.0, imp_model.body_fat_percent, profile.male
            ),
            **common,
        )

    # No impedance-derived model (child profile, implausible input): fall back to
    # the scale's own body-fat figure when it looks sane.
    if scale_fat is not None:
        return ScaleReading(
            composition="scale", body_fat_percent=scale_fat, body_fat_source="scale", **common
        )
    return ScaleReading(composition="none", **common)


# --------------------------------------------------------------- guest profile
#
# A guest is the one person the household has no profile for, and the scale wants
# height, age and sex *before* it measures — otherwise it only reports a weight.
# So guest mode builds a throwaway profile: what the household typed in the
# options, or, when it typed nothing, figures guessed from the guest's own first
# weigh-in. That profile is sent to the scale only, and everything built from the
# reading is labelled as a guest reading. Nothing is ever filed under it.


def guest_age_from_weight(weight_kg: float, height_cm: float) -> int:
    """Plausible age for a height/weight pair — a crude adult BMI lookup.

    Only used when neither the options nor an earlier guest weigh-in know
    anything: enough to let the scale produce a composition, nothing more.
    """
    if weight_kg <= 0 or height_cm <= 0:
        return GUEST_DEFAULT_AGE
    bmi = weight_kg / (height_cm / 100.0) ** 2
    if bmi < 18.5:
        return 25
    if bmi < 25.0:
        return 35
    if bmi < 30.0:
        return 45
    if bmi < 35.0:
        return 55
    return 65


def guess_guest_height(weight_kg: float) -> float:
    """Height that gives an average-built guest of this weight a normal BMI.

    The scale computes its own body-fat percentage with the profile it is given,
    and nobody knows a visitor's height in advance, so the guess aims at an adult
    BMI in the normal range rather than at accuracy.
    """
    if weight_kg <= 0:
        return GUEST_DEFAULT_HEIGHT_CM
    height_cm = (weight_kg / 23.0) ** 0.5 * 100.0  # 23 = middle of normal BMI
    return float(round(min(215.0, max(140.0, height_cm))))


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _stored_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def build_guest_profile(
    guest: dict[str, Any] | None,
    measurement: WeightMeasurement | None,
    *,
    today: date,
    status: str = GUEST_STATUS_AUTO,
) -> Profile:
    """The profile a guest weigh-in is measured with.

    ``guest`` is the guest record stored from earlier guest weigh-ins (only its
    guessed height and age are reused, so consecutive guest weigh-ins remain
    comparable); ``measurement`` is the reading itself, if there is one yet.

    The household does not have to type anything: the height is guessed from the
    weight, the age from the resulting BMI. Anything typed in the options wins.
    """
    stored = guest if isinstance(guest, dict) else {}
    weight = measurement.weight_kg if measurement is not None else 0.0

    height = _positive_float(stored.get("height_cm")) or (
        guess_guest_height(weight) if weight > 0 else GUEST_DEFAULT_HEIGHT_CM
    )

    age = _positive_float(stored.get("age"))
    if age is not None and 5 <= age <= 110:
        # A typed age beats any date stored earlier (that date was a guess).
        birth_date: date | None = date(max(1900, today.year - int(age)), 1, 1)
    else:
        birth_date = _stored_date(stored.get("birth_date"))
    if birth_date is None:
        years = guest_age_from_weight(weight, height)
        birth_date = date(max(1900, today.year - years), 1, 1)

    return Profile(
        name=GUEST_NAME,
        male=bool(stored.get("male", GUEST_DEFAULT_MALE)),
        birth_date=birth_date,
        height_cm=float(height),
        initial_weight_kg=weight or 70.0,
        status=status,
    )


def relabel_reading(reading: ScaleReading, *, guest: bool, assigned_by: str) -> ScaleReading:
    """Mark a reading as a guest's: its own name, and why it was filed that way.

    The composition itself is kept — a guest's weight, fat share and impedance are
    worth seeing, they just belong to nobody. ``assigned_by`` records how the
    guest came about (``guest_mode``: the select said so; ``guest_auto``: the
    weight fitted nobody).
    """
    if not guest:
        return reading
    return replace(reading, profile=GUEST_NAME, assigned_by=assigned_by)
