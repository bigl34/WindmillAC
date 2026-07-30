"""Tests for the Windmill cloud API client."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from homeassistant.components.climate.const import HVACMode

from custom_components.windmillac.api import (
    WindmillAmbiguousWriteError,
    WindmillApiClient,
    WindmillAuthError,
    WindmillProtocolError,
    WindmillSnapshot,
    WindmillTransientError,
)

TEST_TOKEN = "test-token-never-log"


class FakeResponse:
    """Minimal aiohttp response used by the API tests."""

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

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def json(self, **_kwargs: Any) -> Any:
        if isinstance(self._json_data, Exception):
            raise self._json_data
        return self._json_data

    async def text(self) -> str:
        return self._text_data


class FailingRequest:
    """Request context that fails while opening the response."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    async def __aenter__(self) -> None:
        raise self.error

    async def __aexit__(self, *_args: Any) -> None:
        return None


class BlockingResponse(FakeResponse):
    """Response that holds the client's I/O lock until released."""

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__(json_data=snapshot_payload())
        self.entered = entered
        self.release = release

    async def __aenter__(self) -> BlockingResponse:
        self.entered.set()
        await self.release.wait()
        return self


class FakeSession:
    """Queue-backed aiohttp session fake."""

    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse | FailingRequest:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            return FailingRequest(outcome)
        return outcome


def snapshot_payload() -> dict[str, list[str]]:
    """Return a representative getAll response."""
    return {
        "V0": ["1"],
        "V1": ["74"],
        "V2": ["68"],
        "V3": ["cool"],
        "V4": ["medium"],
    }


@pytest.mark.asyncio
async def test_get_all_builds_an_immutable_validated_snapshot() -> None:
    session = FakeSession([FakeResponse(json_data=snapshot_payload())])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    snapshot = await client.async_get_snapshot()

    assert snapshot == WindmillSnapshot(
        power=True,
        current_temperature=74.0,
        target_temperature=68.0,
        hvac_mode=HVACMode.COOL,
        fan_mode="Medium",
    )
    with pytest.raises(AttributeError):
        snapshot.power = False  # type: ignore[misc]
    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/external/api/getAll")
    assert session.calls[0][1]["timeout"].total == 10


@pytest.mark.asyncio
async def test_get_all_accepts_documented_lowercase_pin_keys() -> None:
    payload = {pin.lower(): value for pin, value in snapshot_payload().items()}
    client = WindmillApiClient(
        FakeSession([FakeResponse(json_data=payload)]),
        TEST_TOKEN,
        retry_delay=0,
    )

    snapshot = await client.async_get_snapshot()

    assert snapshot.target_temperature == 68


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        ["1", "74", "68", "cool", "medium", "unexpected"],
        {**snapshot_payload(), "v0": ["0"]},
    ],
)
async def test_get_all_rejects_ambiguous_extra_required_pin_values(payload: Any) -> None:
    client = WindmillApiClient(
        FakeSession([FakeResponse(json_data=payload)]),
        TEST_TOKEN,
        retry_delay=0,
    )

    with pytest.raises(WindmillProtocolError, match="invalid response"):
        await client.async_get_snapshot()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405])
async def test_get_all_falls_back_to_individual_pins_only_when_unsupported(
    status: int,
) -> None:
    session = FakeSession(
        [
            FakeResponse(status=status),
            FakeResponse(text_data="1"),
            FakeResponse(text_data="74"),
            FakeResponse(text_data="68"),
            FakeResponse(text_data="cool"),
            FakeResponse(text_data="medium"),
        ]
    )
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    snapshot = await client.async_get_snapshot()

    assert snapshot.power is True
    assert len(session.calls) == 6
    assert all(call[0].endswith("/external/api/get") for call in session.calls[1:])


@pytest.mark.asyncio
async def test_get_all_does_not_fallback_for_server_errors() -> None:
    session = FakeSession([FakeResponse(status=500), FakeResponse(json_data=snapshot_payload())])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    snapshot = await client.async_get_snapshot()

    assert snapshot.power is True
    assert len(session.calls) == 2
    assert all(call[0].endswith("/external/api/getAll") for call in session.calls)


