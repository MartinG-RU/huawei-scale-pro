"""Sensors for the Huawei Scale 3 Pro."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ScaleConfigEntry
from .body_composition import PHYSIQUE_OPTIONS
from .const import DOMAIN, GUEST_DEVICE_NAME_RU, STATUS_OPTIONS
from .coordinator import HuaweiScaleProCoordinator
from .entity import HuaweiScaleProEntity
from .models import ScaleReading


@dataclass(frozen=True, kw_only=True)
class ScaleSensorDescription(SensorEntityDescription):
    """Describes a sensor fed from the latest reading."""

    value_fn: Callable[[ScaleReading], Any]
    #: Also expose the raw impedance words / payload of the reading.
    raw_details: bool = False


#: HA prints "kcal", "bpm", "kg/m²", "years" and "Ω" as-is in every language, and
#: this integration is Russian-only in its UI, so each of those spells itself out.
#: Kilograms and percent stay HA units: the frontend already localises those, and
#: keeping them lets HA convert the weight for anyone who prefers pounds.
UNIT_KCAL = "ккал"
UNIT_BPM = "уд/мин"
UNIT_KG_M2 = "кг/м²"
UNIT_YEARS = "лет"
UNIT_OHM = "Ом"

READING_SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="weight",
        translation_key="weight",
        icon="mdi:scale-bathroom",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        raw_details=True,
        value_fn=lambda r: r.weight_kg,
    ),
    ScaleSensorDescription(
        key="bmi",
        translation_key="bmi",
        icon="mdi:human-male-height",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.bmi,
    ),
    ScaleSensorDescription(
        key="body_fat",
        translation_key="body_fat",
        icon="mdi:circle-slice-8",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.body_fat_percent,
    ),
    ScaleSensorDescription(
        key="body_water",
        translation_key="body_water",
        icon="mdi:water-percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.water_percent,
    ),
    ScaleSensorDescription(
        key="skeletal_muscle",
        translation_key="skeletal_muscle",
        icon="mdi:arm-flex",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.skeletal_muscle_percent,
    ),
    ScaleSensorDescription(
        key="bone_mass",
        translation_key="bone_mass",
        icon="mdi:bone",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r: r.bone_mass_kg,
    ),
    ScaleSensorDescription(
        key="lean_body_mass",
        translation_key="lean_body_mass",
        icon="mdi:human",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.lean_body_mass_kg,
    ),
    ScaleSensorDescription(
        key="protein",
        translation_key="protein",
        icon="mdi:food-steak",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.protein_percent,
    ),
    ScaleSensorDescription(
        key="bmr",
        translation_key="bmr",
        icon="mdi:fire",
        native_unit_of_measurement=UNIT_KCAL,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.bmr_kcal,
    ),
    ScaleSensorDescription(
        key="visceral_fat",
        translation_key="visceral_fat",
        icon="mdi:stomach",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.visceral_fat_level,
    ),
    ScaleSensorDescription(
        key="heart_rate",
        translation_key="heart_rate",
        icon="mdi:heart-pulse",
        native_unit_of_measurement=UNIT_BPM,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.heart_rate,
    ),
    ScaleSensorDescription(
        key="muscle_index",
        translation_key="muscle_index",
        icon="mdi:human-handsup",
        native_unit_of_measurement=UNIT_KG_M2,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.muscle_index_kg_m2,
    ),
    ScaleSensorDescription(
        key="body_age",
        translation_key="body_age",
        icon="mdi:cake-variant",
        native_unit_of_measurement=UNIT_YEARS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.body_age_years,
    ),
    ScaleSensorDescription(
        key="physique",
        translation_key="physique",
        icon="mdi:human-male",
        device_class=SensorDeviceClass.ENUM,
        options=list(PHYSIQUE_OPTIONS),
        value_fn=lambda r: r.physique,
    ),
    ScaleSensorDescription(
        key="impedance",
        translation_key="impedance",
        icon="mdi:flash",
        native_unit_of_measurement=UNIT_OHM,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.impedance_ohm,
    ),
    ScaleSensorDescription(
        key="impedance_high",
        translation_key="impedance_high",
        icon="mdi:lightning-bolt",
        native_unit_of_measurement=UNIT_OHM,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.impedance_high_ohm,
    ),
    ScaleSensorDescription(
        key="body_water_kg",
        translation_key="body_water_kg",
        icon="mdi:water",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.water_kg,
    ),
    ScaleSensorDescription(
        key="protein_kg",
        translation_key="protein_kg",
        icon="mdi:food-steak",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.protein_kg,
    ),
    ScaleSensorDescription(
        key="skeletal_muscle_kg",
        translation_key="skeletal_muscle_kg",
        icon="mdi:arm-flex",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.skeletal_muscle_kg,
    ),
    ScaleSensorDescription(
        key="measured_user",
        translation_key="measured_user",
        icon="mdi:account-check",
        value_fn=lambda r: r.profile,
    ),
    ScaleSensorDescription(
        key="last_measurement",
        translation_key="last_measurement",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.timestamp,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        ReadingSensor(coordinator, description) for description in READING_SENSORS
    ]
    entities.append(StatusSensor(coordinator))
    entities.append(ModelSensor(coordinator))
    async_add_entities(entities)

    def _guest_entities() -> list[SensorEntity]:
        return [
            GuestSensor(coordinator),
            *(GuestReadingSensor(coordinator, description) for description in GUEST_SENSORS),
        ]

    # The guest's sensors (and its "Гостевые данные" device) only appear once there
    # is a guest to show, so an unused guest leaves nothing empty behind. Guest data
    # restored from earlier visits is already there at this point.
    if coordinator.guest_has_data:
        async_add_entities(_guest_entities())
        coordinator.mark_guest_entities_added()
    else:
        coordinator.async_set_guest_entity_hook(
            lambda: async_add_entities(_guest_entities())
        )


class ReadingSensor(HuaweiScaleProEntity, SensorEntity):
    """A value from the most recent measurement."""

    entity_description: ScaleSensorDescription

    def __init__(
        self, coordinator: HuaweiScaleProCoordinator, description: ScaleSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.icon:
            # Belt and braces: apply the description's icon explicitly rather
            # than relying on the description being merged into the entity.
            self._attr_icon = description.icon

    @property
    def native_value(self) -> float | int | str | datetime | None:
        reading = self.coordinator.reading
        return self.entity_description.value_fn(reading) if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        reading = self.coordinator.reading
        if reading is None:
            return None
        attributes: dict[str, Any] = {
            "history": reading.history,
            "composition": reading.composition,
            "source": reading.source,
        }
        if self.entity_description.key == "measured_user":
            # How this measurement was attributed (see the user-match option).
            attributes |= {
                "assigned_by": reading.assigned_by,
                "match_reference_kg": reading.match_reference_kg,
                "match_distance_kg": reading.match_distance_kg,
                "match_basis": reading.match_basis,
                "match_ambiguous": list(reading.match_ambiguous),
                "learned_weights": self.coordinator.learned_weights(),
            }
        if self.entity_description.raw_details:
            attributes |= {
                "payload_length": reading.payload_length,
                "payload_hex": reading.payload_hex,
                "impedance_raw_low": reading.impedance_raw_low,
                "impedance_raw_high": reading.impedance_raw_high,
                "model_impedance_ohm": reading.model_impedance_ohm,
                "body_fat_source": reading.body_fat_source,
                # 8-electrode data: whole-body values are in impedance_ohm /
                # impedance_high_ohm, these are the segmental ones.
                "impedance_segments_low": reading.impedance_segments_low,
                "impedance_segments_high": reading.impedance_segments_high,
            }
        return attributes


#: The guest's own copy of the appliances a household profile has: a weigh-in made
#: in guest mode is measured with a throwaway profile, so its composition exists —
#: it just belongs to nobody. These live on a separate "Гостевые данные" device and
#: hold the last guest reading only; nothing here is fed back into matching.
GUEST_SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="guest_body_fat",
        translation_key="guest_body_fat",
        icon="mdi:circle-slice-8",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.body_fat_percent,
    ),
    ScaleSensorDescription(
        key="guest_body_water",
        translation_key="guest_body_water",
        icon="mdi:water-percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.water_percent,
    ),
    ScaleSensorDescription(
        key="guest_skeletal_muscle",
        translation_key="guest_skeletal_muscle",
        icon="mdi:arm-flex",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.skeletal_muscle_percent,
    ),
    ScaleSensorDescription(
        key="guest_bmr",
        translation_key="guest_bmr",
        icon="mdi:fire",
        native_unit_of_measurement=UNIT_KCAL,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.bmr_kcal,
    ),
    ScaleSensorDescription(
        key="guest_impedance",
        translation_key="guest_impedance",
        icon="mdi:flash",
        native_unit_of_measurement=UNIT_OHM,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.impedance_ohm,
    ),
    ScaleSensorDescription(
        key="guest_measured_at",
        translation_key="guest_measured_at",
        icon="mdi:clock-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda r: r.timestamp,
    ),
)


def guest_device_info(coordinator: HuaweiScaleProCoordinator) -> DeviceInfo:
    """The child device that carries a guest's readings."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{coordinator.address}_guest")},
        name=GUEST_DEVICE_NAME_RU,
        manufacturer="Huawei",
        model=coordinator.model.display_name,
        via_device=(DOMAIN, coordinator.address),
    )


