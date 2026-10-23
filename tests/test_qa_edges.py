"""Independent checks for resume cursors and contract id validation.

Expected cursors are fixed strings: unpadded URL-safe base64 of
``{microseconds since the Unix epoch},{lowercase uuid}``.
"""

from __future__ import annotations

import base64
import json

import pytest

import connector

API_KEY = "cg_test_api_key_do_not_log"
PAYLOAD_SENTINEL = "payload-secret-do-not-log"
EVENT_ID = "550e8400-e29b-41d4-a716-446655440000"
CONTRACT_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
EARLIER_ID = "6ba7b811-9dad-11d1-80b4-00c04fd430c8"
PAGE_ONE_ID = "11111111-1111-4111-8111-111111111111"

# 2026-10-16T12:00:00Z
CURSOR_ZERO = "MTc5MjE1MjAwMDAwMDAwMCw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 2026-10-16T12:00:00.123Z → 123000 microseconds, not 123
CURSOR_MILLIS = "MTc5MjE1MjAwMDEyMzAwMCw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 2026-10-16T12:00:00.123456Z and the same instant written with a 9-digit fraction
CURSOR_MICROS = "MTc5MjE1MjAwMDEyMzQ1Niw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 2026-10-16T12:00:00.999999999Z truncated, not rounded to the next second
CURSOR_NINE_NINES = "MTc5MjE1MjAwMDk5OTk5OSw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 2026-10-16T12:00:00.1Z → 100000 microseconds, not 1
CURSOR_TENTH = "MTc5MjE1MjAwMDEwMDAwMCw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 1970-01-01T00:00:00.000001Z
CURSOR_EPOCH = "MSw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 2026-10-16T12:00:00.123456789+05:30 → 06:30:00.123456Z
CURSOR_OFFSET = "MTc5MjEzMjIwMDEyMzQ1Niw1NTBlODQwMC1lMjliLTQxZDQtYTcxNi00NDY2NTU0NDAwMDA"
# 2026-10-16T11:00:00Z for PAGE_ONE_ID
CURSOR_PAGE_ONE = "MTc5MjE0ODQwMDAwMDAwMCwxMTExMTExMS0xMTExLTQxMTEtODExMS0xMTExMTExMTExMTE"
# 2026-05-01T12:00:00.123Z for CONTRACT_B
CURSOR_CONTRACT_B = "MTc3NzYzNjgwMDEyMzAwMCw2YmE3YjgxMC05ZGFkLTExZDEtODBiNC0wMGMwNGZkNDMwYzg"

EPOCH_ZERO = 1_792_152_000_000_000
EPOCH_MILLIS = 1_792_152_000_123_000
EPOCH_MICROS = 1_792_152_000_123_456
EPOCH_NINE_NINES = 1_792_152_000_999_999
EPOCH_TENTH = 1_792_152_000_100_000
EPOCH_OFFSET = 1_792_132_200_123_456


class FakeResponse:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.headers: dict = {}
        self.text = json.dumps(body)
        self.ok = 200 <= status_code < 300

    def json(self):
        return self._body


class RecordingSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "params": dict(params or {}), "headers": dict(headers or {})}
        )
        if not self._responses:
            raise AssertionError(f"unexpected request to {url}")
        return self._responses.pop(0)

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
        upserts.append((table, dict(data)))

    def checkpoint(state):
        checkpoints.append(json.loads(json.dumps(state, default=str)))

    monkeypatch.setattr(connector.op, "upsert", upsert)
    monkeypatch.setattr(connector.op, "checkpoint", checkpoint)
    return upserts, checkpoints


def install(monkeypatch, responses):
    session = RecordingSession(responses)
    monkeypatch.setattr(connector.requests, "Session", lambda: session)
    return session


def passed_page(events, has_more, next_cursor="__omit__"):
    body = {"events": events, "has_more": has_more}
    if next_cursor != "__omit__":
        body["next_cursor"] = next_cursor
    return FakeResponse(200, body)


def passed_event(event_id, created_at, payload=None):
    return {
        "id": event_id,
        "contract_version": "1.2.0",
        "created_at": created_at,
        "payload": payload,
    }


