"""Guest mode: a visitor is measured, but nothing is ever filed under them.

The rules these tests pin down, in the words of the requirement:

* a guest weigh-in does not go into any profile — not the selected one, not the
  one whose weight happens to match;
* it does not take part in recognising people by weight: the guest record is not
  a candidate, and a guest weigh-in does not teach anybody's weight;
* guest data lives on its own device, is replaced rather than accumulated, and
  expires.
"""
from __future__ import annotations

import json
import pathlib
import re
from datetime import UTC, date, datetime

import pytest

from custom_components.huawei_scale_pro import protocol as p
from custom_components.huawei_scale_pro import user_matching as um
from custom_components.huawei_scale_pro.const import (
    ASSIGNED_BY_GUEST_AUTO,
    ASSIGNED_BY_GUEST_MODE,
    GUEST_DEVICE_NAME_RU,
    GUEST_KEEP_DAYS,
    GUEST_NAME,
    GUEST_STATUS_AUTO,
    GUEST_STATUS_MODE,
    GUEST_STATUSES,
    STATUS_OPTIONS,
)
from custom_components.huawei_scale_pro.models import (
    Profile,
    build_guest_profile,
    build_reading,
    guess_guest_height,
    guest_age_from_weight,
    relabel_reading,
)

from .fake_scale import realtime_payload

PKG = pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "huawei_scale_pro"

ALEX = Profile("Alex", True, date(1980, 1, 1), 178.0, 86.0)
MARIA = Profile("Maria", False, date(1985, 6, 6), 165.0, 62.0)
TODAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 20, 0, tzinfo=UTC)
SAMPLES = {"Alex": [86.2, 86.0, 86.4], "Maria": [61.8, 62.2]}


def _measure(**kw):
    return p.parse_realtime_measurement(realtime_payload(**kw))


# ------------------------------------------------------------ guest profile


def test_a_guest_profile_is_a_throwaway_profile_of_a_visitor():
    guest = build_guest_profile(None, None, today=TODAY)
    assert guest.is_guest
    assert guest.status == GUEST_STATUS_AUTO
    assert guest.name == GUEST_NAME
    assert 140 <= guest.height_cm <= 215
    assert 18 <= guest.age_on(TODAY) <= 80


def test_the_height_is_guessed_so_that_the_weight_gives_a_normal_bmi():
    for weight in (55.0, 76.25, 95.0):
        height = guess_guest_height(weight)
        bmi = weight / (height / 100.0) ** 2
        assert 21.5 <= bmi <= 24.5, (weight, height, bmi)
        assert 140 <= height <= 215


def test_the_guess_is_clamped_for_extreme_weights():
    assert guess_guest_height(15.0) == 140
    assert guess_guest_height(400.0) == 215
    assert guess_guest_height(0) > 0


def test_a_heavier_guest_is_assumed_older_and_the_age_stays_adult():
    ages = [guest_age_from_weight(w, 170) for w in (50, 65, 80, 95, 120)]
    assert ages == sorted(ages)
    assert all(18 <= age <= 90 for age in ages)


def test_what_the_household_typed_wins_over_the_guess():
    guest = build_guest_profile(
        {"height_cm": 182, "age": 54, "male": False},
        _measure(),
        today=TODAY,
        status=GUEST_STATUS_MODE,
    )
    assert guest.height_cm == 182
    assert guest.age_on(TODAY) == 54
    assert guest.male is False
    assert guest.status == GUEST_STATUS_MODE


def test_a_previous_guest_weigh_in_keeps_consecutive_ones_comparable():
    first = build_guest_profile(None, _measure(), today=TODAY)
    stored = {"height_cm": first.height_cm, "birth_date": first.birth_date.isoformat()}
    again = build_guest_profile(stored, _measure(weight_kg=80.0), today=TODAY)
    assert again.height_cm == first.height_cm
    assert again.birth_date == first.birth_date


def test_guest_is_never_a_household_profile():
    guest = build_guest_profile(None, None, today=TODAY)
    assert guest.is_guest
    assert not ALEX.is_guest
    assert guest.status in GUEST_STATUSES
    # and the record survives a round trip through the store
    assert Profile.from_dict(guest.to_dict()) == guest


# ------------------------------------------------------- attribution rules


def test_a_guest_record_is_never_a_candidate_for_matching():
    """The guest's own weight must not attract anybody's weigh-in to it."""
    guest = build_guest_profile(None, None, today=TODAY)
    decision = um.resolve(86.1, [ALEX, MARIA, guest], SAMPLES)
    assert decision.profile is not None
    assert decision.profile.name == "Alex"
    # the guest record would have been the closest profile had it been kept
    assert all(result.profile is not guest for result in [decision])


def test_a_guest_weigh_in_is_never_matched_onto_a_person():
    """Guest mode is a decision, not a weight: even a matching weight stays guest."""
    guest = build_guest_profile(None, None, today=TODAY, status=GUEST_STATUS_MODE)
    result = um.attribute(
        86.1,  # exactly Alex's weight
        [ALEX, MARIA],
        SAMPLES,
        session_profile=guest,
    )
    assert result.guest is True
    assert result.profile.is_guest
    assert result.decision.matched is False


