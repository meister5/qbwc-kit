"""qbXML request construction.

QuickBooks Desktop only accepts a very particular document: a ``?qbxml``
processing instruction, a ``QBXML`` root, and a ``QBXMLMsgsRq`` element whose
``onError`` attribute decides whether the whole batch aborts on the first bad
request. Getting any of that wrong produces an unhelpful parse error from
QuickBooks, so it is worth building rather than templating by hand.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .types import OnError, is_transaction, iterator_supported

_ESCAPES = (
    ("&", "&amp;"),
    ("<", "&lt;"),
    (">", "&gt;"),
    ('"', "&quot;"),
)


def escape(value: Any) -> str:
    text = "" if value is None else str(value)
    for char, replacement in _ESCAPES:
        text = text.replace(char, replacement)
    return text


def element(name: str, value: Any) -> str:
    """One element. ``None`` renders nothing so optional fields can be passed through.

    A mapping renders as a nested aggregate (``BillAddress``, ``CustomerRef``)
    and a list or tuple as the same element repeated, which is how qbXML spells
    line items. Everything else is a leaf, and its text is escaped: a string
    that happens to look like markup is data, not structure.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        value = "true" if value else "false"
    if isinstance(value, Mapping):
        return f"<{name}>{elements(value)}</{name}>"
    if isinstance(value, (list, tuple)):
        return "".join(element(name, item) for item in value)
    return f"<{name}>{escape(value)}</{name}>"


def elements(fields: Mapping[str, Any] | Sequence[tuple[str, Any]]) -> str:
    """Render an ordered mapping of elements.

    qbXML is order-sensitive: the schema is a sequence, not a set. Python dicts
    preserve insertion order, which is exactly the guarantee this relies on.
    """
    items = fields.items() if isinstance(fields, Mapping) else fields
    return "".join(element(name, value) for name, value in items)


def ref(name: str, full_name: str | None = None, list_id: str | None = None) -> str:
    """A ``*Ref`` aggregate. QuickBooks accepts either a ListID or a FullName."""
    if full_name is None and list_id is None:
        return ""
    body = element("ListID", list_id) + element("FullName", full_name)
    return f"<{name}>{body}</{name}>"


@dataclass
class Request:
    """A single ``*Rq`` element inside the batch."""

    name: str
    body: str = ""
    request_id: str | None = None
    iterator: str | None = None
    iterator_id: str | None = None
    max_returned: int | None = None

    def render(self) -> str:
        attrs = ""
        if self.request_id is not None:
            attrs += f' requestID="{escape(self.request_id)}"'
        if (self.iterator is not None or self.iterator_id is not None) and not iterator_supported(
            self.name
        ):
            raise ValueError(f"{self.name} does not support iterators")
        if self.iterator is not None:
            attrs += f' iterator="{escape(self.iterator)}"'
        if self.iterator_id is not None:
            attrs += f' iteratorID="{escape(self.iterator_id)}"'

        body = self.body
        if self.max_returned is not None:
            # MaxReturned must lead the query body per the schema sequence.
            body = element("MaxReturned", self.max_returned) + body

        return f"<{self.name}{attrs}>{body}</{self.name}>"


@dataclass
class QBXMLRequest:
    """A qbXML batch, ready to hand back from ``sendRequestXML``."""

    requests: list[Request] = field(default_factory=list)
    on_error: OnError = OnError.STOP
    version: str = "13.0"

    def add(self, request: Request) -> QBXMLRequest:
        self.requests.append(request)
        return self

    def extend(self, requests: Iterable[Request]) -> QBXMLRequest:
        self.requests.extend(requests)
        return self

    def render(self) -> str:
        if not self.requests:
            raise ValueError("a qbXML batch needs at least one request")
        body = "".join(request.render() for request in self.requests)
        # Accept a bare "stopOnError" as readily as the enum member.
        on_error = OnError(self.on_error)
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<?qbxml version="{escape(self.version)}"?>'
            "<QBXML>"
            f'<QBXMLMsgsRq onError="{on_error.value}">'
            f"{body}"
            "</QBXMLMsgsRq>"
            "</QBXML>"
        )

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.render()


