"""Credential-safety guarantees for the Windmill cloud API client.

Upstream carries these guarantees in `tests/test_blynk_service_logging.py`,
which exercises `blynk_service.py`. This fork replaced that module with
`api.py`, so the upstream test cannot load. These tests port the same three
properties onto the successor module:

1. the module logger never overrides the level Home Assistant configured,
2. no log record carries the device token or a raw response body, and
3. no raised exception exposes the token through its chained traceback.

Property 3 matters because `api.py` deliberately keeps `raise ... from err`
for diagnosability. That is only safe while no chained `aiohttp` error embeds
the credential-bearing URL, which in turn depends on the client never calling
`raise_for_status()` and always reading JSON with `content_type=None`. These
tests pin that invariant so a future refactor cannot silently break it.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

import pytest
from aiohttp import ClientConnectionError

from custom_components.windmillac import api as api_module
from custom_components.windmillac.api import (
    WindmillApiClient,
    WindmillProtocolError,
    WindmillTransientError,
)

TOKEN = "token-sentinel-never-log"
RESPONSE_SECRET = "reflected-response-secret"


class _Response:
    """Minimal aiohttp response double."""

    def __init__(
        self,
        status: int = 200,
        *,
        json_data: Any = None,
        text_data: str = "",
    ) -> None:
        self.status = status
        self._json_data = json_data
        self._text_data = text_data

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self, **_kwargs: Any) -> Any:
        return self._json_data

    async def text(self) -> str:
        return self._text_data


class _FailingRequest:
    """Request context that fails while opening the response."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def __aenter__(self) -> None:
        raise self._error

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _Session:
    """Queue-backed aiohttp session double that records call parameters."""

    def __init__(self, outcomes: list[_Response | Exception]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response | _FailingRequest:
        self.calls.append((url, kwargs))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            return _FailingRequest(outcome)
        return outcome


def _snapshot_payload() -> dict[str, list[str]]:
    """Return a representative getAll response."""
    return {
        "V0": ["1"],
        "V1": ["74"],
        "V2": ["68"],
        "V3": ["cool"],
        "V4": ["medium"],
    }


def test_module_logger_defers_to_home_assistant_configuration() -> None:
    """Importing the client must not pin its own logger level."""
    logger = logging.getLogger(api_module.__name__)

    assert logger.level == logging.NOTSET

    parent = logging.getLogger("custom_components.windmillac")
    previous = parent.level
    try:
        parent.setLevel(logging.WARNING)
        assert logger.getEffectiveLevel() == logging.WARNING
        assert not logger.isEnabledFor(logging.DEBUG)
    finally:
        parent.setLevel(previous)


@pytest.mark.asyncio
async def test_successful_traffic_logs_no_token_and_no_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full read/write cycle at DEBUG must not log credentials or payloads."""
    session = _Session(
        [
            _Response(json_data=_snapshot_payload()),
            _Response(text_data=RESPONSE_SECRET),
        ]
    )
    client = WindmillApiClient(session, TOKEN, retry_delay=0)

    with caplog.at_level(logging.DEBUG):
        await client.async_get_snapshot()
        await client.async_write_pins({"V2": "70"})

    # The token must still reach the wire, just never the log.
    assert all(call[1]["params"]["token"] == TOKEN for call in session.calls)
    assert TOKEN not in caplog.text
    assert RESPONSE_SECRET not in caplog.text


@pytest.mark.asyncio
async def test_transient_retry_diagnostic_is_credential_free(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one debug line this client emits must stay static."""
    session = _Session(
        [
            ClientConnectionError(f"cannot connect using token={TOKEN}"),
            _Response(json_data=_snapshot_payload()),
        ]
    )
    client = WindmillApiClient(session, TOKEN, retry_delay=0)

    with caplog.at_level(logging.DEBUG):
        await client.async_get_snapshot()

    assert "Transient Windmill read failed; retrying once" in caplog.text
    assert TOKEN not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "operation", "error_type"),
    [
        pytest.param(
            [
                ClientConnectionError(f"boom token={TOKEN}"),
                ClientConnectionError(f"boom token={TOKEN}"),
            ],
            "read",
            WindmillTransientError,
            id="read-transport-failure",
        ),
        pytest.param(
            [ClientConnectionError(f"boom token={TOKEN}")],
            "write",
            WindmillTransientError,
            id="write-transport-failure",
        ),
        pytest.param(
            [_Response(json_data={"V0": ["1"]})],
            "read",
            WindmillProtocolError,
            id="malformed-payload",
        ),
    ],
)
async def test_raised_errors_never_expose_the_token_in_a_traceback(
    caplog: pytest.LogCaptureFixture,
    outcomes: list[Any],
    operation: str,
    error_type: type[Exception],
) -> None:
    """`raise ... from err` must not surface credential-bearing context."""
    session = _Session(outcomes)
    client = WindmillApiClient(session, TOKEN, retry_delay=0)

    with caplog.at_level(logging.DEBUG), pytest.raises(error_type) as caught:
        if operation == "read":
            await client.async_get_snapshot()
        else:
            await client.async_write_pins({"V2": "70"})

    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert TOKEN not in str(caught.value)
    assert TOKEN not in caplog.text


def test_client_never_delegates_status_handling_to_aiohttp() -> None:
    """`raise_for_status()` would chain a ClientResponseError carrying the URL.

    The redaction guarantee above depends on this client classifying statuses
    itself, so assert the unsafe delegation never reappears.
    """
    source = api_module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        body = handle.read()

    assert "raise_for_status" not in body
    # `json()` without content_type=None can raise ContentTypeError, which is a
    # ClientResponseError subclass and renders the credential-bearing URL.
    assert body.count("response.json(") == body.count("response.json(content_type=None)")


def test_asyncio_event_loop_is_available_for_the_parametrized_cases() -> None:
    """Guard against an environment without a usable event loop policy."""
    loop = asyncio.new_event_loop()
    try:
        assert not loop.is_closed()
    finally:
        loop.close()
