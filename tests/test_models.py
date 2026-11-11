"""Profile / reading logic (no Home Assistant needed)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.huawei_scale_pro import protocol as p
from custom_components.huawei_scale_pro.models import Profile, ScaleReading, build_reading

from .fake_scale import realtime_payload, standard_weight_payload

PROFILE = Profile("Alex", True, date(1980, 1, 1), 178.0, 80.0)
NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _measure(**kw):
    return p.parse_realtime_measurement(realtime_payload(**kw))


def test_age_handles_birthdays():
    assert PROFILE.age_on(date(2026, 1, 1)) == 46
    assert PROFILE.age_on(date(2025, 12, 31)) == 45


def test_user_id_is_stable_and_positive():
    assert PROFILE.user_id == Profile("Alex", False, date(1990, 5, 5), 160, 50).user_id
    assert 0 <= PROFILE.user_id < 2**31


def test_profile_roundtrip():
    assert Profile.from_dict(PROFILE.to_dict()) == PROFILE


def test_full_composition_when_impedance_present():
    r = build_reading(_measure(), PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.composition == "model"
    assert r.weight_kg == pytest.approx(86.25)
    assert r.impedance_ohm == pytest.approx(362.5)
    assert r.bmi == pytest.approx(86.25 / 1.78**2)
    assert 5 < r.body_fat_percent < 50
    assert r.bmr_kcal > 1000 and r.visceral_fat_level >= 1
    assert r.heart_rate == 61
    assert r.source == "hagrid"


def test_dual_frequency_reading_exposes_both_impedances():
    measurement = _measure(impedance_raw=3520, high_impedance_raw=tuple(range(260, 266)))
    r = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.impedance_ohm == pytest.approx(352.0)
    assert r.impedance_high_ohm == pytest.approx(260.0)
    assert r.composition == "model"


def test_falls_back_to_scale_fat_without_impedance():
    r = build_reading(_measure(impedance_raw=0), PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.composition == "scale"
    assert r.body_fat_percent == pytest.approx(30.1)
    assert r.water_percent is None and r.bmr_kcal is None


def test_minors_get_no_model_composition():
    kid = Profile("Kid", True, date(2015, 1, 1), 150.0, 40.0)
    r = build_reading(_measure(), kid, timestamp=NOW, today=date(2026, 8, 9))
    assert r.composition == "scale"
    assert r.skeletal_muscle_percent is None


def test_weight_only_when_scale_fat_is_junk():
    r = build_reading(
        _measure(impedance_raw=0, fat=0.0), PROFILE, timestamp=NOW, today=date(2026, 8, 9)
    )
    assert r.composition == "none" and r.body_fat_percent is None


def test_app_formulas_and_impedance_model_are_two_engines_for_the_same_fields():
    """Both engines fill the same metrics, from different inputs."""
    measurement = _measure()
    app = build_reading(
        measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9), prefer_scale_body_fat=True
    )
    model = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))

    assert app.composition == "app" and app.body_fat_source == "scale"
    assert model.composition == "model" and model.body_fat_source == "model"
    assert app.body_fat_percent == pytest.approx(30.1)  # the scale's own figure
    for field in ("water_percent", "skeletal_muscle_percent", "bone_mass_kg",
                  "lean_body_mass_kg", "protein_percent", "bmr_kcal", "visceral_fat_level",
                  "muscle_index_kg_m2", "body_age_years", "physique"):
        assert getattr(app, field) is not None, field
        assert getattr(model, field) is not None, field


def test_impedance_unit_override_changes_the_model_input():
    """'ohms' mode reads a 0.1-ohm word as ohm, so the model rejects it."""
    measurement = _measure(impedance_raw=3625)
    auto = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    as_ohms = build_reading(
        measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9),
        impedance_units=p.IMPEDANCE_MODE_OHMS,
    )
    assert auto.composition == "model"
    assert auto.impedance_ohm == pytest.approx(362.5)
    assert as_ohms.impedance_ohm == pytest.approx(3625.0)
    assert as_ohms.composition == "scale"  # implausible impedance -> no model


def test_standard_weight_reading_has_no_composition():
    measurement = p.parse_standard_weight_measurement(standard_weight_payload(86.25))
    r = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.source == "standard_weight"
    assert r.composition == "none"
    assert r.weight_kg == pytest.approx(86.25, abs=0.01)
    assert r.bmi is not None


def test_reading_serialises():
    r = build_reading(_measure(), PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert ScaleReading.from_dict(r.to_dict()) == r


def test_raw_impedance_words_and_payload_are_kept_for_diagnostics():
    """A firmware that puts impedance elsewhere must be visible, not silent."""
    measurement = _measure(impedance_raw=3520, high_impedance_raw=(261, 0, 0, 0, 0, 0))
    r = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.payload_length == 38
    assert r.payload_hex == measurement.raw_payload.hex().upper()
    assert r.impedance_raw_low[0] == 3520
    assert r.impedance_raw_high[0] == 261
    assert r.model_impedance_ohm == pytest.approx(352.0)


def test_high_frequency_is_used_when_the_low_band_is_empty():
    measurement = _measure(impedance_raw=0, high_impedance_raw=(3520, 0, 0, 0, 0, 0))
    r = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.impedance_ohm is None
    assert r.impedance_high_ohm == pytest.approx(352.0)
    assert r.model_impedance_ohm == pytest.approx(352.0)
    assert r.composition == "model"


def test_segmental_impedances_are_exposed():
    """Synthetic whole-body and segmental impedance values in two bands."""
    payload = bytearray(realtime_payload(weight_kg=86.25, impedance_raw=3625,
        high_impedance_raw=(3900, 4100, 4200, 4300, 4400, 4500)))
    for index, raw in enumerate((3625, 4700, 4800, 4900, 5000, 5100)):
        payload[12 + 2 * index:14 + 2 * index] = raw.to_bytes(2, "little")
    measurement = p.parse_realtime_measurement(bytes(payload))
    assert measurement is not None
    r = build_reading(measurement, PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.impedance_ohm == pytest.approx(362.5)
    assert r.impedance_high_ohm == pytest.approx(390.0)
    assert list(r.impedance_segments_low) == pytest.approx([470, 480, 490, 500, 510])
    assert list(r.impedance_segments_high) == pytest.approx([410, 420, 430, 440, 450])
