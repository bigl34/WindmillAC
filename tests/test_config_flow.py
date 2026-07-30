"""Tests for Windmill configuration flows."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.climate.const import HVACMode
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import Selector
from pytest_homeassistant_custom_component.common import MockConfigEntry
from voluptuous_serialize import UNSUPPORTED, convert

from custom_components.windmillac.api import (
    WindmillAuthError,
    WindmillSnapshot,
    WindmillTransientError,
)
from custom_components.windmillac.const import CONF_TOKEN, DOMAIN

TOKEN_A = "flow-test-token-a"
TOKEN_B = "flow-test-token-b"

SNAPSHOT = WindmillSnapshot(
    power=True,
    current_temperature=74,
    target_temperature=68,
    hvac_mode=HVACMode.COOL,
    fan_mode="Medium",
)


@pytest.mark.asyncio
async def test_user_flow_validates_before_saving(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.windmillac.config_flow.WindmillApiClient.async_get_snapshot",
        AsyncMock(return_value=SNAPSHOT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_TOKEN: TOKEN_A},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_TOKEN: TOKEN_A}
    assert result["title"] == "Windmill AC"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "form_error"),
    [
        (WindmillAuthError("auth"), "invalid_auth"),
        (WindmillTransientError("offline"), "cannot_connect"),
    ],
)
async def test_user_flow_maps_validation_errors(
    hass: HomeAssistant,
    error: Exception,
    form_error: str,
) -> None:
    with patch(
        "custom_components.windmillac.config_flow.WindmillApiClient.async_get_snapshot",
        AsyncMock(side_effect=error),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_TOKEN: TOKEN_A},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": form_error}


@pytest.mark.asyncio
async def test_duplicate_token_is_rejected(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: TOKEN_A}).add_to_hass(hass)

    with patch(
        "custom_components.windmillac.config_flow.WindmillApiClient.async_get_snapshot",
        AsyncMock(return_value=SNAPSHOT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_TOKEN: TOKEN_A},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE],
)
async def test_linked_token_forms_do_not_serialize_the_existing_token(
    hass: HomeAssistant,
    source: str,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: TOKEN_A})
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source, "entry_id": entry.entry_id},
        data=entry.data if source == config_entries.SOURCE_REAUTH else None,
    )

    assert result["type"] is FlowResultType.FORM
    serialized_schema = convert(
        result["data_schema"],
        custom_serializer=lambda value: (
            value.serialize() if isinstance(value, Selector) else UNSUPPORTED
        ),
    )
    serialized_result = json.dumps({"data_schema": serialized_schema})
    assert TOKEN_A not in serialized_result
    assert all("default" not in field for field in serialized_schema)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [config_entries.SOURCE_REAUTH, config_entries.SOURCE_RECONFIGURE],
)
async def test_reauth_and_reconfigure_update_the_same_entry(
    hass: HomeAssistant,
    source: str,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: TOKEN_A})
    entry.add_to_hass(hass)

    with patch(
        "custom_components.windmillac.config_flow.WindmillApiClient.async_get_snapshot",
        AsyncMock(return_value=SNAPSHOT),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": entry.entry_id},
            data=entry.data if source == config_entries.SOURCE_REAUTH else None,
        )
        if result["type"] is FlowResultType.FORM:
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"],
                {CONF_TOKEN: TOKEN_B},
            )

    assert result["type"] is FlowResultType.ABORT
    expected_reason = (
        "reauth_successful" if source == config_entries.SOURCE_REAUTH else "reconfigure_successful"
    )
    assert result["reason"] == expected_reason
    assert entry.data[CONF_TOKEN] == TOKEN_B
    await hass.async_block_till_done()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.asyncio
async def test_reauth_rejects_a_token_owned_by_another_entry(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: TOKEN_A})
    other = MockConfigEntry(domain=DOMAIN, data={CONF_TOKEN: TOKEN_B})
    entry.add_to_hass(hass)
    other.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
        data=entry.data,
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOKEN: TOKEN_B},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "already_configured"}
