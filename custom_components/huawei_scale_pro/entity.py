"""Base entity."""
from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .coordinator import HuaweiScaleProCoordinator


class HuaweiScaleProEntity(Entity):
    """An entity that refreshes whenever the coordinator publishes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: HuaweiScaleProCoordinator, key: str) -> None:
        self.coordinator = coordinator
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(dr.CONNECTION_BLUETOOTH, coordinator.address)},
            name=coordinator.entry.title,
            manufacturer="Huawei",
            model=coordinator.model.display_name,
            sw_version=coordinator.sw_version,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.coordinator.signal, self.async_write_ha_state
            )
        )
