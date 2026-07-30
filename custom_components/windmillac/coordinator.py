"""Data coordination and serialized command handling for WindmillAC."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.components.climate.const import HVACMode
from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    FAN_MODE_TO_PIN,
    HVAC_MODE_TO_PIN,
    WindmillAmbiguousWriteError,
    WindmillApiClient,
    WindmillAuthError,
    WindmillProtocolError,
    WindmillSnapshot,
    WindmillTransientError,
)
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WindmillCoordinatorData:
    """Coordinator state plus whether the snapshot is cached."""

    snapshot: WindmillSnapshot
    stale: bool


class WindmillDataUpdateCoordinator(DataUpdateCoordinator[WindmillCoordinatorData]):
    """Class to manage fetching data from the Windmill AC API."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        api: WindmillApiClient,
    ) -> None:
        """Initialize."""
        self.api = api
        self._has_successful_poll = False
        self._consecutive_transient_failures = 0
        self._command_generation = 0
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            always_update=False,
        )

    @property
    def consecutive_transient_failures(self) -> int:
        """Return the current transient failure count for diagnostics."""
        return self._consecutive_transient_failures

    async def _async_update_data(self) -> WindmillCoordinatorData:
        """Fetch data from Windmill AC."""
        started_generation = self._command_generation
        try:
            snapshot = await self.api.async_get_snapshot()
        except WindmillAuthError as err:
            raise ConfigEntryAuthFailed("Windmill credentials were rejected") from err
        except WindmillTransientError as err:
            self._consecutive_transient_failures += 1
            if (
                self._has_successful_poll
                and self._consecutive_transient_failures == 1
                and self.data is not None
            ):
                _LOGGER.warning("Using cached Windmill data after one transient failure")
                return WindmillCoordinatorData(self.data.snapshot, stale=True)
            raise UpdateFailed("Unable to update Windmill state") from err
        except (WindmillProtocolError, ValueError, TypeError) as err:
            raise UpdateFailed("Windmill returned unusable state") from err

        self._has_successful_poll = True
        self._consecutive_transient_failures = 0

        if started_generation != self._command_generation and self.data is not None:
            _LOGGER.debug("Discarding Windmill state read that predates a command")
            return self.data
        return WindmillCoordinatorData(snapshot, stale=False)

    async def async_set_target_temperature(self, temperature: float) -> None:
        """Set the target temperature and reconcile state."""
        await self._async_write_and_refresh({"V2": _format_number(temperature)})

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set power and mode, batching power-on with the selected mode."""
        if hvac_mode is HVACMode.OFF:
            values = {"V0": "0"}
        else:
            try:
                mode = HVAC_MODE_TO_PIN[hvac_mode]
            except KeyError as err:
                raise ValueError(f"Unsupported HVAC mode: {hvac_mode}") from err
            values = {"V3": mode}
            if self.data is None or not self.data.snapshot.power:
                values = {"V0": "1", "V3": mode}
        await self._async_write_and_refresh(values)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode and reconcile state."""
        try:
            value = FAN_MODE_TO_PIN[fan_mode]
        except KeyError as err:
            raise ValueError(f"Unsupported fan mode: {fan_mode}") from err
        await self._async_write_and_refresh({"V4": value})

    async def async_set_power(self, power: bool) -> None:
        """Set power and reconcile state."""
        await self._async_write_and_refresh({"V0": "1" if power else "0"})

    async def _async_write_and_refresh(self, values: dict[str, str]) -> None:
        """Write once and refresh only after the client's I/O lock is released."""
        self._command_generation += 1
        try:
            await self.api.async_write_pins(values)
        except WindmillAuthError as err:
            raise ConfigEntryAuthFailed("Windmill credentials were rejected") from err
        except WindmillAmbiguousWriteError:
            await self.async_request_refresh()
            raise
        await self.async_request_refresh()


def _format_number(value: float) -> str:
    """Format numeric pin values without unnecessary decimal suffixes."""
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)
