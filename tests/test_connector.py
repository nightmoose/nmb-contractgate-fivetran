"""Mocked HTTP tests for the DataContractGate passed-events connector."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone

import pytest
import requests

import connector

API_KEY = "cg_test_api_key_do_not_log"
PAYLOAD_SENTINEL = "payload-secret-do-not-log"
CONTRACT_A = "550e8400-e29b-41d4-a716-446655440000"
CONTRACT_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


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
        "base_url": "https://staging.example.com",
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


def page(events, next_cursor, has_more, status=200, headers=None, include_next_cursor=True):
    body = {"events": events, "has_more": has_more}
    if include_next_cursor:
        body["next_cursor"] = next_cursor
    return FakeResponse(status, body, headers=headers)


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
        {
            "api_key": f"  {API_KEY}  ",
            "contract_ids": f"{CONTRACT_A}, {CONTRACT_A.upper()}, {CONTRACT_B}",
        }
    )
    assert normalized["api_key"] == API_KEY
    assert normalized["contract_ids"] == [CONTRACT_A, CONTRACT_B]
    assert normalized["base_url"] == connector.PROD_BASE_URL

    staged = connector.validate_configuration(
        {
            "api_key": API_KEY,
            "contract_ids": CONTRACT_A,
            "base_url": "https://staging.example.com/",
        }
    )
    assert staged["base_url"] == "https://staging.example.com"

    with pytest.raises(ValueError, match="must not include credentials"):
        connector.validate_configuration(
            {
                "api_key": API_KEY,
                "contract_ids": CONTRACT_A,
                "base_url": "https://user:secret@api.datacontractgate.com",
            }
        )

    tables = connector.schema({"api_key": API_KEY, "contract_ids": f"{CONTRACT_A},{CONTRACT_B}"})
    assert [table["table"] for table in tables] == [
        "passed_550e8400_e29b_41d4_a716_446655440000",
        "passed_6ba7b810_9dad_11d1_80b4_00c04fd430c8",
    ]
    assert tables[0]["primary_key"] == ["id"]
    assert tables[0]["columns"] == {
        "id": "STRING",
        "contract_version": "STRING",
        "created_at": "UTC_DATETIME",
        "payload": "JSON",
    }
    assert connector.configuration_check({"api_key": API_KEY, "contract_ids": CONTRACT_A}).success
    assert connector.validate_configuration(config())["table_names"] == {}


def test_base_url_requires_https_except_local_http(monkeypatch, ops):
    upserts, _checkpoints = ops
    for base_url in ("http://127.0.0.1:8765", "http://localhost:8765/", "http://LOCALHOST"):
        normalized = connector.validate_configuration(
            {"api_key": API_KEY, "contract_ids": CONTRACT_A, "base_url": base_url}
        )
        assert normalized["base_url"].startswith("http://")
        checked = connector.configuration_check(
            {"api_key": API_KEY, "contract_ids": CONTRACT_A, "base_url": base_url}
        )
        assert checked.success

    rejected = connector.configuration_check(
        {
            "api_key": API_KEY,
            "contract_ids": CONTRACT_A,
            "base_url": "http://example.com",
        }
    )
    assert "https://" in rejected.failure
    assert "localhost" in rejected.failure
    assert "127.0.0.1" in rejected.failure

    for base_url in ("http://10.0.0.1", "http://127.0.0.1.example.com", "ftp://localhost"):
        with pytest.raises(ValueError, match="https"):
            connector.validate_configuration(
                {"api_key": API_KEY, "contract_ids": CONTRACT_A, "base_url": base_url}
            )

    session = install_session(monkeypatch, [page([event("evt-1")], "cursor-1", False)])
    connector.update(config(base_url="http://127.0.0.1:8765"), {})
    assert session.calls[0]["url"].startswith("http://127.0.0.1:8765/v1/contracts/")
    assert upserts[0][1]["id"] == "evt-1"


def test_table_names_override_is_optional_and_used_for_upserts(monkeypatch, ops):
    upserts, _checkpoints = ops
    mapping = f"{CONTRACT_A}=passed_orders"
    tables = connector.schema(
        config(contract_ids=f"{CONTRACT_A},{CONTRACT_B}", table_names=mapping)
    )
    assert [table["table"] for table in tables] == [
        "passed_orders",
        "passed_6ba7b810_9dad_11d1_80b4_00c04fd430c8",
    ]

    install_session(monkeypatch, [page([event("evt-1")], "cursor-1", False)])
    connector.update(config(table_names=mapping), {})
    assert upserts[0][0] == "passed_orders"

    blank = connector.validate_configuration(config(table_names="  "))
    assert blank["table_names"] == {}
    assert connector.table_name_for(CONTRACT_A) == "passed_550e8400_e29b_41d4_a716_446655440000"


def test_table_names_validation_names_the_problem():
    bad_name = connector.configuration_check(config(table_names=f"{CONTRACT_A}=Orders"))
    assert "Orders" in bad_name.failure
    assert "lowercase" in bad_name.failure

    unknown = "6ba7b811-9dad-11d1-80b4-00c04fd430c8"
    missing = connector.configuration_check(config(table_names=f"{unknown}=passed_orders"))
    assert unknown in missing.failure
    assert "contract_ids" in missing.failure

    not_uuid = connector.configuration_check(config(table_names="contract/b=passed_orders"))
    assert "contract/b" in not_uuid.failure
    assert "UUID" in not_uuid.failure

    malformed = connector.configuration_check(config(table_names="not-a-pair"))
    assert "not-a-pair" in malformed.failure

    duplicate = connector.configuration_check(
        config(table_names=f"{CONTRACT_A}=passed_a,{CONTRACT_A.upper()}=passed_b")
    )
    assert CONTRACT_A in duplicate.failure
    assert "more than once" in duplicate.failure

    collision = connector.configuration_check(
        config(
            contract_ids=f"{CONTRACT_A},{CONTRACT_B}",
            table_names=f"{CONTRACT_A}=passed_orders,{CONTRACT_B}=passed_orders",
        )
    )
    assert "passed_orders" in collision.failure

    default_a = "passed_550e8400_e29b_41d4_a716_446655440000"
    shadowed = connector.configuration_check(
        config(
            contract_ids=f"{CONTRACT_A},{CONTRACT_B}",
            table_names=f"{CONTRACT_B}={default_a}",
        )
    )
    assert default_a in shadowed.failure
    assert CONTRACT_A in shadowed.failure


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


# Published passed-events example: created_at 2026-10-16T12:00:00.123456Z and this id.
EVENT_UUID = "550e8400-e29b-41d4-a716-446655440000"
RESUME_CURSOR = "MTc5MjE1MjAwMDEyMzQ1Niw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
EARLIER_EVENT_UUID = "6ba7b811-9dad-11d1-80b4-00c04fd430c8"


def test_cursor_from_last_event_matches_api_high_water_mark():
    encoded = connector._cursor_from_event(
        event(EVENT_UUID.upper(), created_at="2026-10-16T12:00:00.123456Z")
    )
    assert encoded == RESUME_CURSOR
    earlier = connector._cursor_from_event(
        event(EARLIER_EVENT_UUID, created_at="2026-05-01T12:00:00Z")
    )
    assert earlier != RESUME_CURSOR
    assert connector._cursor_from_event(event("evt-1")) is None


def test_final_page_null_next_cursor_saves_last_event_resume_cursor(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            page([event("evt-1", payload={"note": PAYLOAD_SENTINEL})], "cursor-1", True),
            page(
                [
                    event(EARLIER_EVENT_UUID, created_at="2026-05-01T12:00:00Z"),
                    event(EVENT_UUID, created_at="2026-10-16T12:00:00.123456Z"),
                ],
                None,
                False,
            ),
            page([], None, False),
        ],
    )

    connector.update(config(), {"cursors": {CONTRACT_B: "leave-b"}})

    assert [row[1]["id"] for row in upserts] == ["evt-1", EARLIER_EVENT_UUID, EVENT_UUID]
    assert checkpoints[0]["cursors"][CONTRACT_A] == "cursor-1"
    assert checkpoints[1]["cursors"] == {CONTRACT_B: "leave-b", CONTRACT_A: RESUME_CURSOR}
    assert all(cursor is not None for state in checkpoints for cursor in state["cursors"].values())

    upserts.clear()
    connector.update(config(), checkpoints[-1])

    assert session.calls[-1]["params"]["cursor"] == RESUME_CURSOR
    assert upserts == []
    assert checkpoints[-1]["cursors"][CONTRACT_A] == RESUME_CURSOR
    assert_no_secrets(logs, checkpoints)


def test_single_page_null_next_cursor_persists_for_the_next_sync(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            page(
                [event(EVENT_UUID, payload=None, created_at="2026-10-16T12:00:00.123456Z")],
                None,
                False,
            ),
            page([], None, False),
        ],
    )

    connector.update(config(), {})

    assert "cursor" not in session.calls[0]["params"]
    assert checkpoints[-1]["cursors"] == {CONTRACT_A: RESUME_CURSOR}
    assert upserts[0][1]["id"] == EVENT_UUID

    upserts.clear()
    connector.update(config(), checkpoints[-1])

    assert session.calls[1]["params"]["cursor"] == RESUME_CURSOR
    assert upserts == []
    assert checkpoints[-1]["cursors"] == {CONTRACT_A: RESUME_CURSOR}
    assert len(session.calls) == 2
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
    assert sleeps == []
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


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (410, "cursor expired"),
        (400, "invalid cursor"),
        (400, "The passed-event cursor is Invalid Cursor"),
    ],
)
def test_expired_or_invalid_cursor_clears_state_and_resyncs(monkeypatch, logs, ops, status, error):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            FakeResponse(status, {"error": error, "status": status}),
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
            FakeResponse(410, {"error": "gone", "status": 410}),
        ],
    )

    with pytest.raises(connector.ContractGateSyncError, match="after a full re-sync"):
        connector.update(config(), {"cursors": {CONTRACT_A: "expired"}})

    assert len(session.calls) == 2
    assert checkpoints[0]["cursors"] == {}
    assert_no_secrets(logs, checkpoints)


def test_other_400_fails_with_api_error_and_keeps_state(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [FakeResponse(400, {"error": "limit must be at least 1", "status": 400})],
    )

    with pytest.raises(connector.ContractGateSyncError, match="limit must be at least 1") as raised:
        connector.update(config(), {"cursors": {CONTRACT_A: "keep-me", CONTRACT_B: "leave-b"}})

    assert "keep-me" not in str(raised.value)
    assert API_KEY not in str(raised.value)
    assert len(session.calls) == 1
    assert session.calls[0]["params"]["cursor"] == "keep-me"
    assert upserts == []
    assert checkpoints == []
    assert_no_secrets(logs, checkpoints)


def test_other_400_after_a_page_keeps_the_saved_cursor(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            page([event("evt-1")], "cursor-1", True),
            FakeResponse(400, {"error": "limit must be at least 1", "status": 400}),
        ],
    )

    with pytest.raises(connector.ContractGateSyncError, match="limit must be at least 1"):
        connector.update(config(), {"cursors": {CONTRACT_B: "leave-b"}})

    assert len(session.calls) == 2
    assert upserts[0][1]["id"] == "evt-1"
    assert checkpoints == [{"cursors": {CONTRACT_B: "leave-b", CONTRACT_A: "cursor-1"}}]
    assert_no_secrets(logs, checkpoints)


def test_one_page_incremental_resume_upserts_no_old_rows(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            page(
                [event("evt-1"), event("evt-2", created_at="2026-05-01T12:00:01Z")],
                "cursor-2",
                False,
            ),
            page(
                [
                    event("evt-3", created_at="2026-05-01T12:00:02Z"),
                    event("evt-4", created_at="2026-05-01T12:00:03Z"),
                ],
                "cursor-4",
                False,
            ),
        ],
    )

    connector.update(config(), {})
    assert [row[1]["id"] for row in upserts] == ["evt-1", "evt-2"]
    assert checkpoints[-1]["cursors"][CONTRACT_A] == "cursor-2"

    upserts.clear()
    connector.update(config(), checkpoints[-1])

    resumed = [row[1]["id"] for row in upserts]
    assert resumed == ["evt-3", "evt-4"]
    assert "evt-1" not in resumed
    assert "evt-2" not in resumed
    assert session.calls[1]["params"]["cursor"] == "cursor-2"
    assert len(session.calls) == 2
    assert checkpoints[-1]["cursors"][CONTRACT_A] == "cursor-4"
    assert_no_secrets(logs, checkpoints)


def test_multi_page_resume(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install_session(
        monkeypatch,
        [
            page([event("evt-2")], "cursor-2", True),
            page([event("evt-3", created_at="2026-05-01T12:00:02Z")], "cursor-3", False),
        ],
    )

    connector.update(config(), {"cursors": {CONTRACT_A: "cursor-1", CONTRACT_B: "leave-b"}})

    assert [call["params"]["cursor"] for call in session.calls] == ["cursor-1", "cursor-2"]
    assert [row[1]["id"] for row in upserts] == ["evt-2", "evt-3"]
    assert checkpoints[-1]["cursors"] == {CONTRACT_B: "leave-b", CONTRACT_A: "cursor-3"}
    assert_no_secrets(logs, checkpoints)


def test_bad_uuid_fails_and_names_the_id(monkeypatch, ops):
    for bad in (
        "contract/b",
        "550e8400e29b41d4a716446655440000",
        "not-a-uuid",
        "550e8400-e29b-41d4-a716-44665544000",
        "550e8400-e29b-41d4-a716-446655440000-extra",
    ):
        checked = connector.configuration_check(
            {"api_key": API_KEY, "contract_ids": f"{CONTRACT_A},{bad}"}
        )
        assert checked.success is False
        assert bad in checked.failure
        assert "configuration error" in checked.failure
        assert "DataContractGate" in checked.failure
        assert "8-4-4-4-12" in checked.failure
        assert "UUID" in checked.failure
        assert API_KEY not in checked.failure

        with pytest.raises(ValueError, match=re.escape(bad)):
            connector.schema({"api_key": API_KEY, "contract_ids": bad})

        session = install_session(monkeypatch, [page([event("evt-1")], "cursor-1", False)])
        with pytest.raises(ValueError, match=re.escape(bad)):
            connector.update(config(contract_ids=bad), {})
        assert session.calls == []


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
