"""Regression tests using synthetic inputs, not personal measurements."""
from datetime import UTC, date, datetime
import pytest
from custom_components.huawei_scale_pro import body_composition as bc
from custom_components.huawei_scale_pro import protocol as p
from custom_components.huawei_scale_pro.models import Profile, build_reading
from .fake_scale import realtime_payload

PROFILE = Profile("Example", True, date(1980, 1, 1), 180.0, 80.0)
NOW = datetime(2026, 8, 9, 23, 39, tzinfo=UTC)

def test_fat_split():
    c = bc.from_fat(80, 25, 180)
    assert c is not None
    assert c.lean_body_mass_kg == pytest.approx(60)
    assert c.water_kg == pytest.approx(60 * bc.WATER_OF_FFM)
    assert c.protein_kg == pytest.approx(60 * bc.PROTEIN_OF_FFM)
    assert c.mineral_kg == pytest.approx(60 * bc.MINERAL_OF_FFM)
    assert c.skeletal_muscle_kg == pytest.approx(60 * bc.SKELETAL_MUSCLE_OF_FFM)
    assert c.bmr_kcal == pytest.approx(370 + 21.6 * 60)

def test_split_rejects_implausible_input():
    assert bc.from_fat(80, 0, 180) is None
    assert bc.from_fat(80, 90, 180) is None
    assert bc.from_fat(80, 20, 50) is None

def test_metabolic_age_and_physique():
    assert bc.metabolic_age(80, 180, True, 0) is None
    assert bc.physique_category(31.0, 18.0, True) == bc.PHYSIQUE_OBESE
    assert bc.physique_category(23.0, 10.0, True) == bc.PHYSIQUE_ATHLETIC
    assert bc.physique_category(17.5, 15.0, True) == bc.PHYSIQUE_SLIM
    assert bc.physique_category(26.5, 18.0, True) == bc.PHYSIQUE_OVERWEIGHT

def test_reading_uses_scale_fat():
    m = p.parse_realtime_measurement(realtime_payload(weight_kg=80, fat=25, high_impedance_raw=(3900,4100,4200,4300,4400,4500)))
    assert m is not None
    r = build_reading(m, PROFILE, timestamp=NOW, today=date(2026, 8, 9), prefer_scale_body_fat=True)
    assert r.composition == "app"
    assert r.body_fat_source == "scale"
    assert r.weight_kg == pytest.approx(80)
    assert r.body_fat_percent == pytest.approx(25)
    assert r.lean_body_mass_kg == pytest.approx(60)