def config(**overrides):
    base = {
        "api_key": API_KEY,
        "contract_ids": EVENT_ID,
        "base_url": "https://api.datacontractgate.com",
    }
    base.update(overrides)
    return base


def decode_cursor(cursor: str) -> str:
    padding = "=" * ((4 - len(cursor) % 4) % 4)
    return base64.urlsafe_b64decode(cursor + padding).decode("ascii")


def assert_no_secrets(logs, checkpoints):
    blob = json.dumps(logs) + json.dumps(checkpoints)
    assert API_KEY not in blob
    assert PAYLOAD_SENTINEL not in blob
    for state in checkpoints:
        for cursor in state.get("cursors", {}).values():
            assert isinstance(cursor, str) and cursor
            assert cursor != "null"


@pytest.mark.parametrize(
    ("created_at", "cursor", "epoch_us"),
    [
        ("2026-10-16T12:00:00Z", CURSOR_ZERO, EPOCH_ZERO),
        ("2026-10-16T12:00:00+00:00", CURSOR_ZERO, EPOCH_ZERO),
        ("  2026-10-16T12:00:00z  ", CURSOR_ZERO, EPOCH_ZERO),
        ("2026-10-16T12:00:00.123Z", CURSOR_MILLIS, EPOCH_MILLIS),
        ("2026-10-16T12:00:00.123+00:00", CURSOR_MILLIS, EPOCH_MILLIS),
        ("2026-10-16T12:00:00.123456Z", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16T12:00:00.123456+00:00", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16T12:00:00.123456789Z", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16T12:00:00.123456789+00:00", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16T12:00:00,123456789Z", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16 12:00:00.123456789Z", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16T12:00:00.999999999Z", CURSOR_NINE_NINES, EPOCH_NINE_NINES),
        ("2026-10-16T12:00:00.999999999+00:00", CURSOR_NINE_NINES, EPOCH_NINE_NINES),
        ("2026-10-16T12:00:00.1Z", CURSOR_TENTH, EPOCH_TENTH),
        ("1970-01-01T00:00:00.000001Z", CURSOR_EPOCH, 1),
        ("2026-10-16T14:00:00.123456+02:00", CURSOR_MICROS, EPOCH_MICROS),
        ("2026-10-16T12:00:00.123456789+05:30", CURSOR_OFFSET, EPOCH_OFFSET),
    ],
)
def test_fallback_cursor_uses_truncated_utc_microseconds(created_at, cursor, epoch_us):
    parsed = connector._parse_created_at(created_at)
    assert connector._utc_timestamp_micros(parsed) == epoch_us
    encoded = connector._cursor_from_event(
        passed_event(EVENT_ID.upper(), created_at, payload={"note": PAYLOAD_SENTINEL})
    )
    assert encoded == cursor
    assert decode_cursor(encoded) == f"{epoch_us},{EVENT_ID}"
    assert "=" not in encoded
    assert "+" not in encoded
    assert "/" not in encoded


def test_non_uuid_event_id_does_not_invent_a_cursor():
    row = passed_event("evt-1", "2026-10-16T12:00:00.123456Z")
    assert connector._cursor_from_event(row) is None
    assert connector._cursor_from_event("not-an-event") is None


