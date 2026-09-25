# qbwc-kit

[![CI](https://github.com/meister5/qbwc-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/meister5/qbwc-kit/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/qbwc-kit?style=flat-square)](https://pypi.org/project/qbwc-kit/)
[![Python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

QuickBooks Desktop has no HTTP API. The only supported way in is the **Web Connector**: a
Windows service that polls *your* SOAP endpoint on a schedule, asks it for qbXML, hands that
to QuickBooks over COM, and posts the response back.

Before you can read a single invoice you have to implement eight SOAP callbacks, serve a
WSDL, keep a ticket-based session alive across many HTTP round trips, and hand-build XML that
QuickBooks rejects with unhelpful errors when the element order is wrong. I wrote all of that,
so the only part left is the one specific to your data.

Why QuickBooks Desktop has no API at all, and what the Web Connector actually costs you:
[`docs/why-web-connector.md`](docs/why-web-connector.md).

```python
from qbwc_kit import QBWCService, StaticAuthenticator, qbxml
from qbwc_kit.qbxml import QBXMLRequest
from qbwc_kit.server import create_app


class SyncCustomers:
    name = "customers"

    def run(self, ctx):
        request = qbxml.query("Customer", max_returned=100, iterator="Start")
        while True:
            result = yield QBXMLRequest([request])
            page = result.first().raise_for_status()
            save(page.records)
            if not page.has_more:
                return
            request.iterator = "Continue"
            request.iterator_id = page.iterator_id


service = QBWCService(
    authenticator=StaticAuthenticator("qbwc", "s3cret", [SyncCustomers()])
)
app = create_app(service, endpoint_url="https://books.example.com/qbwc")
```

The `while` loop is why I wrote this. Each `yield` suspends the task until the Web Connector
comes back with the response, so pagination, conditional writes, and read-then-write jobs stay in
ordinary control flow instead of being flattened into a per-request state machine.

## Install

It is on PyPI:

```bash
pip install qbwc-kit                    # core: standard library only
pip install 'qbwc-kit[server]'          # adds the FastAPI adapter
```

Or clone it and `pip install -e '.[dev]'` to run the tests.

The core (SOAP, qbXML, sessions, the WSDL generator) has no dependencies outside the standard
library. FastAPI is only needed if you use `qbwc_kit.server`. The service itself is just
`dispatch(soap_body) -> soap_body`, so it drops into Flask, Django or bare WSGI unchanged.

## Testing without QuickBooks

Exercising a Web Connector integration normally requires a Windows box, a QuickBooks Desktop
install, an open company file, and a human clicking *Update Selected*. The failure modes that
matter (a task that never terminates, an iterator that loops forever, a non-zero status nobody
checked) are the ones I kept only finding in production.

`qbwc_kit.testing` replaces both ends:

```python
from qbwc_kit.testing import FakeQuickBooks, FakeWebConnector, service_transport

connector = FakeWebConnector(transport=service_transport(service),
                             username="qbwc", password="s3cret")
quickbooks = FakeQuickBooks(entities={"Customer": [{"ListID": "1", "Name": "Acme"}]})

result = connector.run_update(quickbooks)

assert result.progress[-1] == 100
assert quickbooks.seen[0].count("iterator=\"Start\"") == 1
```

My `FakeWebConnector` replays the real call sequence (`clientVersion`, `authenticate`, the
`sendRequestXML`/`receiveResponseXML` loop, `closeConnection`) and raises if the session
doesn't terminate, which catches runaway iterators in milliseconds instead of in the
connector's log. `FakeQuickBooks` answers qbXML the way QuickBooks does: paged iterators,
`MaxReturned`, status 1 for an empty result, status 3100 for an unsupported request.

## Things that caught me out

**Status codes ride on successful envelopes.** A request QuickBooks refused (status 3100,
"not available in this edition") comes back as a perfectly well-formed response document with
no records in it. If you parse for rows and ignore the status, an unsupported request is
indistinguishable from an empty table, and a cache built on top of it degrades into empty
results without anything logging an error. Every `Response` here carries its status, `ok`
distinguishes "nothing found" (status 1, genuinely fine) from "never ran", and
`raise_for_status()` is one call away.

**Element order is part of the schema.** qbXML is a sequence, not a bag. `MaxReturned` before
the filters, `EditSequence` before the fields being changed. The builders emit the right order,
so you pass a dict and do not have to track it.

**Writes use optimistic concurrency.** Every `Mod` request must carry the `EditSequence` from
the last read, and a stale one is rejected rather than silently clobbering another user's
edit. `qbxml.mod()` requires it as a keyword argument for that reason.

**Iterators only exist on some entities.** Asking for one on an entity that doesn't support it
is an opaque parse error from QuickBooks, so I raise at build time instead.

**Returning 100 ends the session.** `receiveResponseXML` returns percent complete, and a
progress calculation that rounds up too early silently truncates the sync. `Session.progress()`
caps at 99 until the work is genuinely done.

**An unknown ticket is normal.** Restart the server mid-update and the next callback arrives
with a ticket that no longer exists. Faulting makes the Web Connector retry forever, so I tell it
the session is over instead.

## Layout

| Module | What it does |
| --- | --- |
| `qbwc_kit.soap` | The small SOAP 1.1 slice QBWC actually uses: parse a call, build a response or fault |
| `qbwc_kit.qbxml` | Request builders (`query`, `add`, `mod`) and a status-aware response parser |
| `qbwc_kit.session` | Generator-based tasks, the request/response loop, ticket store with TTL |
| `qbwc_kit.service` | The eight callbacks, framework-agnostic |
| `qbwc_kit.wsdl` | WSDL generation, plus the `.qwc` file users import into the connector |
| `qbwc_kit.server` | Optional FastAPI adapter |
| `qbwc_kit.testing` | `FakeWebConnector`, `FakeQuickBooks` |

## Example

[`examples/sync_to_sqlite.py`](examples/sync_to_sqlite.py) is a complete integration: it
mirrors customers and invoices into SQLite, syncs incrementally off a stored watermark, and
generates the `.qwc` file to import into the Web Connector.

```bash
python examples/sync_to_sqlite.py --write-qwc mirror.qwc   # generate the connector file
python examples/sync_to_sqlite.py --url https://books.example.com/qbwc
```

Two details in it are worth copying. The watermark only advances after every page has been
written, so an interrupted run repeats work instead of skipping it. And it is rewound by a
minute, because QuickBooks stamps `TimeModified` from the workstation clock, so a record saved
during the sync can otherwise land just behind the watermark and never be picked up again.

The example is covered by the test suite, so it can't drift from the library.

## Deployment notes

- QBWC will not accept an endpoint on plain HTTP unless it is `localhost`. Use TLS.
- `soap:address` in the WSDL has to be the URL the connector actually posts to. Behind a
  reverse proxy, that is the public URL, not `http://localhost:8000`.
- `OwnerID` and `FileID` in the `.qwc` file identify your integration to QuickBooks.
  Generate them once and keep them; changing them forces every user to re-authorise.
- The connector authenticates with a password stored in Windows' credential store. Treat the
  `authenticate` callback as a real auth boundary. `StaticAuthenticator` compares with
  `secrets.compare_digest`, and anything you write should too.

## Scope

Read and write access to list and transaction entities through qbXML, which is what the Web
Connector exposes. Not covered: QuickBooks Online (which has a REST API of its own),
qbposXML for Point of Sale, and the direct COM `QBFC` interface, which needs code running on
the same Windows machine as QuickBooks.

## Development

```bash
pip install -e '.[dev]'
pytest -q
ruff check . && ruff format --check .
```

## License

MIT
