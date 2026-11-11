"""Diagnostics for the Huawei Scale 3 Pro (secrets and personal data redacted)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from . import ScaleConfigEntry
from .const import CONF_BIRTH_DATE, CONF_C1, CONF_C2, CONF_CAK, CONF_NAME

TO_REDACT = {
    CONF_CAK, CONF_C1, CONF_C2, CONF_ADDRESS, CONF_BIRTH_DATE, CONF_NAME,
    "profile", "user", "huid", "device_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ScaleConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    reading = coordinator.reading.to_dict() if coordinator.reading else None
    return {
        "entry": async_redact_data(
            {"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
        ),
        "status": coordinator.status,
        "last_error": coordinator.last_error,
        "sw_version": coordinator.sw_version,
        "product_id": coordinator.product_id,
        "model": {
            "family": coordinator.model.family,
            "display_name": coordinator.model.display_name,
            "source": coordinator.model_source,
            "expected_high_frequency_impedance": (
                coordinator.model.expected_high_frequency_impedance
            ),
            "gender_encoding": {
                "male": coordinator.model.gender_male,
                "female": coordinator.model.gender_female,
            },
        },
        "capability_bits": coordinator.capability_bits,
        "user_matching": {
            "mode": coordinator.entry.options.get("user_match", "history"),
            "tolerance_kg": coordinator.entry.options.get("user_match_tolerance_kg", 2.0),
            "learned_weights": coordinator.learned_weights(),
            "outlier": (
                {
                    "profile": coordinator._outlier.name,
                    "weight_kg": coordinator._outlier.weight_kg,
                    "at": coordinator._outlier.at.isoformat(),
                }
                if coordinator._outlier
                else None
            ),
            "active_profile": coordinator.active_profile_name,
        },
        "last_reading": async_redact_data(reading, TO_REDACT) if reading else None,
        "last_payload_hex": coordinator.last_raw_hex,
        "frames": coordinator.capture.as_list(),
        "frames_text": coordinator.frames_text(),
    }