def test_a_guest_weigh_in_works_with_matching_switched_off_too():
    guest = build_guest_profile(None, None, today=TODAY)
    result = um.attribute(70.0, [ALEX], SAMPLES, session_profile=guest, mode=um.MODE_OFF)
    assert result.guest is True


def test_a_household_weigh_in_still_works_and_is_attributed_by_weight():
    result = um.attribute(86.1, [ALEX, MARIA], SAMPLES, session_profile=MARIA)
    assert result.guest is False
    assert result.profile.name == "Alex"


# ------------------------------------------------------------ the reading


def test_a_guest_reading_keeps_its_composition_but_belongs_to_nobody():
    guest = build_guest_profile({"height_cm": 180, "age": 40}, None, today=TODAY)
    reading = build_reading(_measure(), guest, timestamp=NOW, today=TODAY)
    assert reading.composition == "model"
    assert reading.profile == GUEST_NAME
    labelled = relabel_reading(reading, guest=True, assigned_by=ASSIGNED_BY_GUEST_MODE)
    assert labelled.profile == GUEST_NAME
    assert labelled.assigned_by == ASSIGNED_BY_GUEST_MODE
    # the body composition the scale produced is kept for the household to see
    assert labelled.body_fat_percent == reading.body_fat_percent
    assert labelled.water_percent == reading.water_percent
    assert labelled.bmr_kcal == reading.bmr_kcal


def test_relabelling_a_household_reading_changes_nothing():
    reading = build_reading(_measure(), ALEX, timestamp=NOW, today=TODAY)
    assert relabel_reading(reading, guest=False, assigned_by="weight").profile == "Alex"


def test_the_guest_and_the_automatic_guest_have_different_markers():
    assert ASSIGNED_BY_GUEST_MODE != ASSIGNED_BY_GUEST_AUTO


# ------------------------------------------------- coordinator invariants
#
# The coordinator imports Home Assistant, so it is inspected as source — the same
# approach ``test_entities`` uses. These are the lines that keep a guest out of
# everyone's data, so they are worth guarding against a careless refactor.


def _coordinator_source() -> str:
    return (PKG / "coordinator.py").read_text()


def test_a_guest_reading_is_stored_separately_and_returns_early():
    source = _coordinator_source()
    block = source[source.index("if attribution.guest:"):]
    block = block[: block.index("target = attribution.profile")]
    assert "_record_guest_mode" in block
    assert "_record_guest" in block
    # nothing of the household bookkeeping may be reached on this path
    for forbidden in ("_last_weights", "add_sample", "self.reading ="):
        assert forbidden not in block


def test_a_guest_mode_reading_never_touches_anybody():
    source = _coordinator_source()
    block = source[source.index("def _record_guest_mode"):]
    block = block[: block.index("def _record_guest(")]
    assert "build_reading" in block  # the composition is really computed
    assert "relabel_reading" in block  # …and filed under nobody
    for forbidden in ("_last_weights", "_learned_weights", "active_profile_name"):
        assert forbidden not in block


def test_learned_weights_are_never_taught_by_a_guest():
    source = _coordinator_source()
    store = source[source.index("def _store_guest_reading"):]
    store = store[: store.index("def _clear_guest")]
    assert "_learned_weights" not in store
    assert "_last_weights" not in store


def test_guest_data_is_replaced_and_forgettable():
    source = _coordinator_source()
    assert "self.guest_reading = reading" in source  # replace, not append
    assert "async_forget_guest" in source
    assert "GUEST_KEEP_DAYS" in source  # expires on load


def test_the_select_offers_the_guest_next_to_the_people():
    source = (PKG / "select.py").read_text()
    assert "self.coordinator.guest_option" in source
    assert "async_set_selection" in source


# ------------------------------------------------------ entities & strings


def test_guest_sensors_live_on_their_own_device():
    source = (PKG / "sensor.py").read_text()
    assert "GUEST_SENSORS" in source
    assert "guest_device_info" in source
    block = source[source.index("class GuestReadingSensor"):]
    block = block[: block.index("class GuestSensor")]
    assert "guest_reading" in block  # reads the guest copy, never the household one
    assert "self.reading" not in block


def test_guest_sensors_are_translated_in_both_languages():
    source = (PKG / "sensor.py").read_text()
    block = source[source.index("GUEST_SENSORS"):source.index("def guest_device_info")]
    keys = re.findall(r'translation_key="([^"]+)"', block)
    assert len(keys) >= 5
    for lang in ("ru", "en"):
        data = json.loads((PKG / "translations" / f"{lang}.json").read_text())
        sensors = data["entity"]["sensor"]
        for key in keys:
            assert key in sensors, f"{key} missing from {lang}.json"
            assert sensors[key]["name"].strip()


def test_the_guest_device_and_button_are_named_in_russian():
    assert GUEST_DEVICE_NAME_RU == "Гостевые данные"
    ru = json.loads((PKG / "translations" / "ru.json").read_text())
    assert ru["entity"]["button"]["forget_guest"]["name"] == "Забыть гостя"
    assert ru["entity"]["sensor"]["guest_weight"]["name"] == "Вес гостя"
    assert ru["entity"]["select"]["active_user"]["name"] == "Кто взвешивается"


