"""Tests for config-entry v2 registry identity migration."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.windmillac import async_migrate_entry
from custom_components.windmillac.const import CONF_TOKEN, DOMAIN


def add_v1_entry(hass: HomeAssistant, token: str) -> MockConfigEntry:
    """Add a version-one entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: token}, version=1)
    entry.add_to_hass(hass)
    return entry


def add_legacy_registry_rows(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    entity_id: str,
) -> tuple[er.RegistryEntry, dr.DeviceEntry]:
    """Create the exact registry rows written by integration version one."""
    old_unique_id = f"{DOMAIN}_{entry.data[CONF_TOKEN]}_windmill_AC"
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, old_unique_id)},
        name="Windmill AC",
        manufacturer="Windmill",
    )
    entity_registry = er.async_get(hass)
    entity = entity_registry.async_get_or_create(
        "climate",
        DOMAIN,
        old_unique_id,
        config_entry=entry,
        device_id=device.id,
        suggested_object_id=entity_id.removeprefix("climate."),
        original_name="Windmill AC",
    )
    return entity, device


@pytest.mark.asyncio
async def test_two_entries_migrate_in_place_during_the_same_restart(
    hass: HomeAssistant,
) -> None:
    entry_a = add_v1_entry(hass, "migration-token-a")
    entry_b = add_v1_entry(hass, "migration-token-b")
    old_entity_a, old_device_a = add_legacy_registry_rows(
        hass,
        entry_a,
        entity_id="climate.living_room_ac",
    )
    old_entity_b, old_device_b = add_legacy_registry_rows(
        hass,
        entry_b,
        entity_id="climate.bedroom_ac",
    )
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    entity_registry.async_update_entity(
        old_entity_a.entity_id,
        area_id="living_room",
        name="Downstairs AC",
    )
    device_registry.async_update_device(
        old_device_a.id,
        area_id="living_room",
        name_by_user="Downstairs Windmill",
    )

    assert await async_migrate_entry(hass, entry_a) is True
    assert await async_migrate_entry(hass, entry_b) is True

    migrated_a = entity_registry.async_get(old_entity_a.entity_id)
    migrated_b = entity_registry.async_get(old_entity_b.entity_id)
    assert migrated_a is not None and migrated_a.unique_id == f"{entry_a.entry_id}-climate"
    assert migrated_b is not None and migrated_b.unique_id == f"{entry_b.entry_id}-climate"
    assert migrated_a.id == old_entity_a.id
    assert migrated_b.id == old_entity_b.id
    assert migrated_a.device_id == old_device_a.id
    assert migrated_b.device_id == old_device_b.id
    assert migrated_a.area_id == "living_room"
    assert migrated_a.name == "Downstairs AC"
    assert device_registry.async_get(old_device_a.id).name_by_user == "Downstairs Windmill"
    assert (
        device_registry.async_get_device(identifiers={(DOMAIN, entry_a.entry_id)}).id
        == old_device_a.id
    )


@pytest.mark.asyncio
async def test_migration_fails_closed_on_entity_collision(hass: HomeAssistant) -> None:
    entry = add_v1_entry(hass, "collision-token")
    legacy_entity, _ = add_legacy_registry_rows(
        hass,
        entry,
        entity_id="climate.windmill_ac",
    )
    other_entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: "other-token"})
    other_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "climate",
        DOMAIN,
        f"{entry.entry_id}-climate",
        config_entry=other_entry,
        suggested_object_id="conflict",
    )

    assert await async_migrate_entry(hass, entry) is False
    assert er.async_get(hass).async_get(legacy_entity.entity_id).unique_id.endswith("_windmill_AC")
    assert entry.version == 1


@pytest.mark.asyncio
async def test_migration_fails_closed_on_device_collision(hass: HomeAssistant) -> None:
    entry = add_v1_entry(hass, "device-collision-token")
    legacy_entity, legacy_device = add_legacy_registry_rows(
        hass,
        entry,
        entity_id="climate.windmill_ac",
    )
    other_entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: "other-token"})
    other_entry.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name="Conflicting device",
    )

    assert await async_migrate_entry(hass, entry) is False
    assert er.async_get(hass).async_get(legacy_entity.entity_id).unique_id.endswith(
        "_windmill_AC"
    )
    assert dr.async_get(hass).async_get(legacy_device.id).identifiers != {
        (DOMAIN, entry.entry_id)
    }
    assert entry.version == 1


@pytest.mark.asyncio
async def test_migration_rolls_back_entity_when_device_update_fails(
    hass: HomeAssistant,
) -> None:
    entry = add_v1_entry(hass, "rollback-token")
    legacy_entity, legacy_device = add_legacy_registry_rows(
        hass,
        entry,
        entity_id="climate.windmill_ac",
    )
    device_registry = dr.async_get(hass)
    original_update = device_registry.async_update_device

    def fail_new_identifier(device_id: str, **kwargs):
        if kwargs.get("new_identifiers") == {(DOMAIN, entry.entry_id)}:
            raise RuntimeError("simulated registry failure")
        return original_update(device_id, **kwargs)

    with patch.object(device_registry, "async_update_device", side_effect=fail_new_identifier):
        assert await async_migrate_entry(hass, entry) is False

    assert er.async_get(hass).async_get(legacy_entity.entity_id).unique_id.endswith("_windmill_AC")
    assert device_registry.async_get(legacy_device.id).identifiers == {
        (DOMAIN, f"{DOMAIN}_{entry.data[CONF_TOKEN]}_windmill_AC")
    }
    assert entry.version == 1


@pytest.mark.asyncio
async def test_completed_migration_is_idempotent(hass: HomeAssistant) -> None:
    entry = add_v1_entry(hass, "idempotent-token")
    entity, device = add_legacy_registry_rows(
        hass,
        entry,
        entity_id="climate.windmill_ac",
    )

    assert await async_migrate_entry(hass, entry) is True
    assert await async_migrate_entry(hass, entry) is True
    assert er.async_get(hass).async_get(entity.entity_id).id == entity.id
    assert dr.async_get(hass).async_get(device.id).id == device.id
