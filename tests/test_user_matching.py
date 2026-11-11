"""Attributing a weigh-in to a person by body weight."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.huawei_scale_pro import user_matching as um
from custom_components.huawei_scale_pro.models import Profile

ALEX = Profile("Alex", True, date(1973, 5, 1), 176.0, 70.0)
MARIA = Profile("Maria", False, date(1980, 2, 2), 166.0, 60.0)
KID = Profile("Kid", True, date(2015, 3, 3), 140.0, 35.0)
PROFILES = [ALEX, MARIA, KID]


def test_learned_weights_accumulate_and_are_capped():
    samples: dict[str, list[float]] = {}
    for weight in (70.1, 70.3, 70.2, 70.4, 70.0, 70.5):
        samples = um.add_sample(samples, "Alex", weight)
    assert samples["Alex"] == [70.3, 70.2, 70.4, 70.0, 70.5]  # last five only
    assert "Alex" in samples and list(samples) == ["Alex"]


def test_median_of_recent_weights_is_used():
    samples = um.add_sample({}, "Alex", 70.0)
    samples = um.add_sample(samples, "Alex", 71.0)
    samples = um.add_sample(samples, "Alex", 70.5)
    reference = um.reference_weight(ALEX, samples, um.MODE_HISTORY)
    assert reference == (70.5, "learned")


def test_history_mode_ignores_profiles_that_never_stepped_on_the_scale():
    """The first weigh-in of a person must be done manually."""
    decision = um.resolve(60.0, PROFILES, {}, mode=um.MODE_HISTORY)
    assert decision.profile is None
    assert decision.basis == "none"


def test_history_and_initial_mode_uses_the_profile_weight_as_a_hint():
    decision = um.resolve(60.0, PROFILES, {}, mode=um.MODE_HISTORY_AND_INITIAL)
    assert decision.profile is MARIA
    assert decision.basis == "initial"


def test_matching_picks_the_closest_learned_profile():
    samples = um.add_sample({}, "Alex", 70.3)
    samples = um.add_sample(samples, "Maria", 60.1)
    decision = um.resolve(69.8, PROFILES, samples, mode=um.MODE_HISTORY)
    assert decision.profile is ALEX
    assert decision.basis == "learned"
    assert decision.distance_kg == pytest.approx(0.5, abs=0.01)
    assert decision.ambiguous == ()


def test_learned_profiles_win_over_the_initial_weight_hint():
    samples = um.add_sample({}, "Maria", 60.1)
    decision = um.resolve(
        60.0, PROFILES, samples, mode=um.MODE_HISTORY_AND_INITIAL
    )
    assert decision.profile is MARIA and decision.basis == "learned"


def test_a_weight_which_fits_nobody_is_not_forced_onto_someone():
    samples = um.add_sample({}, "Alex", 70.3)
    decision = um.resolve(84.0, PROFILES, samples, mode=um.MODE_HISTORY)
    assert decision.profile is None
    assert decision.ambiguous == ()


def test_tolerance_is_absolute_kilograms():
    assert um.DEFAULT_TOLERANCE_KG == 2.0
    assert um.tolerance_for(70.0) == pytest.approx(2.0)
    assert um.tolerance_for(120.0) == pytest.approx(2.0)
    assert um.tolerance_for(70.0, base_kg=0.5) == pytest.approx(0.5)


def test_two_similar_people_are_reported_as_ambiguous_and_the_selection_holds():
    samples = um.add_sample({}, "Alex", 70.0)
    samples = um.add_sample(samples, "Maria", 70.3)
    decision = um.resolve(70.1, PROFILES, samples, mode=um.MODE_HISTORY, active_name="Alex")
    assert decision.profile is ALEX          # no flapping: the selected profile wins
    assert set(decision.ambiguous) == {"Maria"}
    other = um.resolve(70.1, PROFILES, samples, mode=um.MODE_HISTORY, active_name="Maria")
    assert other.profile is MARIA


def test_off_mode_never_matches():
    samples = um.add_sample({}, "Alex", 70.3)
    decision = um.resolve(70.3, PROFILES, samples, mode=um.MODE_OFF)
    assert decision.profile is None


def test_decision_is_frozen_and_has_a_matched_property():
    decision = um.resolve(70.3, PROFILES, um.add_sample({}, "Alex", 70.3))
    assert decision.matched is True
    with pytest.raises(Exception):
        decision.basis = "other"  # type: ignore[misc]


# --------------------------------------------------------------- guests


def test_first_weigh_in_is_never_a_guest():
    """Nothing learned yet: the manual weigh-in is taken at face value."""
    result = um.attribute(84.0, PROFILES, {}, session_profile=ALEX)
    assert result.guest is False
    assert result.profile is ALEX


def test_weight_that_fits_nobody_is_a_guest():
    samples = um.add_sample({}, "Alex", 70.3)
    result = um.attribute(84.0, PROFILES, samples, session_profile=ALEX)
    assert result.guest is True
    assert result.profile is ALEX          # the reading keeps the session profile…
    assert result.decision.matched is False  # …but nothing was attributed


def test_guest_window_is_the_tolerance():
    samples = um.add_sample({}, "Alex", 70.3)
    assert um.attribute(72.0, PROFILES, samples, session_profile=ALEX).guest is False
    assert um.attribute(72.5, PROFILES, samples, session_profile=ALEX).guest is True
    assert um.attribute(68.4, PROFILES, samples, session_profile=ALEX).guest is False
    assert um.attribute(68.2, PROFILES, samples, session_profile=ALEX).guest is True


def test_a_meal_sized_fluctuation_is_never_a_guest():
    """Eating, drinking, a toilet trip: ±1 kg is just life, not a stranger."""
    samples = um.add_sample({}, "Alex", 70.2)
    for weight in (71.2, 70.9, 69.2, 69.5):
        result = um.attribute(weight, PROFILES, samples, session_profile=ALEX)
        assert result.guest is False, weight
        assert result.profile is ALEX


def test_the_guest_window_is_wide_enough_for_a_full_meal_and_narrow_enough_to_work():
    samples = um.add_sample({}, "Alex", 70.2)
    # right at the tolerance the person is still recognised…
    assert um.attribute(72.2, PROFILES, samples, session_profile=ALEX).guest is False
    assert um.attribute(68.2, PROFILES, samples, session_profile=ALEX).guest is False
    # …a real stranger is not
    assert um.attribute(72.3, PROFILES, samples, session_profile=ALEX).guest is True
    assert um.attribute(68.1, PROFILES, samples, session_profile=ALEX).guest is True


def test_a_matched_profile_is_never_a_guest():
    samples = um.add_sample({}, "Alex", 70.3)
    samples = um.add_sample(samples, "Maria", 60.0)
    result = um.attribute(60.4, PROFILES, samples, session_profile=ALEX)
    assert result.guest is False
    assert result.profile is MARIA


LATER = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)  # next morning


def test_the_same_strange_weight_next_day_is_a_real_change_not_a_guest():
    samples = um.add_sample({}, "Alex", 70.3)
    first = um.attribute(66.0, PROFILES, samples, session_profile=ALEX)
    assert first.guest is True
    second = um.attribute(
        66.1,
        PROFILES,
        samples,
        session_profile=ALEX,
        previous_outlier=um.Outlier("Alex", 66.0, LATER - timedelta(hours=18)),
        now=LATER,
    )
    assert second.guest is False
    assert second.reanchored is True
    assert second.profile is ALEX


def test_a_guest_weighing_twice_in_a_row_stays_a_guest():
    """A visitor who steps on again a minute later must not "become" the owner."""
    samples = um.add_sample({}, "Alex", 70.3)
    first = um.attribute(84.0, PROFILES, samples, session_profile=ALEX)
    assert first.guest is True
    second = um.attribute(
        84.0,
        PROFILES,
        samples,
        session_profile=ALEX,
        previous_outlier=um.Outlier("Alex", 84.0, LATER),
        now=LATER + timedelta(minutes=1),
    )
    assert second.guest is True
    assert second.reanchored is False


def test_a_repeat_that_is_not_the_same_weight_stays_a_guest():
    samples = um.add_sample({}, "Alex", 70.3)
    result = um.attribute(
        86.0,
        PROFILES,
        samples,
        session_profile=ALEX,
        previous_outlier=um.Outlier("Alex", 84.0, LATER - timedelta(days=1)),
        now=LATER,
    )
    assert result.guest is True


def test_reanchoring_only_applies_to_the_selected_profile():
    samples = um.add_sample({}, "Alex", 70.3)
    result = um.attribute(
        66.0,
        PROFILES,
        samples,
        session_profile=ALEX,
        previous_outlier=um.Outlier("Maria", 66.0, LATER - timedelta(days=1)),
        now=LATER,
    )
    assert result.guest is True


def test_reanchor_constants_are_conservative():
    assert um.REANCHOR_TOLERANCE_KG == 1.0
    assert um.REANCHOR_MIN_GAP == timedelta(hours=6)
