# ContractGate Fivetran connector

Source connector for [ContractGate](https://github.com/nightmoose/contractgate), the shared engine behind DataContractGate and AISchemaGate. It syncs only records that passed validation.

Each configured contract becomes one destination table, `passed_<sanitized contract id>`, with primary key `id` and columns `contract_version`, `created_at` (UTC), and `payload` (JSON). `payload` is null when that customer's event-body storage is off.

The connector calls `GET /v1/contracts/{id}/passed` with the `x-api-key` header. Pages are oldest first, at most 1000 events per request. Production is `https://api.datacontractgate.com`. Staging is `https://contractgate-api-staging.fly.dev`.

This repository is public. `configuration.json` is committed with empty secrets. Put a real API key only in your local file or in Fivetran, and do not commit it.

## Deploy into your own Fivetran account

A customer installs this connector in their own Fivetran account with the Connector SDK CLI. A one-click **Connect Fivetran** button inside ContractGate is coming; it is not available yet.

From this directory, with Python 3.10–3.14:

```bash
python -m venv .venv
source .venv/bin/activate
pip install fivetran-connector-sdk
```

Fill `configuration.json` (see below), then deploy. `FIVETRAN_API_KEY` is the Base64 encoding of `{api_key}:{api_secret}` from your Fivetran account, not the ContractGate key.

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
pip install fivetran-connector-sdk
# Edit configuration.json with your API key, contract ids, and base URL.
fivetran debug --configuration configuration.json
```

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

`fivetran debug` scans every Python file, including `tests/`, and may suggest adding `pytest` to `requirements.txt`. Keep the committed file. The connector runtime has no extra packages; `requests` and `fivetran_connector_sdk` stay pre-installed. The tester can also remove a comment-only `requirements.txt`. Restore the committed copy if that happens.

## Configuration

| Field | Required | Secret | Description |
| --- | --- | --- | --- |
| `api_key` | yes | yes | ContractGate API key, sent as `x-api-key`. |
| `contract_ids` | yes | no | Comma-separated contract ids. One table per id. |
| `base_url` | no | no | API origin. Defaults to `https://api.datacontractgate.com`. |

Values are strings. `schema()` and `update()` both run `validate_configuration`. The dashboard setup form registers the same rules as a setup test named **Configuration check**.

```json
{
  "api_key": "",
  "contract_ids": "contract-id-1,contract-id-2",
  "base_url": "https://api.datacontractgate.com"
}
```

## Paid plan

Passed-event export is a paid-plan feature. DataContractGate Free (`free`) and AISchemaGate Free (`aisg_free`) receive HTTP 403. The sync then fails with a message that says **paid plan required**. Paid plans are `growth`, `aisg_pro`, and `enterprise`.

## Retention and cursors

State is a JSON object of opaque cursors, one per contract:

```json
{"cursors": {"your-contract-id": "opaque-cursor"}}
```

The connector checkpoints that object after every page. A checkpoint never stores null. On the last page the API may return either a new `next_cursor` or `next_cursor: null` with `has_more: false`. A non-null cursor is saved. A null cursor leaves the last saved cursor in place, so the next sync still has a position and does not treat the contract as a first run.

HTTP 400 (malformed cursor) and HTTP 410 (cursor older than the plan retention window) both drop that contract's cursor and re-sync from the oldest passed record. HTTP 429 and 5xx responses are retried with backoff. Requests use a 10-second connect timeout and a 60-second read timeout.

The API holds back rows newer than about five seconds, so the newest passed records can lag a sync by that much. Kafka and Kinesis consumers write a different path, so this export contains the HTTP and batch records that passed.

Sync logs omit the API key and event payloads.

## License

MIT. Copyright NightMoose, Inc. See [LICENSE](LICENSE).
