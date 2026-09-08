"""Asynchronous client for the Windmill cloud API."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from aiohttp import ClientError, ClientResponse, ClientSession, ClientTimeout
from homeassistant.components.climate.const import HVACMode

from .const import BASE_URL

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT: Final = ClientTimeout(total=10)
_TRANSIENT_STATUS_CODES: Final = {408, 429, 500, 502, 503, 504}
_REQUIRED_PINS: Final = ("V0", "V1", "V2", "V3", "V4")
_INVALID_TOKEN_RESPONSE: Final = {"error": {"message": "Invalid token."}}
_FAN_MODES: Final = {
    "0": "Auto",
    "auto": "Auto",
    "1": "Low",
    "low": "Low",
    "2": "Medium",
    "medium": "Medium",
    "3": "High",
    "high": "High",
}
_HVAC_MODES: Final = {
    "0": HVACMode.FAN_ONLY,
    "fan": HVACMode.FAN_ONLY,
    "fan_only": HVACMode.FAN_ONLY,
    "1": HVACMode.COOL,
    "cool": HVACMode.COOL,
    "2": HVACMode.AUTO,
    "auto": HVACMode.AUTO,
    "eco": HVACMode.AUTO,
}
HVAC_MODE_TO_PIN: Final = {
    HVACMode.FAN_ONLY: "0",
    HVACMode.COOL: "1",
    HVACMode.AUTO: "2",
}
FAN_MODE_TO_PIN: Final = {
    "Auto": "0",
    "Low": "1",
    "Medium": "2",
    "High": "3",
}


class WindmillError(Exception):
    """Base error for the Windmill client."""


class WindmillTransientError(WindmillError):
    """The cloud service may recover without user action."""


class WindmillAuthError(WindmillError):
    """The supplied device token was rejected."""


class WindmillProtocolError(WindmillError):
    """The cloud service returned a response the integration cannot use."""


class WindmillAmbiguousWriteError(WindmillTransientError):
    """A write may have reached the appliance, so it must not be retried."""


class _GetAllUnsupported(WindmillProtocolError):
    """The account endpoint does not support getAll."""


@dataclass(frozen=True, slots=True)
class WindmillSnapshot:
    """A validated, immutable view of a Windmill appliance."""

    power: bool
    current_temperature: float
    target_temperature: float
    hvac_mode: HVACMode
    fan_mode: str


class WindmillApiClient:
    """Talk to the Windmill Blynk-compatible cloud endpoint."""

    def __init__(
        self,
        session: ClientSession,
        token: str,
        *,
        base_url: str = BASE_URL,
        retry_delay: float = 0.5,
    ) -> None:
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._retry_delay = retry_delay
        self._io_lock = asyncio.Lock()

    async def async_get_snapshot(self) -> WindmillSnapshot:
        """Read and validate all state, serializing against writes."""
        async with self._io_lock:
            try:
                payload = await self._async_read_json("external/api/getAll")
                pins = self._normalize_get_all(payload)
            except _GetAllUnsupported:
                pins = {
                    pin: await self._async_read_text(
                        "external/api/get",
                        extra_params={pin: ""},
                    )
                    for pin in ("V0", "V1", "V2", "V3", "V4")
                }

            return self._snapshot_from_pins(pins)

    async def async_write_pins(self, values: Mapping[str, str]) -> None:
        """Write one or more pins in a single non-retriable request."""
        if not values:
            return
        async with self._io_lock:
            params = {"token": self._token, **values}
            try:
                async with self._session.get(
                    f"{self._base_url}/external/api/batch/update",
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    await self._async_raise_for_response(
                        response,
                        allow_get_all_fallback=False,
                    )
                    await response.text()
            except WindmillAuthError:
                raise
            except (TimeoutError, ClientError, WindmillTransientError) as err:
                # Chain the transport error's type but never its message: an
                # aiohttp error can render the credential-bearing request URL,
                # and Home Assistant logs the full traceback.
                raise WindmillAmbiguousWriteError(
                    "Windmill command outcome is unknown; it was not retried"
                    f" ({type(err).__name__})"
                ) from None

    async def _async_read_json(self, endpoint: str) -> Any:
        """Read JSON with one retry for transient failures."""
        return await self._async_read(endpoint, response_kind="json")

    async def _async_read_text(
        self,
        endpoint: str,
        *,
        extra_params: Mapping[str, str] | None = None,
    ) -> str:
        """Read text with one retry for transient failures."""
        result = await self._async_read(
            endpoint,
            response_kind="text",
            extra_params=extra_params,
        )
        if not isinstance(result, str):
            raise WindmillProtocolError("Windmill returned an invalid response")
        return result.strip()

    async def _async_read(
        self,
        endpoint: str,
        *,
        response_kind: str,
        extra_params: Mapping[str, str] | None = None,
    ) -> Any:
        """Perform a redacted read, retrying one transient failure."""
        params = {"token": self._token, **(extra_params or {})}
        for attempt in range(2):
            try:
                async with self._session.get(
                    f"{self._base_url}/{endpoint}",
                    params=params,
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    await self._async_raise_for_response(
                        response,
                        allow_get_all_fallback=endpoint.endswith("/getAll"),
                    )
                    if response_kind == "json":
                        try:
                            return await response.json(content_type=None)
                        except (TypeError, ValueError) as err:
                            raise WindmillProtocolError(
                                "Windmill returned an invalid response"
                            ) from err
                    return await response.text()
            except (WindmillAuthError, WindmillProtocolError, _GetAllUnsupported):
                raise
            except (TimeoutError, ClientError, WindmillTransientError) as err:
                if attempt == 0:
                    _LOGGER.debug("Transient Windmill read failed; retrying once")
                    await asyncio.sleep(self._retry_delay)
                    continue
                # See async_write_pins: the transport error's message may embed
                # the token, so only its type survives into the raised error.
                raise WindmillTransientError(
                    "Windmill service is temporarily unavailable"
                    f" ({type(err).__name__})"
                ) from None

        raise AssertionError("read retry loop terminated unexpectedly")

    @staticmethod
    async def _async_raise_for_response(
        response: ClientResponse,
        *,
        allow_get_all_fallback: bool,
    ) -> None:
        """Classify a response without exposing credential-bearing details."""
        status = response.status
        if status == 200:
            return
        if status in (401, 403):
            raise WindmillAuthError("Windmill authentication failed")
        if status == 400:
            try:
                payload = await response.json(content_type=None)
            except (TimeoutError, ClientError, TypeError, ValueError):
                payload = None
            if payload == _INVALID_TOKEN_RESPONSE:
                raise WindmillAuthError("Windmill authentication failed")
            raise WindmillProtocolError("Windmill returned an invalid request response")
        if allow_get_all_fallback and status in (404, 405):
            raise _GetAllUnsupported("Windmill getAll endpoint is unsupported")
        if status in _TRANSIENT_STATUS_CODES or status >= 500:
            raise WindmillTransientError("Windmill service is temporarily unavailable")
        raise WindmillProtocolError(f"Windmill returned unexpected HTTP status {status}")

    @staticmethod
    def _normalize_get_all(payload: Any) -> dict[str, Any]:
        """Normalize known Blynk getAll response shapes."""
        if isinstance(payload, list):
            if len(payload) != len(_REQUIRED_PINS):
                raise WindmillProtocolError("Windmill returned an invalid response")
            pin_values = dict(zip(_REQUIRED_PINS, payload, strict=True))
        elif isinstance(payload, dict):
            pin_values = {}
            for raw_pin, value in payload.items():
                if not isinstance(raw_pin, str):
                    continue
                pin = raw_pin.upper()
                if pin not in _REQUIRED_PINS:
                    continue
                if pin in pin_values:
                    raise WindmillProtocolError("Windmill returned an invalid response")
                pin_values[pin] = value
            if set(pin_values) != set(_REQUIRED_PINS):
                raise WindmillProtocolError("Windmill returned an incomplete response")
        else:
            raise WindmillProtocolError("Windmill returned an invalid response")

        normalized: dict[str, Any] = {}
        for pin, value in pin_values.items():
            if isinstance(value, list):
                if len(value) != 1:
                    raise WindmillProtocolError("Windmill returned an invalid response")
                value = value[0]
            normalized[pin] = value
        return normalized

    @staticmethod
    def _snapshot_from_pins(pins: Mapping[str, Any]) -> WindmillSnapshot:
        """Validate raw pins before publishing state to Home Assistant."""
        try:
            power_raw = str(pins["V0"]).strip().lower()
            if power_raw not in {"0", "1", "false", "true"}:
                raise ValueError
            power = power_raw in {"1", "true"}

            current_temperature = float(pins["V1"])
            target_temperature = float(pins["V2"])
            if not -40 <= current_temperature <= 140:
                raise ValueError
            if not 50 <= target_temperature <= 100:
                raise ValueError

            mode_raw = str(pins["V3"]).strip().lower()
            if mode_raw not in _HVAC_MODES:
                raise ValueError
            hvac_mode = _HVAC_MODES[mode_raw] if power else HVACMode.OFF

            fan_raw = str(pins["V4"]).strip().lower()
            if fan_raw not in _FAN_MODES:
                raise ValueError
            fan_mode = _FAN_MODES[fan_raw]
        except (KeyError, TypeError, ValueError) as err:
            raise WindmillProtocolError("Windmill returned an invalid response") from err

        return WindmillSnapshot(
            power=power,
            current_temperature=current_temperature,
            target_temperature=target_temperature,
            hvac_mode=hvac_mode,
            fan_mode=fan_mode,
        )
