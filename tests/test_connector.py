"""Mocked HTTP tests for the ContractGate passed-events connector."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest
import requests

import connector

API_KEY = "cg_test_api_key_do_not_log"
PAYLOAD_SENTINEL = "payload-secret-do-not-log"
CONTRACT_A = "550e8400-e29b-41d4-a716-446655440000"
CONTRACT_B = "contract/b"


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._body = {} if body is None else body
        self.headers = headers or {}
        self.text = json.dumps(self._body)
        self.ok = 200 <= status_code < 300

    def json(self):
        if not isinstance(self._body, dict):
            raise ValueError("not json")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": params, "headers": dict(headers or {}), "timeout": timeout}
        )
        if not self._responses:
            raise AssertionError(f"unexpected request to {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def logs(monkeypatch):
    records = []

    def capture(level):
        def _write(message, *args, **kwargs):
            records.append((level, message))

        return _write

    for level in ("debug", "info", "warning", "error"):
        monkeypatch.setattr(connector.log, level, capture(level))
    return records


@pytest.fixture
def ops(monkeypatch):
    upserts = []
    checkpoints = []

    def upsert(table, data, **kwargs):
        upserts.append((table, copy.deepcopy(data)))

    def checkpoint(state):
        checkpoints.append(copy.deepcopy(state))

    monkeypatch.setattr(connector.op, "upsert", upsert)
    monkeypatch.setattr(connector.op, "checkpoint", checkpoint)
    return upserts, checkpoints


@pytest.fixture
def sleeps(monkeypatch):
    delays = []
    monkeypatch.setattr(connector, "_sleep", delays.append)
    return delays


def config(**overrides):
    base = {
        "api_key": API_KEY,
        "contract_ids": CONTRACT_A,
        "base_url": "https://contractgate-api-staging.fly.dev",
    }
    base.update(overrides)
    return base


def event(event_id="evt-1", payload=None, version="1.2.0", created_at="2026-05-01T12:00:00Z"):
    return {
        "id": event_id,
        "contract_version": version,
        "created_at": created_at,
        "payload": payload,
    }


def page(events, next_cursor, has_more, status=200, headers=None):
    return FakeResponse(
        status,
        {"events": events, "next_cursor": next_cursor, "has_more": has_more},
        headers=headers,
    )


def install_session(monkeypatch, responses):
    session = FakeSession(responses)
    monkeypatch.setattr(connector.requests, "Session", lambda: session)
    return session


def assert_no_secrets(logs, checkpoints):
    blob = json.dumps(logs) + json.dumps(checkpoints, default=str)
    assert API_KEY not in blob
    assert PAYLOAD_SENTINEL not in blob
    for state in checkpoints:
        assert json.dumps(state)
        for cursor in state.get("cursors", {}).values():
            assert isinstance(cursor, str) and cursor
            assert cursor != "null"


def test_configuration_check_rejects_missing_fields():
    missing_key = connector.configuration_check({"contract_ids": CONTRACT_A})
    assert "api_key is required" in missing_key.failure
    assert API_KEY not in missing_key.failure

    blank_ids = connector.configuration_check({"api_key": API_KEY, "contract_ids": " , "})
    assert "empty contract id" in blank_ids.failure
    assert API_KEY not in blank_ids.failure

    with pytest.raises(ValueError, match="api_key is required"):
        connector.schema({"contract_ids": CONTRACT_A})


def test_configuration_check_defaults_base_url_and_schema_tables():
    normalized = connector.validate_configuration(
        {"api_key": f"  {API_KEY}  ", "contract_ids": f"{CONTRACT_A}, {CONTRACT_A}, other"}
    )
    assert normalized["api_key"] == API_KEY
    assert normalized["contract_ids"] == [CONTRACT_A, "other"]
    assert normalized["base_url"] == connector.PROD_BASE_URL

    staged = connector.validate_configuration(
        {
            "api_key": API_KEY,
            "contract_ids": CONTRACT_A,
            "base_url": "https://contractgate-api-staging.fly.dev/",
        }
    )
    assert staged["base_url"] == "https://contractgate-api-staging.fly.dev"

    with pytest.raises(ValueError, match="must not include credentials"):
        connector.validate_configuration(
            {
                "api_key": API_KEY,
                "contract_ids": CONTRACT_A,
                "base_url": "https://user:secret@api.datacontractgate.com",
            }
        )

    tables = connector.schema(
        {"api_key": API_KEY, "contract_ids": f"{CONTRACT_A},{CONTRACT_B}"}
    )
    assert [table["table"] for table in tables] == [
        "passed_550e8400_e29b_41d4_a716_446655440000",
        "passed_contract_b",
    ]
    assert tables[0]["primary_key"] == ["id"]
    assert tables[0]["columns"] == {
        "id": "STRING",
        "contract_version": "STRING",
        "created_at": "UTC_DATETIME",
        "payload": "JSON",
    }
    assert connector.configuration_check(
        {"api_key": API_KEY, "contract_ids": CONTRACT_A}
    ).success


def test_paging_checkpoints_each_page_and_keeps_non_null_final_cursor(
    monkeypatch, logs, ops, sleeps
):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            page(
                [event("evt-1", payload={"note": PAYLOAD_SENTINEL})],
                "cursor-1",
                True,
            ),
            page(
                [event("evt-2", payload={"note": "second"}, created_at="2026-05-01T12:00:01Z")],
                "cursor-2",
                False,
            ),
        ],
    )

    connector.update(config(), {})

    assert len(session.calls) == 2
    assert session.calls[0]["params"] == {"limit": 1000}
    assert "cursor" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["cursor"] == "cursor-1"
    assert session.calls[1]["params"]["limit"] == 1000
    assert session.calls[0]["headers"]["x-api-key"] == API_KEY
    assert session.calls[0]["timeout"] == connector.REQUEST_TIMEOUT
    assert session.calls[0]["url"].endswith(
        "/v1/contracts/550e8400-e29b-41d4-a716-446655440000/passed"
    )
    assert [row[0] for row in upserts] == [
        "passed_550e8400_e29b_41d4_a716_446655440000",
        "passed_550e8400_e29b_41d4_a716_446655440000",
    ]
    assert upserts[0][1]["id"] == "evt-1"
    assert upserts[0][1]["payload"] == {"note": PAYLOAD_SENTINEL}
    assert upserts[0][1]["contract_version"] == "1.2.0"
    assert upserts[0][1]["created_at"] == datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert [state["cursors"][CONTRACT_A] for state in checkpoints] == ["cursor-1", "cursor-2"]
    assert sleeps == []
    assert session.closed
    assert_no_secrets(logs, checkpoints)


def test_null_next_cursor_on_last_page_keeps_last_saved_cursor(monkeypatch, logs, ops, sleeps):
    _upserts, checkpoints = ops
    install_session(
        monkeypatch,
        [
            page([event("evt-1", payload={"note": PAYLOAD_SENTINEL})], "cursor-1", True),
            page([event("evt-2")], None, False),
        ],
    )

    connector.update(config(), {"cursors": {CONTRACT_A: "already-saved"}})

    assert checkpoints[0]["cursors"][CONTRACT_A] == "cursor-1"
    assert checkpoints[1]["cursors"][CONTRACT_A] == "cursor-1"
    assert all(cursor is not None for state in checkpoints for cursor in state["cursors"].values())
    assert_no_secrets(logs, checkpoints)


def test_null_next_cursor_on_only_page_keeps_previously_saved_cursor(monkeypatch, logs, ops):
    _upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [page([event("evt-9", payload=None)], None, False)],
    )

    connector.update(config(), {"cursors": {CONTRACT_A: "keep-me", "other": None}})

    assert session.calls[0]["params"]["cursor"] == "keep-me"
    assert checkpoints[-1]["cursors"] == {CONTRACT_A: "keep-me"}
    assert_no_secrets(logs, checkpoints)


def test_null_payload_is_upserted_as_null(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    install_session(
        monkeypatch,
        [page([event("evt-null", payload=None, version=None)], "cursor-after", False)],
    )

    connector.update(config(), {})

    assert upserts[0][1]["payload"] is None
    assert upserts[0][1]["contract_version"] is None
    assert checkpoints[-1]["cursors"][CONTRACT_A] == "cursor-after"
    assert_no_secrets(logs, checkpoints)


@pytest.mark.parametrize("status", [400, 410])
def test_rejected_cursor_clears_state_and_resyncs(monkeypatch, logs, ops, status):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            FakeResponse(status, {"error": "cursor rejected", "status": status}),
            page([event("evt-full", payload={"note": PAYLOAD_SENTINEL})], "fresh", False),
        ],
    )

    connector.update(
        config(),
        {"cursors": {CONTRACT_A: "expired", CONTRACT_B: "leave-b"}},
    )

    assert session.calls[0]["params"]["cursor"] == "expired"
    assert "cursor" not in session.calls[1]["params"]
    assert checkpoints[0]["cursors"] == {CONTRACT_B: "leave-b"}
    assert checkpoints[-1]["cursors"][CONTRACT_A] == "fresh"
    assert checkpoints[-1]["cursors"][CONTRACT_B] == "leave-b"
    assert upserts[0][1]["id"] == "evt-full"
    assert_no_secrets(logs, checkpoints)


def test_rejected_cursor_without_a_saved_cursor_does_not_loop(monkeypatch, logs, ops):
    _upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [FakeResponse(410, {"error": "gone", "status": 410})],
    )

    with pytest.raises(connector.ContractGateSyncError, match="without a cursor"):
        connector.update(config(), {})

    assert len(session.calls) == 1
    assert checkpoints == []
    assert_no_secrets(logs, checkpoints)


def test_second_cursor_rejection_during_resync_fails(monkeypatch, logs, ops):
    _upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            FakeResponse(410, {"error": "gone", "status": 410}),
            FakeResponse(400, {"error": "bad", "status": 400}),
        ],
    )

    with pytest.raises(connector.ContractGateSyncError, match="after a full re-sync"):
        connector.update(config(), {"cursors": {CONTRACT_A: "expired"}})

    assert len(session.calls) == 2
    assert checkpoints[0]["cursors"] == {}
    assert_no_secrets(logs, checkpoints)


def test_free_plan_403_fails_with_paid_plan_required(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [FakeResponse(403, {"error": "free plan", "status": 403})],
    )

    with pytest.raises(connector.PaidPlanRequiredError, match="paid plan required"):
        connector.update(config(), {"cursors": {CONTRACT_A: "keep"}})

    assert len(session.calls) == 1
    assert upserts == []
    assert checkpoints == []
    assert_no_secrets(logs, checkpoints)


def test_unknown_contract_404(monkeypatch, logs, ops):
    _upserts, _checkpoints = ops
    install_session(monkeypatch, [FakeResponse(404, {"error": "missing", "status": 404})])

    with pytest.raises(connector.UnknownContractError, match="unknown"):
        connector.update(config(), {})

    assert_no_secrets(logs, [])


def test_429_retries_with_backoff_then_succeeds(monkeypatch, logs, ops, sleeps):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            FakeResponse(429, {"error": "slow down", "status": 429}, headers={"Retry-After": "0"}),
            page([event("evt-1", payload={"note": PAYLOAD_SENTINEL})], "cursor-1", False),
        ],
    )

    connector.update(config(), {})

    assert len(session.calls) == 2
    assert sleeps == [0.0]
    assert upserts[0][1]["id"] == "evt-1"
    assert checkpoints[-1]["cursors"][CONTRACT_A] == "cursor-1"
    assert any("HTTP 429" in message for _level, message in logs)
    assert_no_secrets(logs, checkpoints)


def test_5xx_and_timeout_retry(monkeypatch, logs, ops, sleeps):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            FakeResponse(503, {"error": "unavailable", "status": 503}),
            requests.Timeout("timed out"),
            page([event("evt-1")], "cursor-1", False),
        ],
    )

    connector.update(config(), {})

    assert len(session.calls) == 3
    assert sleeps == [1.0, 2.0]
    assert upserts[0][1]["id"] == "evt-1"
    assert checkpoints[-1]["cursors"][CONTRACT_A] == "cursor-1"
    assert_no_secrets(logs, checkpoints)


def test_retries_exhausted_do_not_include_secrets(monkeypatch, logs, ops, sleeps):
    _upserts, checkpoints = ops
    install_session(
        monkeypatch,
        [FakeResponse(429, {"error": PAYLOAD_SENTINEL, "status": 429}) for _ in range(5)],
    )

    with pytest.raises(connector.ContractGateSyncError, match="HTTP 429") as raised:
        connector.update(config(), {})

    assert API_KEY not in str(raised.value)
    assert PAYLOAD_SENTINEL not in str(raised.value)
    assert len(sleeps) == 4
    assert checkpoints == []
    assert_no_secrets(logs, checkpoints)


def test_slash_in_contract_id_is_escaped(monkeypatch, ops, logs):
    _upserts, _checkpoints = ops
    session = install_session(monkeypatch, [page([event("evt-1")], "c", False)])

    connector.update(config(contract_ids=CONTRACT_B), {})

    assert session.calls[0]["url"].endswith("/v1/contracts/contract%2Fb/passed")