def query(
    entity: str,
    *,
    request_id: str | None = None,
    max_returned: int | None = None,
    iterator: str | None = None,
    iterator_id: str | None = None,
    modified_after: str | None = None,
    modified_before: str | None = None,
    active_status: str | None = None,
    include_fields: Sequence[str] | None = None,
    owner_id: str | None = None,
    extra: Mapping[str, Any] | None = None,
    transaction: bool | None = None,
) -> Request:
    """Build a ``<Entity>QueryRq``.

    ``modified_after`` is the workhorse for incremental syncs: pair it with the
    last successful sync timestamp and QuickBooks returns only what changed.

    The two families of query spell that filter differently, which is the kind
    of asymmetry that costs an afternoon:

    * list queries (Customer, Vendor, Item, Account, ...) take bare
      ``<FromModifiedDate>`` / ``<ToModifiedDate>`` children, after
      ``<ActiveStatus>``;
    * transaction queries (Invoice, Bill, Check, ...) wrap the same two
      elements in a ``<ModifiedDateRangeFilter>`` and have no ``ActiveStatus``.

    The family is inferred from ``entity``; pass ``transaction=`` explicitly for
    an entity this library does not know about.
    """
    if transaction is None:
        transaction = is_transaction(entity)

    body = ""
    if transaction:
        if modified_after is not None or modified_before is not None:
            body += (
                "<ModifiedDateRangeFilter>"
                + element("FromModifiedDate", modified_after)
                + element("ToModifiedDate", modified_before)
                + "</ModifiedDateRangeFilter>"
            )
        body += element("ActiveStatus", active_status)
    else:
        body += element("ActiveStatus", active_status)
        body += element("FromModifiedDate", modified_after)
        body += element("ToModifiedDate", modified_before)
    if extra:
        body += elements(extra)
    # IncludeRetElement then OwnerID: both trail the filters, in that order.
    if include_fields:
        body += "".join(element("IncludeRetElement", name) for name in include_fields)
    body += element("OwnerID", owner_id)

    return Request(
        name=f"{entity}QueryRq",
        body=body,
        request_id=request_id,
        iterator=iterator,
        iterator_id=iterator_id,
        max_returned=max_returned,
    )


def add(entity: str, fields: Mapping[str, Any], *, request_id: str | None = None) -> Request:
    """Build an ``<Entity>AddRq`` wrapping an ``<Entity>Add`` aggregate.

    Nested aggregates go in as nested mappings and repeated ones as lists::

        add("Invoice", {
            "CustomerRef": {"FullName": "Acme"},
            "InvoiceLineAdd": [{"Amount": "10.00"}, {"Amount": "5.00"}],
        })

    Passing a string builds nothing: it is escaped and rendered as text. Hand
    ``fields`` a pre-rendered fragment (from :func:`ref`, say) only as the whole
    body, where it is used verbatim.
    """
    inner = elements(fields) if isinstance(fields, Mapping) else str(fields)
    return Request(
        name=f"{entity}AddRq",
        body=f"<{entity}Add>{inner}</{entity}Add>",
        request_id=request_id,
    )


def mod(
    entity: str,
    fields: Mapping[str, Any],
    *,
    list_id: str | None = None,
    txn_id: str | None = None,
    edit_sequence: str,
    request_id: str | None = None,
) -> Request:
    """Build an ``<Entity>ModRq``.

    QuickBooks uses optimistic concurrency: every modification must carry the
    ``EditSequence`` returned by the last read, and a stale one is rejected
    rather than silently overwriting somebody else's edit.
    """
    if list_id is None and txn_id is None:
        raise ValueError("a Mod request needs either a ListID or a TxnID")
    head = element("ListID", list_id) + element("TxnID", txn_id)
    head += element("EditSequence", edit_sequence)
    return Request(
        name=f"{entity}ModRq",
        body=f"<{entity}Mod>{head}{elements(fields)}</{entity}Mod>",
        request_id=request_id,
    )
