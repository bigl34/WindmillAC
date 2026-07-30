"""Tests for diagnostics redaction and entity presentation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.climate import ClimateEntityDescription
from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.windmillac.api import WindmillSnapshot
from custom_components.windmillac.const import CONF_TOKEN, DOMAIN
from custom_components.windmillac.coordinator import WindmillCoordinatorData
from custom_components.windmillac.diagnostics import async_get_config_entry_diagnostics
from custom_components.windmillac.entity import WindmillClimate


def coordinator_data(*, stale: bool) -> WindmillCoordinatorData:
    """Return entity data."""
    return WindmillCoordinatorData(
        WindmillSnapshot(
            power=True,
            current_temperature=74,
            target_temperature=68,
            hvac_mode=HVACMode.COOL,
            fan_mode="Medium",
        ),
        stale,
    )


@pytest.mark.asyncio
async def test_diagnostics_redact_token_and_report_staleness(
    hass: HomeAssistant,
) -> None:
    token = "diagnostics-secret-token"
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: token})
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data=coordinator_data(stale=True),
        consecutive_transient_failures=1,
        last_update_success=True,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"coordinator": coordinator}

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert token not in str(diagnostics)
    assert diagnostics["config_entry"]["data"][CONF_TOKEN] == "**REDACTED**"
    assert diagnostics["coordinator"]["stale"] is True


def test_entity_uses_entry_identity_and_exposes_stale_attribute() -> None:
    data = coordinator_data(stale=True)
    coordinator = SimpleNamespace(
        data=data,
        last_update_success=True,
        async_add_listener=lambda *_args: lambda: None,
    )
    entry_id = "test-entry-id"
    entity = WindmillClimate(
        coordinator,
        entry_id,
        ClimateEntityDescription(key="climate", name="Windmill AC"),
    )

    assert entity.unique_id == f"{entry_id}-climate"
    assert entity.device_info["identifiers"] == {(DOMAIN, entry_id)}
    assert entity.extra_state_attributes == {"stale": True}
    assert entity.available is True