@pytest.mark.parametrize(
    "next_cursor",
    [None, "", "   ", "\n\t", "__omit__"],
)
def test_single_page_missing_cursor_is_reused_and_then_kept(monkeypatch, logs, ops, next_cursor):
    upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page(
                [
                    passed_event(
                        EVENT_ID,
                        "2026-10-16T12:00:00.123456789Z",
                        {"note": PAYLOAD_SENTINEL},
                    )
                ],
                False,
                next_cursor,
            ),
            passed_page(
                [passed_event(EARLIER_ID, "2026-10-16T12:00:01Z")],
                False,
                None,
            ),
            passed_page([], False, None),
        ],
    )

    connector.update(config(), {})

    assert "cursor" not in session.calls[0]["params"]
    assert session.calls[0]["params"]["limit"] == 1000
    assert checkpoints[-1]["cursors"] == {EVENT_ID: CURSOR_MICROS}
    assert upserts[0][1]["created_at"].microsecond == 123456
    assert [row[1]["id"] for row in upserts] == [EVENT_ID]

    upserts.clear()
    connector.update(config(), checkpoints[-1])

    assert session.calls[1]["params"]["cursor"] == CURSOR_MICROS
    assert [row[1]["id"] for row in upserts] == [EARLIER_ID]
    advanced = checkpoints[-1]["cursors"][EVENT_ID]
    assert advanced != CURSOR_MICROS
    assert decode_cursor(advanced).endswith(EARLIER_ID)

    upserts.clear()
    connector.update(config(), checkpoints[-1])

    assert session.calls[2]["params"]["cursor"] == advanced
    assert upserts == []
    assert checkpoints[-1]["cursors"] == {EVENT_ID: advanced}
    assert_no_secrets(logs, checkpoints)


@pytest.mark.parametrize("next_cursor", [None, "", "   ", "__omit__"])
def test_final_page_missing_cursor_advances_past_the_previous_page(
    monkeypatch, logs, ops, next_cursor
):
    upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page(
                [passed_event(PAGE_ONE_ID, "2026-10-16T11:00:00Z")],
                True,
                "cursor-page-1",
            ),
            passed_page(
                [
                    passed_event(
                        EVENT_ID,
                        "2026-10-16T12:00:00.999999999Z",
                        {"note": PAYLOAD_SENTINEL},
                    )
                ],
                False,
                next_cursor,
            ),
            passed_page([], False, "   "),
        ],
    )

    connector.update(config(), {})

    assert [call["params"].get("cursor") for call in session.calls] == [None, "cursor-page-1"]
    saved = [state["cursors"][EVENT_ID] for state in checkpoints]
    assert saved == ["cursor-page-1", CURSOR_NINE_NINES]
    assert upserts[-1][1]["created_at"].microsecond == 999999
    assert upserts[-1][1]["created_at"].second == 0

    upserts.clear()
    connector.update(config(), checkpoints[-1])

    assert session.calls[-1]["params"]["cursor"] == CURSOR_NINE_NINES
    assert upserts == []
    assert checkpoints[-1]["cursors"][EVENT_ID] == CURSOR_NINE_NINES
    assert_no_secrets(logs, checkpoints)


def test_omitted_cursor_on_every_page_still_pages_forward(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page([passed_event(PAGE_ONE_ID, "2026-10-16T11:00:00Z")], True),
            passed_page([passed_event(EVENT_ID, "2026-10-16T12:00:00.123Z")], False),
        ],
    )

    connector.update(config(), {})

    assert len(session.calls) == 2
    assert "cursor" not in session.calls[0]["params"]
    assert session.calls[1]["params"]["cursor"] == CURSOR_PAGE_ONE
    assert [state["cursors"][EVENT_ID] for state in checkpoints] == [CURSOR_PAGE_ONE, CURSOR_MILLIS]
    assert [row[1]["id"] for row in upserts] == [PAGE_ONE_ID, EVENT_ID]
    assert_no_secrets(logs, checkpoints)


@pytest.mark.parametrize("server_cursor", ["opaque-server-cursor", "  opaque-server-cursor  "])
def test_server_next_cursor_wins_over_the_synthesized_cursor(monkeypatch, logs, ops, server_cursor):
    upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page(
                [passed_event(EVENT_ID, "2026-10-16T12:00:00.123456Z")],
                False,
                server_cursor,
            ),
            passed_page([], False, None),
        ],
    )

    connector.update(config(), {})

    assert checkpoints[-1]["cursors"] == {EVENT_ID: "opaque-server-cursor"}
    upserts.clear()
    connector.update(config(), checkpoints[-1])
    assert session.calls[1]["params"]["cursor"] == "opaque-server-cursor"
    assert upserts == []
    assert_no_secrets(logs, checkpoints)


