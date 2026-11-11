"""Four-electrode body-composition model (Huawei Health, adult branch).

Port of ``HuaweiScale3BodyComposition.kt`` from openScale (GPL-3.0). The
coefficients were fitted by the openScale contributors to the input/output
behaviour of the Huawei Health app for the adult four-electrode branch.
Children/teenagers (< 18) are not supported, as in the original.

Scope note for the Scale 3 Pro: Huawei's own TruFit 2.0 model is what the Pro
uses on-device with its 8 electrodes and two frequencies, and it is closed. This
model is the four-electrode adult branch fitted to Huawei Health's behaviour, so
for a Scale 3 Pro the numbers it produces are an *estimate* derived from the
low-frequency whole-body impedance — not the figures the Huawei Health app
shows. They track weight and impedance consistently, which is what matters for
trends, but they will not match the app to the decimal.

Pure Python, no Home Assistant imports.
"""
from __future__ import annotations

from dataclasses import dataclass

# Feature order: [h^2 / z, z, weight, height, age^2, age, 1]


@dataclass(frozen=True)
class _Coefficients:
    water_kg: tuple[float, ...]
    fat_free_mass_kg: tuple[float, ...]
    muscle_mass_kg: tuple[float, ...]
    bone_mineral_kg: tuple[float, ...]
    bmr: tuple[float, ...]
    visceral_fat: tuple[float, ...]


_FEMALE = _Coefficients(
    water_kg=(0.445999704, 0.0139999486, 0.167000103, -0.102999643,
              -0.00186499864, 0.172699790, 2.47318013),
    fat_free_mass_kg=(-0.0000000591396, -0.0150000073, 0.198000056, 0.313999865,
                      -0.00148397395, 0.124597440, -16.3761132),
    muscle_mass_kg=(-0.0000000596289, -0.0130000008, 0.180999914, 0.286000079,
                    -0.00136797854, 0.115597450, -14.1328394),
    bone_mineral_kg=(-0.00400158350, -0.00200033477, 0.0160000363, 0.0260015858,
                     -0.000119001796, 0.00930013136, -1.67940940),
    bmr=(1.75784170, -0.181837584, 9.65817197, 5.89515491,
         0.00782921597, -2.89574283, -203.303636),
    visceral_fat=(-0.00158317035, 0.00544003197, 0.314994178, -0.103662636,
                  0.000967108734, 0.00527136893, -1.33727631),
)

_MALE = _Coefficients(
    water_kg=(0.446000315, 0.0140000556, 0.166999963, -0.103000253,
              -0.00104299708, 0.0356998392, 9.22059951),
    fat_free_mass_kg=(0.114000146, -0.0149999678, 0.322000075, 0.272999903,
                      -0.000979001428, 0.0170001265, -13.0029150),
    muscle_mass_kg=(0.107999947, -0.0140000275, 0.305999991, 0.258999945,
                    -0.000929995268, 0.0160995340, -12.5056628),
    bone_mineral_kg=(-0.00400070678, -0.00200017483, 0.0160001373, 0.0260008239,
                     -0.0000579943116, 0.00269952368, -1.50661300),
    bmr=(6.43629949, -0.0002299374, 10.9972143, 4.74191847,
         0.0238549107, -6.20381101, -233.995162),
    visceral_fat=(-0.168283428, -0.0119657591, 0.377569276, -0.138136532,
                  0.000252403405, 0.134562343, 17.0432610),
)


@dataclass(frozen=True)
class BodyComposition:
    impedance_ohm: float
    body_fat_percent: float
    water_percent: float
    skeletal_muscle_kg: float
    skeletal_muscle_percent: float
    bone_mineral_kg: float
    lean_body_mass_kg: float
    protein_percent: float
    bmr_kcal: float
    visceral_fat_level: float


def _evaluate(coefficients: tuple[float, ...], features: list[float]) -> float:
    return sum(c * f for c, f in zip(coefficients, features, strict=True))


