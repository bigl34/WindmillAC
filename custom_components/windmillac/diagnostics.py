"""Diagnostics support for WindmillAC."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_TOKEN, DOMAIN

_TO_REDACT = {CONF_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return token-free diagnostics for a Windmill config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    data = coordinator.data
    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), _TO_REDACT),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "consecutive_transient_failures": coordinator.consecutive_transient_failures,
            "stale": data.stale if data is not None else None,
            "snapshot": asdict(data.snapshot) if data is not None else None,
        },
    }
