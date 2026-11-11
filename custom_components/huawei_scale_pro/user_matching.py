"""Decide which person a weigh-in belongs to, from the body weight itself.

The scale needs the user profile *before* it measures: the profile is sent right
after authentication (``set_user_info``) and the completed measurement arrives
seconds later, so by the time the weight is known the scale has already computed
its own body-fat percentage for whoever was configured. Auto-selection therefore
cannot influence the scale's own figure — it re-attributes the reading
afterwards and recomputes the composition for the person actually on the scale.

Learning is deliberately conservative: a profile becomes selectable only after it
has been weighed at least once in this integration (``MODE_HISTORY``), which is
the "first weigh-in manually, then by weight" behaviour. ``MODE_HISTORY_AND_INITIAL``
additionally allows the initial weight typed into the profile as a rough prior.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import median

from .models import Profile

MODE_OFF = "off"
MODE_HISTORY = "history"
MODE_HISTORY_AND_INITIAL = "history_and_initial"
USER_MATCH_MODES = (MODE_OFF, MODE_HISTORY, MODE_HISTORY_AND_INITIAL)

DEFAULT_MODE = MODE_HISTORY
#: Absolute window, in kilograms. It is deliberately absolute rather than a
#: percentage: a household tunes it once, knowing how far apart its people are.
DEFAULT_TOLERANCE_KG = 2.0
#: Weights kept per profile for the median.
SAMPLE_LIMIT = 5
#: Two plausible people closer than this to the measured weight are reported as
#: ambiguous instead of being silently guessed between.
AMBIGUITY_KG = 0.8
#: A repeated "strange" weight counts as a real change of the person's weight
#: (instead of a second guest weigh-in) only if it is this close to the previous
#: one — a guest stepping on twice shows almost the same number.
REANCHOR_TOLERANCE_KG = 1.0
#: …and only if the two weighings are this far apart. Guests step on twice in a
#: row within a minute; a real change shows up on the next day's weighing.
REANCHOR_MIN_GAP = timedelta(hours=6)

BASIS_LEARNED = "learned"
BASIS_INITIAL = "initial"
BASIS_NONE = "none"


@dataclass(frozen=True)
class MatchDecision:
    """Outcome of matching one weight against the known profiles."""

    profile: Profile | None = None
    reference_kg: float | None = None
    distance_kg: float | None = None
    basis: str = BASIS_NONE
    #: Other plausible profiles, when the measured weight is not decisive.
    ambiguous: tuple[str, ...] = ()

    @property
    def matched(self) -> bool:
        return self.profile is not None


def tolerance_for(reference_kg: float, base_kg: float = DEFAULT_TOLERANCE_KG) -> float:
    """Distance window around a profile's typical weight.

    Currently simply ``base_kg`` — kept as a function so the rule can grow (for
    example a percentage floor for very heavy people) without touching callers.
    """
    return float(base_kg)


def add_sample(
    samples: dict[str, list[float]], name: str, weight_kg: float
) -> dict[str, list[float]]:
    """Return ``samples`` with one more weight for ``name`` (new dict)."""
    history = list(samples.get(name, []))
    history.append(round(float(weight_kg), 2))
    updated = dict(samples)
    updated[name] = history[-SAMPLE_LIMIT:]
    return updated


def reference_weight(
    profile: Profile, samples: dict[str, list[float]], mode: str
) -> tuple[float, str] | None:
    """Typical weight for ``profile`` and where the number came from."""
    learned = samples.get(profile.name) or []
    if learned:
        return median(learned), BASIS_LEARNED
    if mode == MODE_HISTORY_AND_INITIAL and profile.initial_weight_kg > 0:
        return profile.initial_weight_kg, BASIS_INITIAL
    return None


def resolve(
    weight_kg: float,
    profiles: list[Profile],
    samples: dict[str, list[float]],
    *,
    mode: str = DEFAULT_MODE,
    tolerance_kg: float = DEFAULT_TOLERANCE_KG,
    active_name: str | None = None,
) -> MatchDecision:
    """Pick the profile the weight most likely belongs to.

    ``active_name`` is the profile that was sent to the scale for this session;
    it wins ties, so a household where two people weigh almost the same does not
    flap between profiles.
    """
    if mode == MODE_OFF or not profiles:
        return MatchDecision()

    # The guest record is not a household person: a guest weigh-in must never be
    # matched onto it, and it must never become a reference weight for anybody.
    profiles = [profile for profile in profiles if not profile.is_guest]

    candidates: list[tuple[float, int, Profile, float, str]] = []
    for profile in profiles:
        reference = reference_weight(profile, samples, mode)
        if reference is None:
            continue
        reference_kg, basis = reference
        distance = abs(weight_kg - reference_kg)
        if distance > tolerance_for(reference_kg, tolerance_kg):
            continue
        rank = 0 if basis == BASIS_LEARNED else 1
        candidates.append((distance, rank, profile, reference_kg, basis))

    if not candidates:
        return MatchDecision()

    candidates.sort(key=lambda item: (item[1], item[0], item[2].name))
    best = candidates[0]
    plausible = [item for item in candidates if item[0] <= best[0] + AMBIGUITY_KG]
    ambiguous = tuple(item[2].name for item in plausible if item[2].name != best[2].name)

    chosen = best
    if len(plausible) > 1 and active_name:
        for item in plausible:
            if item[2].name == active_name:
                chosen = item
                break

    return MatchDecision(
        profile=chosen[2],
        reference_kg=round(chosen[3], 2),
        distance_kg=round(chosen[0], 2),
        basis=chosen[4],
        ambiguous=ambiguous,
    )


@dataclass(frozen=True)
class Outlier:
    """A weigh-in that fitted nobody, remembered to recognise a real change."""

    name: str
    weight_kg: float
    at: datetime


@dataclass(frozen=True)
class Attribution:
    """Who a weigh-in belongs to, and whether it belongs to anybody at all."""

    profile: Profile
    #: True when the weight fits nobody: a guest stepped on the scale.
    guest: bool = False
    decision: MatchDecision = MatchDecision()
    #: True when this profile's weight history was clearly stale (a big but
    #: repeated change) and should be restarted from this measurement.
    reanchored: bool = False


def attribute(
    weight_kg: float,
    profiles: list[Profile],
    samples: dict[str, list[float]],
    *,
    session_profile: Profile,
    mode: str = DEFAULT_MODE,
    tolerance_kg: float = DEFAULT_TOLERANCE_KG,
    previous_outlier: Outlier | None = None,
    now: datetime | None = None,
) -> Attribution:
    """Attribute a live weigh-in, recognising one-off "guest" weighings.

    The scale cannot be told who is on it before it measures, so this runs after
    the fact:

    * a weight that fits someone's learned history goes to that person;
    * when nothing fits and the selected profile has no history at all, it is
      taken as that profile's very first (manual) weigh-in;
    * when nothing fits although the selected profile *does* have history, it is
      treated as a guest: one-off, not recorded as anybody's data. A *repeated*
      weight like that — within ``REANCHOR_TOLERANCE_KG`` and at least
      ``REANCHOR_MIN_GAP`` later — is not a guest but a real change of the
      profile's weight, so the history is restarted from it (``reanchored``).
    """
    if session_profile.is_guest:
        # Guest mode: the reading was measured on purpose for a visitor, so it is
        # a guest weigh-in whatever the weight is and whoever it resembles.
        return Attribution(profile=session_profile, guest=True)

    if mode == MODE_OFF:
        # Matching disabled: the selected profile always gets the reading.
        return Attribution(profile=session_profile)

    decision = resolve(
        weight_kg,
        profiles,
        samples,
        mode=mode,
        tolerance_kg=tolerance_kg,
        active_name=session_profile.name,
    )
    if decision.matched and decision.profile is not None:
        return Attribution(profile=decision.profile, decision=decision)

    learned = samples.get(session_profile.name) or []
    if not learned:
        # First weigh-in for this profile: trust it, there is nothing to compare.
        return Attribution(profile=session_profile, decision=decision)

    if previous_outlier is not None and previous_outlier.name == session_profile.name:
        # The same unusual weight again is not a guest but a real change of this
        # person's weight — provided it is really the same weight (a guest shows
        # nearly the same number twice) and really later (a guest's repeat comes
        # seconds later, a person's new weight shows up on the next day).
        close = abs(weight_kg - previous_outlier.weight_kg) <= min(
            REANCHOR_TOLERANCE_KG, tolerance_kg
        )
        later = True
        if now is not None:
            later = (now - previous_outlier.at) >= REANCHOR_MIN_GAP
        if close and later:
            return Attribution(profile=session_profile, decision=decision, reanchored=True)

    return Attribution(profile=session_profile, guest=True, decision=decision)