def calculate(
    height_cm: float,
    weight_kg: float,
    age_years: int,
    male: bool,
    impedance_ohm: float,
) -> BodyComposition | None:
    """Compute body composition, or ``None`` if inputs are unsupported/implausible."""
    if height_cm <= 0 or weight_kg <= 0 or impedance_ohm <= 0:
        return None
    if age_years < 18:  # Huawei uses a separate child/teenager path
        return None

    h, w, age, z = float(height_cm), float(weight_kg), float(age_years), impedance_ohm
    if not (120.0 <= h <= 230.0 and 20.0 <= w <= 250.0 and 100.0 <= z <= 1000.0):
        return None

    x = [h * h / z, z, w, h, age * age, age, 1.0]
    c = _MALE if male else _FEMALE

    water_kg = _evaluate(c.water_kg, x)
    ffm_kg = _evaluate(c.fat_free_mass_kg, x)
    muscle_mass_kg = _evaluate(c.muscle_mass_kg, x)
    bone_kg = _evaluate(c.bone_mineral_kg, x)

    skeletal_muscle_kg = 0.605 * muscle_mass_kg - 1.833
    fat_percent = (1.0 - ffm_kg / w) * 100.0
    water_percent = water_kg / w * 100.0
    skeletal_muscle_percent = skeletal_muscle_kg / w * 100.0
    # Protein mass = FFM - total body water - bone mineral (matches Huawei Health).
    protein_percent = (ffm_kg - water_kg - bone_kg) / w * 100.0

    # The native four-electrode path stores these as positive integers
    # (truncated toward zero, as the original ``Double.toInt()``).
    bmr = int(_evaluate(c.bmr, x))
    visceral = max(1, int(_evaluate(c.visceral_fat, x)))

    if not (
        0.0 <= fat_percent <= 75.0
        and 10.0 <= water_percent <= 85.0
        and 0.0 <= skeletal_muscle_kg <= w
        and 0.0 <= bone_kg <= 10.0
        and 0.0 <= protein_percent <= 40.0
        and 500 <= bmr <= 5000
        and 1 <= visceral <= 60
    ):
        return None

    return BodyComposition(
        impedance_ohm=z,
        body_fat_percent=fat_percent,
        water_percent=water_percent,
        skeletal_muscle_kg=skeletal_muscle_kg,
        skeletal_muscle_percent=skeletal_muscle_percent,
        bone_mineral_kg=bone_kg,
        lean_body_mass_kg=ffm_kg,
        protein_percent=protein_percent,
        bmr_kcal=float(bmr),
        visceral_fat_level=float(visceral),
    )


# ---------------------------------------------------------------------------
# Composition as Huawei Health itself reports it
# ---------------------------------------------------------------------------
#
# The following coefficients define an approximate fat-based model.
# The results are estimates and may differ from the Huawei Health application.

WATER_OF_FFM = 0.7348
PROTEIN_OF_FFM = 0.2120
MINERAL_OF_FFM = 0.0524
SKELETAL_MUSCLE_OF_FFM = 0.5335
#: Appendicular (arms + legs) share of the total skeletal muscle mass; used for
#: the "skeletal muscle index" the app shows in kg/m².
APPENDICULAR_OF_MUSCLE = 0.758
#: Katch-McArdle, as used by the app: BMR = 370 + 21.6 * FFM.
BMR_INTERCEPT = 370.0
BMR_PER_FFM_KG = 21.6


@dataclass(frozen=True)
class FatBasedComposition:
    """Body composition derived the way the Huawei Health app derives it."""

    body_fat_percent: float
    lean_body_mass_kg: float
    water_kg: float
    water_percent: float
    protein_kg: float
    protein_percent: float
    mineral_kg: float
    skeletal_muscle_kg: float
    skeletal_muscle_percent: float
    bmr_kcal: float
    muscle_index_kg_m2: float


