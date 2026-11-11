"""Reference vectors from openScale's HuaweiScale3BodyCompositionTest."""
from __future__ import annotations

import pytest

from custom_components.huawei_scale_pro.body_composition import calculate


def _check(h, w, age, male, z, fat, water, smm_kg, bone, bmr, visceral):
    r = calculate(h, w, age, male, z)
    assert r is not None
    assert r.body_fat_percent == pytest.approx(fat, abs=0.01)
    assert r.water_percent == pytest.approx(water, abs=0.01)
    assert r.skeletal_muscle_kg == pytest.approx(smm_kg, abs=0.01)
    assert r.bone_mineral_kg == pytest.approx(bone, abs=0.01)
    assert r.bmr_kcal == pytest.approx(bmr, abs=1)
    assert r.visceral_fat_level == visceral
    assert r.skeletal_muscle_percent == pytest.approx(smm_kg / w * 100, abs=0.05)


def test_male_vectors():
    _check(178, 91.45, 46, True, 362.5, 25.341705, 54.293285, 37.3210, 3.51146, 1943, 14)
    _check(178, 91.50, 46, True, 370.5, 25.73119, 53.47523, 37.13919, 3.50382, 1931, 14)


def test_female_vector():
    _check(165, 65, 40, False, 400.0, 30.9025, 55.7124, 23.6117, 2.7600, 1340, 5)


def test_rejects_children_and_malformed_inputs():
    assert calculate(150, 45, 17, True, 450.0) is None
    assert calculate(178, 91, 46, True, 0.0) is None
    assert calculate(50, 91, 46, True, 360.0) is None