def test_the_status_says_when_the_scale_is_paused_or_measuring_a_guest():
    assert {"guest", "paused"} <= set(STATUS_OPTIONS)
    ru = json.loads((PKG / "translations" / "ru.json").read_text())
    states = ru["entity"]["sensor"]["status"]["state"]
    assert set(STATUS_OPTIONS) <= set(states)
    assert states["paused"] == "Пауза после гостя"


def test_guest_options_have_labels_in_both_languages():
    source = (PKG / "config_flow.py").read_text()
    block = source[source.index("async def async_step_guest"):]
    fields = re.findall(r'vol\.Optional\(\s*(?:"([^"]+)"|(CONF_\w+))', block)
    names = {a or b for a, b in fields}
    consts = dict(re.findall(r'^(CONF_\w+) = "([^"]+)"', (PKG / "const.py").read_text(), re.M))
    labels = {consts.get(name, name) for name in names}
    assert {"guest_aftermath", "guest_height_cm", "guest_age", "guest_sex"} <= labels
    for lang in ("ru", "en"):
        data = json.loads((PKG / "translations" / f"{lang}.json").read_text())
        step = data["options"]["step"]["guest"]
        for label in labels:
            assert label in step["data"], f"{label} has no label in {lang}.json"
        assert data["options"]["step"]["init"]["menu_options"]["guest"]
        for key in ("guest_aftermath", "guest_sex"):
            assert data["selector"][key]["options"]


def test_guest_expiry_is_a_week():
    assert GUEST_KEEP_DAYS == 7


@pytest.mark.parametrize("status", [GUEST_STATUS_AUTO, GUEST_STATUS_MODE])
def test_both_guest_statuses_mark_a_guest(status):
    guest = build_guest_profile(None, None, today=TODAY, status=status)
    assert guest.is_guest


def test_guest_entities_appear_only_once_there_is_a_guest():
    source = _coordinator_source()
    # created lazily from both guest paths (mode and auto), never at setup
    mode_path = source[source.index("def _record_guest_mode"):]
    assert "_store_guest_reading" in mode_path[: mode_path.index("def _record_guest(self")]
    store = source[source.index("def _store_guest_reading"):]
    assert "async_add_guest_entities" in store[: store.index("def _clear_guest")]
    auto = source[source.index("def _record_guest(self"):]
    assert "async_add_guest_entities" in auto[: auto.index("def _async_pause_after_guest")]
    sensors = (PKG / "sensor.py").read_text()
    assert "async_set_guest_entity_hook" in sensors
    assert "if coordinator.guest_has_data:" in sensors


def test_forget_guest_button_is_always_available():
    """The button must not override HA's default availability based on guest data."""
    import ast
    tree = ast.parse((PKG / "button.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "ForgetGuestButton")
    assert not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == "available" for n in cls.body)
    assert not any(isinstance(n, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id == "_attr_available" for t in n.targets
    ) for n in cls.body)


@pytest.mark.parametrize("has_data", [False, True])
def test_forget_guest_noop_or_clears_without_touching_profile(has_data):
    """Run the real coordinator methods with a minimal stand-in, no HA required."""
    import ast
    import asyncio
    from types import SimpleNamespace

    tree = ast.parse((PKG / "coordinator.py").read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "HuaweiScaleProCoordinator")
    methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and n.name in ("_clear_guest", "async_forget_guest")]
    assert len(methods) == 2
    calls = []
    namespace = {"async_dispatcher_send": lambda *args: calls.append("signal"),
                 "_LOGGER": SimpleNamespace(info=lambda *args: calls.append("log"))}
    exec(compile(ast.Module(body=methods, type_ignores=[]), "<coordinator>", "exec"), namespace)
    class Coordinator:
        _clear_guest = namespace["_clear_guest"]
        async_forget_guest = namespace["async_forget_guest"]
        def _save(self): calls.append("save")
        def _refresh_idle_status(self): calls.append("refresh")
    obj = Coordinator()
    obj.guest_has_data = has_data
    obj.guest_reading = "reading" if has_data else None
    obj.guest_mode_reading = "reading" if has_data else None
    obj.guest_weight_kg = 76.25 if has_data else None
    obj.guest_at = NOW if has_data else None
    obj.guest_mode = has_data
    obj._guest_mode_manual = has_data
    obj.active_profile_name = "Alex"
    obj.hass, obj.signal = object(), "signal"
    before = obj.__dict__.copy()
    asyncio.run(obj.async_forget_guest())
    assert obj.active_profile_name == "Alex"
    if not has_data:
        assert obj.__dict__ == before
        assert calls == []
    else:
        assert obj.guest_reading is obj.guest_mode_reading is None
        assert obj.guest_weight_kg is obj.guest_at is None
        assert not obj.guest_mode and not obj._guest_mode_manual
        assert calls == ["save", "refresh", "signal", "log"]