def from_fat(
    weight_kg: float, fat_percent: float, height_cm: float
) -> FatBasedComposition | None:
    """Split fat-free mass into water/protein/mineral like the Huawei app.

    ``None`` when the inputs are implausible, in which case the caller can fall
    back to the impedance regression.
    """
    if not (20.0 <= weight_kg <= 250.0 and 120.0 <= height_cm <= 230.0):
        return None
    if not (3.0 <= fat_percent <= 65.0):
        return None

    height_m = height_cm / 100.0
    fat_kg = weight_kg * fat_percent / 100.0
    ffm = weight_kg - fat_kg
    if ffm <= 0:
        return None

    water = WATER_OF_FFM * ffm
    protein = PROTEIN_OF_FFM * ffm
    mineral = MINERAL_OF_FFM * ffm
    muscle = SKELETAL_MUSCLE_OF_FFM * ffm
    appendicular = APPENDICULAR_OF_MUSCLE * muscle
    return FatBasedComposition(
        body_fat_percent=fat_percent,
        lean_body_mass_kg=ffm,
        water_kg=water,
        water_percent=water / weight_kg * 100.0,
        protein_kg=protein,
        protein_percent=protein / weight_kg * 100.0,
        mineral_kg=mineral,
        skeletal_muscle_kg=muscle,
        skeletal_muscle_percent=muscle / weight_kg * 100.0,
        bmr_kcal=BMR_INTERCEPT + BMR_PER_FFM_KG * ffm,
        muscle_index_kg_m2=appendicular / (height_m * height_m) if height_m else 0.0,
    )


def metabolic_age(
    weight_kg: float, height_cm: float, male: bool, bmr_kcal: float
) -> int | None:
    """Estimate a "biological/metabolic age" by inverting Harris-Benedict.

    This is the classic metabolic-age trick: find the age whose predicted BMR
    equals the measured one. It reproduces the figure the Huawei app shows to
    within a year or two, but it is an estimate, not a measurement.
    """
    if bmr_kcal <= 0 or weight_kg <= 0 or height_cm <= 0:
        return None
    if male:
        base = 88.362 + 13.397 * weight_kg + 4.799 * height_cm
        per_year = 5.677
    else:
        base = 447.593 + 9.247 * weight_kg + 3.098 * height_cm
        per_year = 4.330
    age = (base - bmr_kcal) / per_year
    if not 5.0 <= age <= 110.0:
        return None
    return round(age)


#: Physique categories, roughly what the app's "телосложение" field shows.
#: Ordered from leanest to heaviest; the caller translates them.
PHYSIQUE_SLIM = "slim"
PHYSIQUE_ATHLETIC = "athletic"
PHYSIQUE_NORMAL = "normal"
PHYSIQUE_OVERWEIGHT = "overweight"
PHYSIQUE_OBESE = "obese"
PHYSIQUE_OPTIONS = [
    PHYSIQUE_SLIM, PHYSIQUE_ATHLETIC, PHYSIQUE_NORMAL, PHYSIQUE_OVERWEIGHT, PHYSIQUE_OBESE,
]


def physique_category(bmi: float, fat_percent: float, male: bool) -> str | None:
    """Classify the physique from BMI and body fat (approximation of the app)."""
    if bmi <= 0 or fat_percent <= 0:
        return None
    obese_fat = 25.0 if male else 32.0
    overweight_fat = 20.0 if male else 25.0
    athletic_fat = 12.0 if male else 18.0
    if bmi >= 30.0 or fat_percent >= obese_fat:
        return PHYSIQUE_OBESE
    if bmi >= 25.0 or fat_percent >= overweight_fat:
        return PHYSIQUE_OVERWEIGHT
    if fat_percent <= athletic_fat and bmi < 25.0:
        return PHYSIQUE_ATHLETIC
    if bmi < 18.5:
        return PHYSIQUE_SLIM
    return PHYSIQUE_NORMAL
