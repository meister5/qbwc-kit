# The only supported way into QuickBooks Desktop is the Web Connector

*Published 2026-09-25. Copy approved by Eren Altuntas on AMA-5 (**[interaction ff7926af](https://github.com/meister5/qbwc-kit)** card accepted 2026-09-25T05:24:47Z). This file is byte-identical to the approved text apart from this line.*

Every QuickBooks integration search lands in the same dead end. You want invoice data, you search "QuickBooks API", and you arrive at Intuit's REST API — which talks to QuickBooks **Online**: a different product, a different database, a different plan. If the books are in QuickBooks **Desktop**, none of it applies. Desktop exposes no HTTP API. There is no port to call and no token to request.

Two ways in exist. The first is `QBFC`, Intuit's COM interface, which needs your code running as a Windows process on the same machine as QuickBooks. That is correct for a desktop add-in and useless when the thing that wants the data is a Python service on another host.

The second is the **Web Connector**, and it inverts the direction of everything. You run an HTTP endpoint. A Windows service on the QuickBooks machine polls *you* on a schedule, asks for qbXML, hands it to QuickBooks over COM, and posts the response back. Your server never calls QuickBooks. QuickBooks calls your server. That inversion is the entire architecture, and it is where all the difficulty lives — because a request/response conversation has been turned into a sequence of unrelated HTTP callbacks, and you have to put the conversation back together yourself.

## The eight callbacks

SOAP 1.1, eight methods, and you host all of them. In the order the connector uses them:

| # | Callback | What it does | What breaks when you get it wrong |
|---|---|---|---|
| 1 | `serverVersion` | Announces your server version. | Nothing, usually. Do not lie about it. |
| 2 | `clientVersion` | You vet the connector build. Return `""` to accept, `W:` to warn and continue, `E:` to refuse the update. | Returning `E:` for a version you merely dislike locks users out of their own sync. |
| 3 | `authenticate` | Returns `[ticket, company-file-or-status]`. | Slot 1 is overloaded: a path (or `""`) means *start work on this file*, `"none"` means *authenticated but idle*, `"nvu"` means *credentials rejected*. Return the wrong one and the connector opens a company file for no reason, or asks the user to log in again forever. |
| 4 | `sendRequestXML` | You return the next qbXML request. Empty string means nothing more to send. | An exception here faults the whole call. One broken task then cancels every task queued behind it. |
| 5 | `receiveResponseXML` | You consume the response and return percent complete. | `100` **ends the session**. A progress calculation that rounds up early silently truncates the sync, and nothing in your logs says so. |
| 6 | `connectionError` | The connector could not reach QuickBooks. | Return `"done"` to give up, or a company file path to retry against it. Retrying forever is what happens when you return something optimistic. |
| 7 | `getLastError` | The connector asks what went wrong. | Returning an empty string loses the only diagnostic the user will ever see. |
| 8 | `closeConnection` | Session over; clean up. | Leaked sessions if you never prune the ticket store. |

## The session is a ticket you have to keep

`authenticate` mints a ticket, and every later callback carries it. The work itself — your pagination loop, your read-then-write job — has to survive being sliced across many HTTP round trips.

The way out is to make the task a **generator**. Each `yield` suspends the task until the connector returns with a response, so ordinary control flow stays ordinary:

```python
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
```

Without that, the same job has to be flattened into a per-request state machine that tracks where it was — which is how integrations acquire a `state` column and a class of bugs nobody can reproduce.

Two consequences worth stating plainly. First, **an unknown ticket is normal, not an error**: restart your server mid-update and the next callback arrives holding a ticket that no longer exists. Faulting there makes the connector retry until a human intervenes, so the honest answer is to tell it the session is over. Second, **progress caps at 99** until the work is genuinely complete, because returning 100 is not a status update — it is how you end the session.

## qbXML is a sequence, not a bag

qbXML is XSD-ordered. `MaxReturned` before the filters. `EditSequence` before the fields being changed. Get the order wrong and QuickBooks rejects the document with an error that does not name the offending element — which is a genuinely bad afternoon to debug by hand. Builders that emit the right order mean you pass a dict and never track it.

The subtler trap is that **status codes ride on successful envelopes**. A request QuickBooks refused (status `3100`, "not available in this edition") comes back as a perfectly well-formed response document containing no records. If you parse for rows and ignore the status, "this request never ran" is indistinguishable from "this table is empty" — and a cache built on top of that degrades into missing data with nothing in the logs to explain it. Status `1`, by contrast, means "nothing found", which is genuinely fine. Every response in `qbwc-kit` carries its status and exposes `raise_for_status()`.

## What the test doubles replace

Testing this normally costs a Windows box, a QuickBooks install, an open company file, and a human clicking *Update Selected*. The failure modes that actually hurt — a task that never terminates, an iterator that loops forever, a non-zero status nobody checked — are exactly the ones that only show up in production.

`FakeWebConnector` replays the real callback sequence and raises if the session fails to terminate, so a runaway iterator fails in milliseconds instead of in the connector's log. `FakeQuickBooks` answers qbXML the way QuickBooks does: paged iterators, `MaxReturned`, status 1 for empty, status 3100 for unsupported. Together they run the whole integration with no Windows and no company file.

## Install

```bash
pip install qbwc-kit                 # core: standard library only
pip install 'qbwc-kit[server]'       # adds the FastAPI adapter
```

The core has no dependencies outside the standard library, and the service is just `dispatch(soap_body) -> soap_body`, so it drops into Flask, Django or bare WSGI unchanged.

Repo: https://github.com/meister5/qbwc-kit · MIT · on PyPI at v0.1.2.
