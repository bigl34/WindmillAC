"""Tests for Windmill coordinator resilience and command ordering."""

from __future__ import annotations

import asyncio
from collections import deque
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.climate.const import HVACMode
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.windmillac.api import (
    WindmillAmbiguousWriteError,
    WindmillAuthError,
    WindmillProtocolError,
    WindmillSnapshot,
    WindmillTransientError,
)
from custom_components.windmillac.const import CONF_TOKEN, DOMAIN
from custom_components.windmillac.coordinator import (
    WindmillCoordinatorData,
    WindmillDataUpdateCoordinator,
)


def make_snapshot(*, power: bool = True, target: float = 68) -> WindmillSnapshot:
    """Create a valid immutable snapshot."""
    return WindmillSnapshot(
        power=power,
        current_temperature=74,
        target_temperature=target,
        hvac_mode=HVACMode.COOL if power else HVACMode.OFF,
        fan_mode="Medium",
    )


class FakeApi:
    """Queue-backed API fake."""

    def __init__(
        self,
        outcomes: list[WindmillSnapshot | Exception],
        *,
        write_error: Exception | None = None,
    ) -> None:
        self.outcomes = deque(outcomes)
        self.writes: list[dict[str, str]] = []
        self.write_error = write_error

    async def async_get_snapshot(self) -> WindmillSnapshot:
        outcome = self.outcomes.popleft()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def async_write_pins(self, values: dict[str, str]) -> None:
        self.writes.append(values)
        if self.write_error is not None:
            raise self.write_error


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """Create the owner for each coordinator under test."""
    return MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: "coordinator-test-token"})


def test_coordinator_is_bound_to_its_entry_and_suppresses_equal_updates(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    coordinator = WindmillDataUpdateCoordinator(hass, config_entry, FakeApi([]))

    assert coordinator.config_entry is config_entry
    assert coordinator.always_update is False


@pytest.mark.asyncio
async def test_startup_transient_failure_raises_for_normal_setup_retry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    coordinator = WindmillDataUpdateCoordinator(
        hass,
        config_entry,
        FakeApi([WindmillTransientError("temporary")]),
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_first_transient_after_success_uses_stale_cache_then_second_fails(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    snapshot = make_snapshot()
    coordinator = WindmillDataUpdateCoordinator(
        hass,
        config_entry,
        FakeApi(
            [
                snapshot,
                WindmillTransientError("temporary"),
                WindmillTransientError("temporary"),
            ]
        ),
    )

    fresh = await coordinator._async_update_data()
    coordinator.async_set_updated_data(fresh)
    assert fresh == WindmillCoordinatorData(snapshot, False)
    assert await coordinator._async_update_data() == WindmillCoordinatorData(snapshot, True)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


@pytest.mark.asyncio
async def test_auth_failure_is_immediate_and_protocol_failure_is_not_reauth(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    auth = WindmillDataUpdateCoordinator(
        hass,
        config_entry,
        FakeApi([WindmillAuthError("auth")]),
    )
    protocol = WindmillDataUpdateCoordinator(
        hass,
        config_entry,
        FakeApi([WindmillProtocolError("bad payload")]),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await auth._async_update_data()
    with pytest.raises(UpdateFailed):
        await protocol._async_update_data()


@pytest.mark.asyncio
async def test_success_resets_transient_failure_counter(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
) -> None:
    first = make_snapshot(target=68)
    recovered = make_snapshot(target=70)
    api = FakeApi(
        [
            first,
            WindmillTransientError("temporary"),
            recovered,
            WindmillTransientError("temporary"),
        ]
    )
    coordinator = WindmillDataUpdateCoordinator(hass, config_entry, api)

    fresh = await coordinator._async_update_data()
    coordinator.async_set_updated_data(fresh)
    assert (await coordinator._async_update_data()).stale is True
    recovered_data = await coordinator._async_update_data()
    coordinator.async_set_updated_data(recovered_data)
    assert recovered_data == WindmillCoordinatorData(recovered, False)
    assert (await coordinator._async_update_data()).stale is True


@pytest.mark.asyncio
async def test_power_on_and_mode_are_sent_in_one_write_and_refreshed_afterward(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi([make_snapshot(power=False)])
    coordinator = WindmillDataUpdateCoordinator(hass, config_entry, api)
    coordinator.async_set_updated_data(WindmillCoordinatorData(make_snapshot(power=False), False))
    refresh_called = asyncio.Event()

    async def fake_refresh() -> None:
        refresh_called.set()

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    await coordinator.async_set_hvac_mode(HVACMode.COOL)

    assert api.writes == [{"V0": "1", "V3": "1"}]
    assert refresh_called.is_set()


@pytest.mark.asyncio
async def test_older_refresh_cannot_overwrite_a_later_command(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_snapshot = make_snapshot(target=68)
    current_snapshot = make_snapshot(target=72)
    read_started = asyncio.Event()
    release_read = asyncio.Event()

    class OrderedApi(FakeApi):
        async def async_get_snapshot(self) -> WindmillSnapshot:
            read_started.set()
            await release_read.wait()
            return old_snapshot

    api = OrderedApi([])
    coordinator = WindmillDataUpdateCoordinator(hass, config_entry, api)
    coordinator.async_set_updated_data(WindmillCoordinatorData(current_snapshot, False))

    async def fake_refresh() -> None:
        return None

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)
    pending_read = asyncio.create_task(coordinator._async_update_data())
    await read_started.wait()

    command = asyncio.create_task(coordinator.async_set_target_temperature(72))
    await asyncio.sleep(0)
    release_read.set()

    assert await pending_read == WindmillCoordinatorData(current_snapshot, False)
    await command
    assert api.writes == [{"V2": "72"}]


@pytest.mark.asyncio
async def test_fractional_native_temperature_is_rounded_for_integer_pin(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi([])
    coordinator = WindmillDataUpdateCoordinator(hass, config_entry, api)
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)

    await coordinator.async_set_target_temperature(60.98)

    assert api.writes == [{"V2": "61"}]
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_auth_failure_requests_linked_reauthentication(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = WindmillDataUpdateCoordinator(
        hass,
        config_entry,
        FakeApi([], write_error=WindmillAuthError("auth")),
    )
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_set_power(False)

    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_write_refreshes_state_without_retrying(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi([], write_error=WindmillAmbiguousWriteError("unknown"))
    coordinator = WindmillDataUpdateCoordinator(hass, config_entry, api)
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)

    with pytest.raises(WindmillAmbiguousWriteError):
        await coordinator.async_set_power(False)

    assert api.writes == [{"V0": "0"}]
    refresh.assert_awaited_once()
