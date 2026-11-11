"""Manual trigger: start listening for the scale before stepping on it.

The scale only advertises while it is being stood on, so the automatic trigger
depends on the advertisement reaching the Bluetooth stack. When that is flaky
(phone app holding the connection, a proxy that misses the advertisement), the
button is the deterministic way: press it, then step on the scale — the session
sits on the connection and waits for the measurement.
"""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
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
    async_add_entities([MeasureButton(entry.runtime_data), ForgetGuestButton(entry.runtime_data)])


class MeasureButton(HuaweiScaleProEntity, ButtonEntity):
    """Start a weigh-in session now."""

    _attr_icon = "mdi:scale-bathroom"
    _attr_translation_key = "measure"

    def __init__(self, coordinator: HuaweiScaleProCoordinator) -> None:
        super().__init__(coordinator, "measure")

    async def async_press(self) -> None:
        await self.coordinator.async_trigger_session()


class ForgetGuestButton(HuaweiScaleProEntity, ButtonEntity):
    """Throw away the guest data and leave guest mode.

    Guest data expires on its own (see ``GUEST_KEEP_DAYS``), but a household that
    knows the visitor has gone should not have to wait a week for the sensors to
    clear — and a "paused after a guest" scale is resumed from here too.
    """

    _attr_icon = "mdi:account-remove"
    _attr_translation_key = "forget_guest"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HuaweiScaleProCoordinator) -> None:
        super().__init__(coordinator, "forget_guest")

    async def async_press(self) -> None:
        await self.coordinator.async_forget_guest()