@pytest.mark.parametrize("next_cursor", [None, "", "   ", "__omit__"])
def test_empty_page_does_not_replace_saved_cursors(monkeypatch, logs, ops, next_cursor):
    upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page([], False, next_cursor),
            passed_page([], False, next_cursor),
        ],
    )
    saved = {EVENT_ID: "keep-a", CONTRACT_B: "keep-b"}

    connector.update(config(contract_ids=f"{EVENT_ID},{CONTRACT_B}"), {"cursors": dict(saved)})

    assert [call["params"]["cursor"] for call in session.calls] == ["keep-a", "keep-b"]
    assert upserts == []
    assert checkpoints[-1]["cursors"] == saved
    assert all(cursor is not None for cursor in checkpoints[-1]["cursors"].values())
    assert_no_secrets(logs, checkpoints)


def test_each_contract_keeps_its_own_cursor(monkeypatch, logs, ops):
    upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page([passed_event(EVENT_ID, "2026-10-16T12:00:00.123456Z")], False, None),
            passed_page(
                [passed_event(CONTRACT_B.upper(), "2026-05-01T12:00:00.123+00:00")],
                False,
                "   ",
            ),
            passed_page([], False, None),
            passed_page([], False),
        ],
    )

    connector.update(config(contract_ids=f"{EVENT_ID},{CONTRACT_B}"), {})

    assert session.calls[0]["url"].endswith(f"/v1/contracts/{EVENT_ID}/passed")
    assert session.calls[1]["url"].endswith(f"/v1/contracts/{CONTRACT_B}/passed")
    assert "cursor" not in session.calls[0]["params"]
    assert "cursor" not in session.calls[1]["params"]
    assert checkpoints[-1]["cursors"] == {EVENT_ID: CURSOR_MICROS, CONTRACT_B: CURSOR_CONTRACT_B}
    assert [row[1]["id"] for row in upserts] == [EVENT_ID, CONTRACT_B.upper()]

    upserts.clear()
    connector.update(config(contract_ids=f"{EVENT_ID},{CONTRACT_B}"), checkpoints[-1])

    assert session.calls[2]["params"]["cursor"] == CURSOR_MICROS
    assert session.calls[3]["params"]["cursor"] == CURSOR_CONTRACT_B
    assert upserts == []
    assert checkpoints[-1]["cursors"] == {EVENT_ID: CURSOR_MICROS, CONTRACT_B: CURSOR_CONTRACT_B}
    assert_no_secrets(logs, checkpoints)


def test_server_cursor_on_one_contract_does_not_clobber_the_other(monkeypatch, logs, ops):
    _upserts, checkpoints = ops
    session = install(
        monkeypatch,
        [
            passed_page(
                [passed_event(EVENT_ID, "2026-10-16T12:00:00.123456Z")],
                False,
                "server-for-a",
            ),
            passed_page([passed_event(CONTRACT_B, "2026-05-01T12:00:00.123Z")], False, None),
        ],
    )

    connector.update(config(contract_ids=f" {EVENT_ID.upper()} , {CONTRACT_B} "), {})

    assert session.calls[0]["url"].endswith(f"/v1/contracts/{EVENT_ID}/passed")
    assert session.calls[1]["url"].endswith(f"/v1/contracts/{CONTRACT_B}/passed")
    assert checkpoints[-1]["cursors"] == {EVENT_ID: "server-for-a", CONTRACT_B: CURSOR_CONTRACT_B}


def test_last_non_uuid_event_does_not_move_the_cursor_backward(monkeypatch, ops):
    upserts, checkpoints = ops
    install(
        monkeypatch,
        [
            passed_page(
                [
                    passed_event(EARLIER_ID, "2026-10-16T12:00:00Z"),
                    passed_event("evt-last", "2026-10-16T12:00:01Z"),
                ],
                False,
                None,
            )
        ],
    )

    connector.update(config(), {"cursors": {EVENT_ID: "keep-me", CONTRACT_B: "leave-b"}})

    assert [row[1]["id"] for row in upserts] == [EARLIER_ID, "evt-last"]
    assert checkpoints[-1]["cursors"] == {EVENT_ID: "keep-me", CONTRACT_B: "leave-b"}