class GuestReadingSensor(HuaweiScaleProEntity, SensorEntity):
    """A value from the last guest weigh-in, on the guest's own device."""

    entity_description: ScaleSensorDescription

    def __init__(
        self, coordinator: HuaweiScaleProCoordinator, description: ScaleSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        if description.icon:
            self._attr_icon = description.icon
        self._attr_device_info = guest_device_info(coordinator)

    @property
    def native_value(self) -> float | int | str | datetime | None:
        reading = self.coordinator.guest_reading
        return self.entity_description.value_fn(reading) if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        reading = self.coordinator.guest_reading
        if reading is None:
            return None
        return {
            "guest": True,
            "assigned_to": None,
            "composition": reading.composition,
            "body_fat_source": reading.body_fat_source,
            "note": (
                "Гостевое измерение: никому не назначено, в определение человека "
                "по весу не участвует"
            ),
        }


class GuestSensor(HuaweiScaleProEntity, SensorEntity):
    """The last weigh-in that fitted nobody — a one-off, e.g. a visitor.

    It is kept separate on purpose: a guest's weight must not overwrite the
    household's measurements and must not teach the weight-based matching.
    """

    _attr_device_class = SensorDeviceClass.WEIGHT
    _attr_native_unit_of_measurement = UnitOfMass.KILOGRAMS
    _attr_suggested_display_precision = 2
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:account-question"

    def __init__(self, coordinator: HuaweiScaleProCoordinator) -> None:
        super().__init__(coordinator, "guest_weight")
        self._attr_device_info = guest_device_info(coordinator)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.guest_weight_kg

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        return {
            "weighed_at": (
                coordinator.guest_at.isoformat() if coordinator.guest_at else None
            ),
            "guest_weighings": coordinator.guest_count,
            "tolerance_kg": coordinator.entry.options.get("user_match_tolerance_kg", 2.0),
            "known_weights": coordinator.learned_weights(),
            "mode": "guest_mode" if coordinator.guest_mode else "auto",
            "note": (
                "Не назначено ни одному профилю: сенсоры людей и определение "
                "человека по весу не затрагиваются"
            ),
        }


class StatusSensor(HuaweiScaleProEntity, SensorEntity):
    """What the integration is doing right now; handy for troubleshooting."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:bluetooth-connect"

    def __init__(self, coordinator: HuaweiScaleProCoordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        return {
            "last_error": coordinator.last_error,
            "frames_captured": len(coordinator.capture),
            # Trigger diagnostics: compare these before and after stepping on
            # the scale. If advertisements_seen does not grow, the scale never
            # reaches the Bluetooth stack; if it grows but sessions_started does
            # not, the session gate (cool-down / busy) swallowed it.
            "advertisements_seen": coordinator.advertisements_seen,
            "advertisements_ignored_busy": coordinator.advertisements_ignored_busy,
            "advertisements_ignored_cool_down": coordinator.advertisements_ignored_backoff,
            "sessions_started": coordinator.sessions_started,
            "last_advertisement": (
                coordinator.last_advertisement_at.isoformat()
                if coordinator.last_advertisement_at
                else None
            ),
        }


class ModelSensor(HuaweiScaleProEntity, SensorEntity):
    """Which model the integration thinks the scale is, and why."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:scale-bathroom"

    def __init__(self, coordinator: HuaweiScaleProCoordinator) -> None:
        super().__init__(coordinator, "model")

    @property
    def native_value(self) -> str:
        return self.coordinator.model.display_name

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coordinator = self.coordinator
        return {
            "family": coordinator.model.family,
            "detected_from": coordinator.model_source,
            "product_id": coordinator.product_id,
            "capability_bits": coordinator.capability_bits,
            "high_frequency_impedance_expected": (
                coordinator.model.expected_high_frequency_impedance
            ),
        }
