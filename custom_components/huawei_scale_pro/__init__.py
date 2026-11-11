"""Huawei Scale 3 Pro (Hagrid WSP, Bluetooth) integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .coordinator import HuaweiScaleProCoordinator

type ScaleConfigEntry = ConfigEntry[HuaweiScaleProCoordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT, Platform.BUTTON]

_LOGGER = logging.getLogger(__name__)

SERVICE_DUMP_FRAMES = "dump_frames"
SERVICE_MEASURE = "measure"
SERVICE_DUMP_FRAMES_SCHEMA = vol.Schema({vol.Optional("entry_id"): cv.string})
FRAME_DUMP_FILENAME = "huawei_scale_pro_frames.txt"


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the frame-dump service once."""

    if hass.services.has_service(DOMAIN, SERVICE_DUMP_FRAMES):
        return

    async def _dump(call: ServiceCall) -> ServiceResponse:
        wanted = call.data.get("entry_id")
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if wanted in (None, entry.entry_id) and getattr(entry, "runtime_data", None)
        ]
        if not entries:
            raise HomeAssistantError("No loaded Huawei Scale 3 Pro entry to dump")

        dumps = {
            entry.title: entry.runtime_data.frames_text()  # type: ignore[union-attr]
            for entry in entries
        }
        text = "\n\n".join(dumps.values())
        path = hass.config.path(FRAME_DUMP_FILENAME)

        def _write() -> None:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")

        await hass.async_add_executor_job(_write)
        _LOGGER.info("Wrote frame dump to %s", path)
        return {"path": path, "frames": dumps}

    hass.services.async_register(
        DOMAIN,
        SERVICE_DUMP_FRAMES,
        _dump,
        schema=SERVICE_DUMP_FRAMES_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    async def _measure(call: ServiceCall) -> None:
        wanted = call.data.get("entry_id")
        for entry in hass.config_entries.async_entries(DOMAIN):
            if wanted not in (None, entry.entry_id):
                continue
            coordinator = getattr(entry, "runtime_data", None)
            if coordinator is not None:
                await coordinator.async_trigger_session()

    hass.services.async_register(
        DOMAIN,
        SERVICE_MEASURE,
        _measure,
        schema=SERVICE_DUMP_FRAMES_SCHEMA,
    )


async def async_setup_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Set up a scale from a config entry."""
    _async_register_services(hass)
    coordinator = HuaweiScaleProCoordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Registered last so no session can start before the entities listen.
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register the frame-dump service."""
    _async_register_services(hass)
    return True