def test_unusable_timestamp_fails_the_row_without_storing_null(monkeypatch, ops):
    _upserts, checkpoints = ops
    install(
        monkeypatch,
        [passed_page([passed_event(EVENT_ID, "not-a-timestamp")], False, None)],
    )

    with pytest.raises(connector.ContractGateSyncError, match="created_at"):
        connector.update(config(), {"cursors": {EVENT_ID: "keep-me"}})

    assert checkpoints == []


@pytest.mark.parametrize(
    ("contract_ids", "snippet"),
    [
        ("", "contract_ids is required"),
        ("   ", "contract_ids is required"),
        ("\t", "contract_ids is required"),
        (",", "empty contract id"),
        (" , ", "empty contract id"),
        (f"{EVENT_ID},", "empty contract id"),
        (f",{EVENT_ID}", "empty contract id"),
        (f"{EVENT_ID},   ,{CONTRACT_B}", "empty contract id"),
        ("not-a-uuid", "not-a-uuid"),
        ("contract/b", "contract/b"),
        ("550e8400e29b41d4a716446655440000", "550e8400e29b41d4a716446655440000"),
        ("550e8400-e29b-41d4-a716-44665544000", "550e8400-e29b-41d4-a716-44665544000"),
        (
            "550e8400-e29b-41d4-a716-446655440000-extra",
            "550e8400-e29b-41d4-a716-446655440000-extra",
        ),
        (
            "{550e8400-e29b-41d4-a716-446655440000}",
            "{550e8400-e29b-41d4-a716-446655440000}",
        ),
        ("gggggggg-e29b-41d4-a716-446655440000", "gggggggg-e29b-41d4-a716-446655440000"),
        (f" {EVENT_ID} , not-a-uuid ", "not-a-uuid"),
    ],
)
def test_invalid_contract_ids_fail_before_any_request(monkeypatch, contract_ids, snippet):
    opened = []

    class ExplodingSession:
        def __init__(self):
            opened.append(self)

        def get(self, *args, **kwargs):
            raise AssertionError("HTTP request issued during configuration validation")

        def close(self):
            return None

    monkeypatch.setattr(connector.requests, "Session", ExplodingSession)

    checked = connector.configuration_check({"api_key": API_KEY, "contract_ids": contract_ids})
    assert checked.success is False
    assert snippet in checked.failure
    assert API_KEY not in checked.failure

    with pytest.raises(ValueError, match="contract_ids") as schema_error:
        connector.schema({"api_key": API_KEY, "contract_ids": contract_ids})
    with pytest.raises(ValueError, match="contract_ids") as update_error:
        connector.update(
            {
                "api_key": API_KEY,
                "contract_ids": contract_ids,
                "base_url": "https://api.datacontractgate.com",
            },
            {"cursors": {EVENT_ID: "should-not-be-used"}},
        )

    assert opened == []
    for message in (checked.failure, str(schema_error.value), str(update_error.value)):
        assert snippet in message
        assert API_KEY not in message
        if snippet not in {"contract_ids is required", "empty contract id"}:
            assert "DataContractGate" in message
            assert "8-4-4-4-12" in message
            assert "UUID" in message


def test_uppercase_and_whitespace_uuids_are_normalized_before_the_request(monkeypatch, ops):
    upserts, _checkpoints = ops
    session = install(monkeypatch, [passed_page([], False, None)])
    raw_ids = f"  {EVENT_ID.upper()} , {EVENT_ID}  "

    normalized = connector.validate_configuration(
        {"api_key": f"  {API_KEY}  ", "contract_ids": raw_ids}
    )
    assert normalized["contract_ids"] == [EVENT_ID]
    assert normalized["api_key"] == API_KEY

    tables = connector.schema({"api_key": API_KEY, "contract_ids": raw_ids})
    assert tables[0]["table"] == "passed_550e8400_e29b_41d4_a716_446655440000"

    connector.update(config(contract_ids=raw_ids), {})

    assert len(session.calls) == 1
    assert session.calls[0]["url"].endswith(f"/v1/contracts/{EVENT_ID}/passed")
    assert upserts == []
