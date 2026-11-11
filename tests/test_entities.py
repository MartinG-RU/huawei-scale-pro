"""Static checks on the entity descriptions.

``sensor.py`` imports Home Assistant, which is not installed in this test
environment, so these checks read the source and the translations directly.
They guard the things a user notices first: every advertised sensor has a
readable name in both languages, an icon, and a ``value_fn`` that points at a
field which really exists on ScaleReading.
"""
from __future__ import annotations

import json
import pathlib
import re

from custom_components.huawei_scale_pro.models import ScaleReading

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "huawei_scale_pro"


def _descriptions() -> list[dict[str, str]]:
    source = (PKG / "sensor.py").read_text()
    block = source[source.index("READING_SENSORS"):source.index("async def async_setup_entry")]
    chunks = re.split(r"ScaleSensorDescription\(", block)[1:]
    out = []
    for chunk in chunks:
        get = lambda name: (re.search(rf'{name}="([^"]+)"', chunk) or [None, None])[1]
        out.append(
            {
                "key": get("key"),
                "translation_key": get("translation_key"),
                "icon": get("icon"),
                "field": (re.search(r"lambda r: r\.(\w+)", chunk) or [None, None])[1],
            }
        )
    return out


def test_every_sensor_has_a_name_an_icon_and_a_real_field():
    descriptions = _descriptions()
    assert len(descriptions) >= 20
    for description in descriptions:
        assert description["key"], description
        assert description["translation_key"] == description["key"]
        assert description["icon"], f"{description['key']} has no icon"
        assert description["field"] in ScaleReading.__dataclass_fields__, description["key"]


def test_every_sensor_is_translated_in_both_languages():
    keys = {d["key"] for d in _descriptions()}
    for lang in ("en", "ru"):
        data = json.loads((PKG / "translations" / f"{lang}.json").read_text())
        sensor = data["entity"]["sensor"]
        for key in keys:
            assert key in sensor, f"{key} missing from {lang}.json"
            assert sensor[key]["name"].strip(), f"{key} has an empty {lang} name"


def test_guest_sensor_is_translated_and_kept_out_of_the_reading_sensors():
    descriptions = {d["key"] for d in _descriptions()}
    assert "guest_weight" not in descriptions  # a guest has no composition
    for lang, expected in (("en", "Guest weight"), ("ru", "Вес гостя")):
        sensor = json.loads((PKG / "translations" / f"{lang}.json").read_text())[
            "entity"
        ]["sensor"]
        assert sensor["guest_weight"]["name"] == expected


def test_app_parity_sensors_are_present_and_named():
    descriptions = {d["key"]: d for d in _descriptions()}
    assert {"body_age", "physique", "muscle_index"} <= descriptions.keys()
    # the mass twins are diagnostic extras, they must not be the only way to see
    # water / protein / muscle
    assert "body_water" in descriptions and "protein" in descriptions
    assert "skeletal_muscle" in descriptions
    ru = json.loads((PKG / "translations" / "ru.json").read_text())["entity"]["sensor"]
    assert ru["body_age"]["name"] == "Биологический возраст"
    assert ru["physique"]["name"] == "Телосложение"
    assert ru["muscle_index"]["name"] == "Скелетно-мышечный индекс"
    assert set(ru["physique"]["state"]) == {
        "slim", "athletic", "normal", "overweight", "obese"
    }


# ---------------------------------------------------------------- options form


def _const_values() -> dict[str, str]:
    """Map CONF_* names to their string values from const.py."""
    source = (PKG / "const.py").read_text()
    return dict(re.findall(r'^(CONF_\w+) = "([^"]+)"', source, re.MULTILINE))


def _settings_fields() -> list[str]:
    """Field names used in the options "settings" form, in order."""
    source = (PKG / "config_flow.py").read_text()
    start = source.index("async def async_step_settings")
    end = source.index("async def async_step_add_profile")
    block = source[start:end]
    values = _const_values()
    fields = []
    for name in re.findall(r"vol\.(?:Required|Optional)\(\s*(CONF_\w+)", block):
        fields.append(values[name])
    return fields


def test_every_options_field_has_a_label_in_both_languages():
    """A missing label shows the raw key in the UI (e.g. "user_match")."""
    fields = _settings_fields()
    assert {"user_match", "user_match_tolerance_kg", "cooldown"} <= set(fields)
    for lang in ("en", "ru"):
        data = json.loads((PKG / "translations" / f"{lang}.json").read_text())
        labels = data["options"]["step"]["settings"]["data"]
        for field in fields:
            assert field in labels, f"{field} has no label in {lang}.json"
            assert labels[field].strip()


def test_every_options_selector_translates_its_values():
    """Dropdown values are translated through `selector.<key>.options`."""
    source = (PKG / "config_flow.py").read_text()
    start = source.index("async def async_step_settings")
    end = source.index("async def async_step_add_profile")
    keys = set(re.findall(r'translation_key="(\w+)"', source[start:end]))
    assert "user_match" in keys
    for lang in ("en", "ru"):
        selector = json.loads((PKG / "translations" / f"{lang}.json").read_text())["selector"]
        for key in keys:
            assert key in selector, f"selector.{key} missing in {lang}.json"
            assert selector[key].get("options"), f"selector.{key}.options empty in {lang}.json"


def test_option_choices_match_the_translated_ones():
    from custom_components.huawei_scale_pro import const

    for lang in ("en", "ru"):
        selector = json.loads((PKG / "translations" / f"{lang}.json").read_text())["selector"]
        assert set(selector["user_match"]["options"]) == set(const.USER_MATCH_OPTIONS)
        assert set(selector["model"]["options"]) == set(const.MODEL_OPTIONS)
        assert set(selector["impedance_units"]["options"]) == set(const.IMPEDANCE_UNIT_OPTIONS)
        assert set(selector["gender_encoding"]["options"]) == set(const.GENDER_ENCODING_OPTIONS)


def test_units_are_russian():
    """Nothing Latin survives in the unit column: HA prints those strings as-is."""
    source = (PKG / "sensor.py").read_text()
    for latin in ('native_unit_of_measurement="kcal"', 'native_unit_of_measurement="bpm"',
                  'native_unit_of_measurement="kg/m²"', 'native_unit_of_measurement="Ω"',
                  "native_unit_of_measurement=UnitOfTime.YEARS"):
        assert latin not in source, latin
    for russian in ('UNIT_KCAL = "ккал"', 'UNIT_BPM = "уд/мин"', 'UNIT_KG_M2 = "кг/м²"',
                    'UNIT_YEARS = "лет"', 'UNIT_OHM = "Ом"'):
        assert russian in source, russian