@pytest.mark.asyncio
async def test_transient_reads_retry_once() -> None:
    session = FakeSession([TimeoutError(), FakeResponse(json_data=snapshot_payload())])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    assert (await client.async_get_snapshot()).target_temperature == 68
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_second_transient_read_failure_is_classified() -> None:
    session = FakeSession([TimeoutError(), TimeoutError()])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    with pytest.raises(WindmillTransientError, match="temporarily unavailable"):
        await client.async_get_snapshot()
    assert len(session.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_errors_are_not_retried(status: int) -> None:
    session = FakeSession([FakeResponse(status=status)])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    with pytest.raises(WindmillAuthError, match="authentication failed"):
        await client.async_get_snapshot()
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"error": {"message": "Invalid token."}}, WindmillAuthError),
        ({"error": {"message": "Invalid token"}}, WindmillProtocolError),
        ({"error": {"message": TEST_TOKEN}}, WindmillProtocolError),
    ],
)
async def test_read_400_classifies_only_exact_invalid_token_error_as_auth(
    payload: dict[str, Any],
    error_type: type[WindmillAuthError] | type[WindmillProtocolError],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FakeSession([FakeResponse(status=400, json_data=payload)])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    with caplog.at_level(logging.DEBUG), pytest.raises(error_type) as caught:
        await client.async_get_snapshot()

    assert len(session.calls) == 1
    assert TEST_TOKEN not in caplog.text
    assert TEST_TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_malformed_payload_is_a_protocol_error() -> None:
    payload = snapshot_payload()
    payload["V2"] = ["not-a-temperature"]
    client = WindmillApiClient(
        FakeSession([FakeResponse(json_data=payload)]),
        TEST_TOKEN,
        retry_delay=0,
    )

    with pytest.raises(WindmillProtocolError, match="invalid response"):
        await client.async_get_snapshot()


@pytest.mark.asyncio
async def test_writes_are_batched_and_never_retried_when_ambiguous() -> None:
    session = FakeSession([TimeoutError(), FakeResponse(text_data="ok")])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    with pytest.raises(WindmillAmbiguousWriteError, match="outcome is unknown"):
        await client.async_write_pins({"V0": "1", "V3": "1"})

    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/external/api/batch/update")
    assert session.calls[0][1]["params"] == {
        "token": TEST_TOKEN,
        "V0": "1",
        "V3": "1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        ({"error": {"message": "Invalid token."}}, WindmillAuthError),
        ({"error": {"message": "Invalid token"}}, WindmillProtocolError),
        ({"error": {"message": TEST_TOKEN}}, WindmillProtocolError),
    ],
)
async def test_write_400_classifies_only_exact_invalid_token_error_as_auth(
    payload: dict[str, Any],
    error_type: type[WindmillAuthError] | type[WindmillProtocolError],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FakeSession([FakeResponse(status=400, json_data=payload)])
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    with caplog.at_level(logging.DEBUG), pytest.raises(error_type) as caught:
        await client.async_write_pins({"V0": "0"})

    assert len(session.calls) == 1
    assert session.calls[0][0].endswith("/external/api/batch/update")
    assert TEST_TOKEN not in caplog.text
    assert TEST_TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_secret_is_absent_from_logs_and_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = WindmillApiClient(
        FakeSession([FakeResponse(status=401)]),
        TEST_TOKEN,
        retry_delay=0,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(WindmillAuthError) as caught:
        await client.async_get_snapshot()

    assert TEST_TOKEN not in caplog.text
    assert TEST_TOKEN not in str(caught.value)


@pytest.mark.asyncio
async def test_reads_and_writes_are_serialized_by_one_client_lock() -> None:
    read_entered = asyncio.Event()
    release_read = asyncio.Event()
    session = FakeSession(
        [
            BlockingResponse(read_entered, release_read),
            FakeResponse(text_data="ok"),
        ]
    )
    client = WindmillApiClient(session, TEST_TOKEN, retry_delay=0)

    read = asyncio.create_task(client.async_get_snapshot())
    await read_entered.wait()
    write = asyncio.create_task(client.async_write_pins({"V0": "0"}))
    await asyncio.sleep(0)

    assert len(session.calls) == 1
    release_read.set()
    await asyncio.gather(read, write)
    assert len(session.calls) == 2
