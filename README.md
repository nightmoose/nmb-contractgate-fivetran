# DataContractGate Fivetran connector

Source connector for DataContractGate, which shares an engine with AISchemaGate ([nightmoose/contractgate](https://github.com/nightmoose/contractgate)). It syncs only records that passed validation.

Each configured contract becomes one destination table. The default name is `passed_<sanitized contract id>`, and it stays that way unless `table_names` sets `contract_id=name` for that id. The primary key is `id`. Columns are `contract_version`, `created_at` (UTC), and `payload` (JSON). `payload` is null when that customer's event-body storage is off.

The connector calls `GET /v1/contracts/{id}/passed` with the `x-api-key` header. Pages are oldest first, at most 1000 events per request. Production is `https://api.datacontractgate.com`. To point at another environment, set `base_url` in `configuration.json` (see below).

This repository is public. `configuration.json` is committed with empty secrets. Put a real API key only in your local file or in Fivetran, and do not commit it.

## Deploy into your own Fivetran account

A customer installs this connector in their own Fivetran account with the Connector SDK CLI. A one-click **Connect Fivetran** button inside DataContractGate is coming; it is not available yet.

From this directory, with Python 3.10–3.14:

```bash
python -m venv .venv
source .venv/bin/activate
pip install fivetran-connector-sdk
```

Fill `configuration.json` (see below), then deploy. `FIVETRAN_API_KEY` is the Base64 encoding of `{api_key}:{api_secret}` from your Fivetran account, not the DataContractGate key.

```bash
fivetran deploy \
  --api-key "$FIVETRAN_API_KEY" \
  --destination "$FIVETRAN_DESTINATION_NAME" \
  --connection contractgate_passed \
  --configuration configuration.json \
  --python 3.12
```

Run the command from the directory that contains `connector.py`. The connection name must start with a lowercase letter or underscore and then use only lowercase letters, numbers, and underscores. The new connection is paused until you start the initial sync in the Fivetran dashboard.

`requests` and `fivetran_connector_sdk` are already installed in the Fivetran runtime. `requirements.txt` leaves them unpinned so the runtime versions stay in charge.

## Local debug

`fivetran debug` runs the connector on your machine and writes a local DuckDB file.

```bash
python -m venv .venv
source .venv/bin/activate
pip install fivetran-connector-sdk
# Edit configuration.json with your API key, contract UUIDs, and base URL.
# Activation puts .venv/bin on PATH. fivetran debug runs pipreqs from that directory.
fivetran debug --configuration configuration.json
```

`fivetran debug` needs the virtualenv `bin` directory on `PATH` so it can run `pipreqs`. It may rewrite `requirements.txt`. Keep the committed `requirements.txt`, and restore that copy if the command changes the file.

The tester writes:

- `files/warehouse.db` — rows the connector upserted
- `files/state.json` — cursors saved by `op.checkpoint`

Run a second debug to confirm the next sync resumes from those cursors. To throw away the local warehouse and state:

```bash
fivetran reset
fivetran debug --configuration configuration.json
```

`configuration.json` is required. An empty file fails the configuration check before any request is sent. You can also run `python connector.py`, which loads `configuration.json` and calls `connector.debug`.

To run the setup test without a sync:

```bash
fivetran configuration --test
```

Unit tests mock HTTP and do not need a key:

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The connector runtime has no extra packages. `requests` and `fivetran_connector_sdk` stay pre-installed, so leave the committed `requirements.txt` as it is. The tester scans `tests/` and may suggest adding `pytest`; that suggestion is for the test suite.

## Configuration

| Field | Required | Secret | Description |
| --- | --- | --- | --- |
| `api_key` | yes | yes | DataContractGate API key, sent as `x-api-key`. |
| `contract_ids` | yes | no | Comma-separated contract UUIDs. One table per id. |
| `base_url` | no | no | API origin. Defaults to `https://api.datacontractgate.com`. Must be `https://`. `http://localhost` and `http://127.0.0.1` (any port) are allowed for local tests. |
| `table_names` | no | no | Optional `contract_id=name` pairs, comma-separated. A name must start with a letter or underscore and use only lowercase letters, numbers, and underscores. Unlisted contracts keep `passed_<id>`. |

Values are strings. `schema()` and `update()` both run `validate_configuration`. The dashboard setup form registers the same rules as a setup test named **Configuration check**.

```json
{
  "api_key": "",
  "contract_ids": "550e8400-e29b-41d4-a716-446655440000",
  "base_url": "https://api.datacontractgate.com",
  "table_names": "550e8400-e29b-41d4-a716-446655440000=passed_orders"
}
```

Leave `table_names` empty to keep every default `passed_<id>` name. Two contracts cannot share a table, and a custom name cannot reuse another contract's default `passed_<id>` name. The configuration check reports the id or name that failed. An id in `contract_ids` that is not a UUID fails that check before any API request.

## Paid plan

Passed-event export is a paid-plan feature. DataContractGate Free (`free`) and AISchemaGate Free (`aisg_free`) receive HTTP 403. The sync then fails with a message that says **paid plan required**. Paid plans are `growth`, `aisg_pro`, and `enterprise`.

## Retention and cursors

State is a JSON object of opaque cursors, one per contract:

```json
{"cursors": {"your-contract-id": "opaque-cursor"}}
```

The connector checkpoints that object after every page and stops paging when `has_more` is false. `next_cursor` is a resume cursor. On a page with events it is the cursor of the last event, including when `has_more` is false. On an empty page it repeats the cursor from the request. Every non-null `next_cursor` is saved, including on the last page. When `next_cursor` is null and the page has events, the connector saves a resume cursor built from the last event (that event's `created_at` in microseconds and its id). The next sync sends that cursor, so a one-page contract or a final page that omits `next_cursor` does not start over. An empty page with a null `next_cursor` leaves the last saved cursor in place.

HTTP 410 (cursor older than the plan retention window) and HTTP 400 whose error says `invalid cursor` drop that contract's cursor and re-sync from the oldest passed record. Any other HTTP 400 fails the sync with the API's error message and leaves the saved cursor in place. HTTP 429 and 5xx responses are retried with backoff. Requests use a 10-second connect timeout and a 60-second read timeout.

The API holds back rows newer than about five seconds, so the newest passed records can lag a sync by that much. Kafka and Kinesis consumers write a different path, so this export contains the HTTP and batch records that passed.

Sync logs omit the API key and event payloads.

## License

MIT. Copyright NightMoose, Inc. See [LICENSE](LICENSE).
