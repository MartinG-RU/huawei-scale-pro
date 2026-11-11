"""Choose which person the scale is currently set up for — or that it is a guest."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ScaleConfigEntry
from .coordinator import HuaweiScaleProCoordinator
from .entity import HuaweiScaleProEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ActiveUserSelect(entry.runtime_data)])


class ActiveUserSelect(HuaweiScaleProEntity, SelectEntity):
    """Who is weighing: a household profile, or Guest.

    The scale needs age, height and sex *before* it measures, so whoever steps on
    next is assumed to be the selected person. With user matching enabled the
    selection follows the person who actually weighed in (see ``user_matching``),
    so this entity normally sets itself.

    Guest is not a person: picking it measures a visitor with a throwaway profile
    and files nothing under anybody. It ends with that weigh-in — unless the
    "pause after a guest" option is on, in which case the scale waits here until a
    person is selected again.
    """

    _attr_icon = "mdi:account-switch"

    def __init__(self, coordinator: HuaweiScaleProCoordinator) -> None:
        super().__init__(coordinator, "active_user")

    @property
    def options(self) -> list[str]:
        """Household profiles, then the guest option.

        The guest record is not a person, so it is added as the mode — with the
        label the coordinator answers to, never as a second profile.
        """
        people = [
            profile.name
            for profile in self.coordinator.profiles
            if not profile.is_guest
        ]
        return [*people, self.coordinator.guest_option]

    @property
    def current_option(self) -> str | None:
        """A person's name, or the guest option — never an empty string."""
        return self.coordinator.selection or None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_selection(option)
