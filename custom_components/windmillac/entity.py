"""Climate entity backed by the Windmill coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import WindmillDataUpdateCoordinator


class WindmillClimate(
    CoordinatorEntity[WindmillDataUpdateCoordinator],
    ClimateEntity,
):
    """Representation of a Windmill Climate device."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_min_temp = 50
    # HA displays 100 °F as 37.8 °C and converts it back to 100.04 °F.
    # Keep a small validation tolerance so the displayed maximum is selectable.
    _attr_max_temp = 100.1
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        coordinator: WindmillDataUpdateCoordinator,
        entry_id: str,
        entity_description: ClimateEntityDescription,
    ) -> None:
        """Initialize the climate device."""
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_hvac_modes = [
            HVACMode.OFF,
            HVACMode.COOL,
            HVACMode.AUTO,
            HVACMode.FAN_ONLY,
        ]
        self._attr_fan_modes = ["Low", "Medium", "High", "Auto"]
        self._attr_unique_id = f"{entry_id}-climate"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Windmill AC",
            manufacturer="Windmill",
        )

    @property
    def current_temperature(self) -> float:
        """Return the current temperature."""
        return self.coordinator.data.snapshot.current_temperature

    @property
    def target_temperature(self) -> float:
        """Return the temperature we try to reach."""
        return self.coordinator.data.snapshot.target_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        """Return current operation mode."""
        return self.coordinator.data.snapshot.hvac_mode

    @property
    def fan_mode(self) -> str:
        """Return the fan setting."""
        return self.coordinator.data.snapshot.fan_mode

    @property
    def is_on(self) -> bool:
        """Return whether the appliance is powered."""
        return self.coordinator.data.snapshot.power

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose when state is cached after a transient read failure."""
        return {"stale": self.coordinator.data.stale}

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is not None:
            await self.coordinator.async_set_target_temperature(temperature)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new operation mode."""
        await self.coordinator.async_set_hvac_mode(hvac_mode)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set new fan mode."""
        await self.coordinator.async_set_fan_mode(fan_mode)

    async def async_turn_on(self) -> None:
        """Turn on the device."""
        await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        """Turn off the device."""
        await self.coordinator.async_set_power(False)
