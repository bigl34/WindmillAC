"""WindmillAC integration setup and registry migration."""

from __future__ import annotations

import logging

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WindmillApiClient
from .const import CONF_TOKEN, DOMAIN, PLATFORMS
from .coordinator import WindmillDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Windmill AC from a config entry."""
    api = WindmillApiClient(
        async_get_clientsession(hass),
        entry.data[CONF_TOKEN],
    )
    coordinator = WindmillDataUpdateCoordinator(hass, entry, api)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}

    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate token-derived registry identities to stable config-entry identities."""
    if entry.version >= 2:
        return True
    if entry.version != 1:
        _LOGGER.error("Cannot migrate unsupported Windmill config entry version %s", entry.version)
        return False

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    old_unique_id = f"{DOMAIN}_{entry.data[CONF_TOKEN]}_windmill_AC"
    new_unique_id = f"{entry.entry_id}-climate"
    old_identifier = (DOMAIN, old_unique_id)
    new_identifier = (DOMAIN, entry.entry_id)

    old_entity_id = entity_registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        DOMAIN,
        old_unique_id,
    )
    new_entity_id = entity_registry.async_get_entity_id(
        CLIMATE_DOMAIN,
        DOMAIN,
        new_unique_id,
    )
    if old_entity_id and new_entity_id and old_entity_id != new_entity_id:
        _LOGGER.error("Windmill entity registry collision prevents config entry migration")
        return False
    entity_id = old_entity_id or new_entity_id
    entity_row = entity_registry.async_get(entity_id) if entity_id else None
    if entity_row is not None and entity_row.config_entry_id != entry.entry_id:
        _LOGGER.error("Windmill entity registry ownership prevents config entry migration")
        return False

    old_device = device_registry.async_get_device(identifiers={old_identifier})
    new_device = device_registry.async_get_device(identifiers={new_identifier})
    if old_device and new_device and old_device.id != new_device.id:
        _LOGGER.error("Windmill device registry collision prevents config entry migration")
        return False
    device_row = old_device or new_device
    if device_row is not None and entry.entry_id not in device_row.config_entries:
        _LOGGER.error("Windmill device registry ownership prevents config entry migration")
        return False

    device = device_row
    entity_changed = False
    device_changed = False
    try:
        if old_entity_id and old_entity_id != new_entity_id:
            entity_registry.async_update_entity(
                old_entity_id,
                new_unique_id=new_unique_id,
            )
            entity_changed = True
        if old_device and old_device.identifiers != {new_identifier}:
            device_registry.async_update_device(
                old_device.id,
                new_identifiers={new_identifier},
            )
            device_changed = True
        hass.config_entries.async_update_entry(entry, version=2, minor_version=1)
    except Exception:
        _LOGGER.exception("Unexpected registry failure while migrating a Windmill config entry")
        if device_changed and device is not None:
            try:
                device_registry.async_update_device(
                    device.id,
                    new_identifiers={old_identifier},
                )
            except Exception:
                _LOGGER.exception("Failed to roll back Windmill device registry migration")
        if entity_changed and entity_id is not None:
            try:
                entity_registry.async_update_entity(
                    entity_id,
                    new_unique_id=old_unique_id,
                )
            except Exception:
                _LOGGER.exception("Failed to roll back Windmill entity registry migration")
        return False

    return True
