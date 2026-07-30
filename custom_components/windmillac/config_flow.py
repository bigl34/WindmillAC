"""Configuration, reauthentication, and reconfiguration flows."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    WindmillApiClient,
    WindmillAuthError,
    WindmillProtocolError,
    WindmillTransientError,
)
from .const import CONF_TOKEN, DOMAIN


class WindmillConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Windmill integration."""

    VERSION = 2
    MINOR_VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if self._token_is_configured(token):
                return self.async_abort(reason="already_configured")
            if error := await self._async_validation_error(token):
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title="Windmill AC",
                    data={CONF_TOKEN: token},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_token_schema(),
            errors=errors,
        )

    async def async_step_reauth(
        self,
        entry_data: Mapping[str, Any],
    ) -> config_entries.ConfigFlowResult:
        """Start linked reauthentication for the entry that raised the error."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Validate and replace credentials on the same config entry."""
        return await self._async_token_update(
            "reauth_confirm",
            self._get_reauth_entry(),
            user_input,
            "reauth_successful",
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Validate and replace credentials on the same config entry."""
        return await self._async_token_update(
            "reconfigure",
            self._get_reconfigure_entry(),
            user_input,
            "reconfigure_successful",
        )

    async def _async_token_update(
        self,
        step_id: str,
        entry: config_entries.ConfigEntry,
        user_input: dict[str, Any] | None,
        success_reason: str,
    ) -> config_entries.ConfigFlowResult:
        """Share validation and duplicate protection across linked flows."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN].strip()
            if self._token_is_configured(token, exclude_entry_id=entry.entry_id):
                errors["base"] = "already_configured"
            elif error := await self._async_validation_error(token):
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_TOKEN: token},
                    reason=success_reason,
                )

        return self.async_show_form(
            step_id=step_id,
            data_schema=_token_schema(),
            errors=errors,
        )

    def _token_is_configured(
        self,
        token: str,
        *,
        exclude_entry_id: str | None = None,
    ) -> bool:
        """Return whether another entry already owns this exact token."""
        return any(
            entry.entry_id != exclude_entry_id
            and hmac.compare_digest(str(entry.data.get(CONF_TOKEN, "")), token)
            for entry in self._async_current_entries()
        )

    async def _async_validation_error(self, token: str) -> str | None:
        """Validate credentials and map API failures to config-flow errors."""
        if not token:
            return "invalid_auth"
        api = WindmillApiClient(async_get_clientsession(self.hass), token)
        try:
            await api.async_get_snapshot()
        except WindmillAuthError:
            return "invalid_auth"
        except WindmillTransientError:
            return "cannot_connect"
        except WindmillProtocolError:
            return "invalid_response"
        return None


def _token_schema() -> vol.Schema:
    """Build a password selector without ever rendering a token in logs."""
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD),
            )
        }
    )
