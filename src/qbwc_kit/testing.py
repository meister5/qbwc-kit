"""Test doubles: a fake Web Connector and a fake QuickBooks.

The reason this module exists is that the real integration cannot be exercised
without a Windows machine, a QuickBooks Desktop install, an open company file,
and a human clicking "Update Selected" in the Web Connector. That makes the
obvious failure modes — a task that never terminates, an iterator that loops
forever, a status code nobody checked — exactly the ones that only show up in
production.

:class:`FakeWebConnector` replays the real call sequence (``clientVersion`` ->
``authenticate`` -> ``sendRequestXML``/``receiveResponseXML`` loop ->
``closeConnection``) against a service, and :class:`FakeQuickBooks` answers
qbXML the way QuickBooks does, including iterators and non-zero statuses.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from . import soap
from ._xml import fromstring
from .qbxml.builder import escape
from .qbxml.types import STATUS_OBJECT_NOT_FOUND, STATUS_STALE_EDIT_SEQUENCE
from .service import QBWCService

#: Guard against a task whose iterator never terminates.
DEFAULT_MAX_ROUND_TRIPS = 200


#: A small customer list, handy for wiring up a first test.
CUSTOMERS_FIXTURE: list[dict[str, Any]] = [
    {"ListID": "80000001-1", "EditSequence": "1", "Name": "Acme Hardware"},
    {"ListID": "80000002-1", "EditSequence": "1", "Name": "Globex Supply"},
    {"ListID": "80000003-1", "EditSequence": "1", "Name": "Initech Fasteners"},
]


class Transport(Protocol):
    """Anything that turns a SOAP request body into a SOAP response body."""

    def __call__(self, body: str) -> str: ...


def service_transport(service: QBWCService) -> Transport:
    """Call a service in-process, skipping HTTP entirely."""

    def transport(body: str) -> str:
        return service.dispatch(body)

    return transport


def _result(envelope: str) -> Any:
    """Pull the ``*Result`` payload out of a response envelope."""
    root = fromstring(envelope)
    body = next((child for child in root if soap.localname(child.tag) == "Body"), None)
    if body is None or not len(body):
        # Usually a transport that answered with a plain error page. Saying so
        # beats the StopIteration or IndexError this used to raise.
        raise AssertionError(f"expected a SOAP envelope with a body, got: {envelope[:200]}")

    first = list(body)[0]
    if soap.localname(first.tag) == "Fault":
        message = "".join(
            (node.text or "") for node in first if soap.localname(node.tag) == "faultstring"
        )
        raise AssertionError(f"service returned a SOAP fault: {message}")

    result = list(first)[0]
    strings = [node.text or "" for node in result if soap.localname(node.tag) == "string"]
    if strings:
        return strings
    return result.text or ""


@dataclass
class UpdateResult:
    """Everything that happened during one simulated QBWC update."""

    ticket: str = ""
    company_file: str = ""
    requests: list[str] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)
    progress: list[int] = field(default_factory=list)
    authenticated: bool = False
    close_message: str = ""
    last_error: str = ""

    @property
    def round_trips(self) -> int:
        return len(self.requests)

    @property
    def failed(self) -> bool:
        return any(value < 0 for value in self.progress)


@dataclass
class FakeWebConnector:
    """Drives a QBWC service exactly the way the real connector does."""

    transport: Transport
    username: str = "webconnector"
    password: str = "password"
    client_version: str = "2.3.0.36"
    company_file: str = r"C:\QuickBooks\Test.QBW"
    country: str = "US"
    qbxml_version: tuple[int, int] = (13, 0)
    max_round_trips: int = DEFAULT_MAX_ROUND_TRIPS

    def _call(self, method: str, params: list[tuple[str, str]]) -> Any:
        return _result(self.transport(soap.build_request(method, params)))

    def server_version(self) -> str:
        # serverVersion takes no arguments, matching Intuit's own WSDL.
        return self._call("serverVersion", [])

    def connection_error(self, ticket: str, hresult: str, message: str) -> str:
        """Report that QBWC could not reach QuickBooks, as the real one does."""
        return self._call(
            "connectionError",
            [("ticket", ticket), ("hresult", hresult), ("message", message)],
        )

    def last_error(self, ticket: str) -> str:
        return self._call("getLastError", [("ticket", ticket)])

    def run_update(self, quickbooks: Responder | None = None) -> UpdateResult:
        """Run a full update cycle and return a transcript of it.

        ``quickbooks`` receives each qbXML request and returns the response;
        :class:`FakeQuickBooks` is the usual choice.
        """
        responder: Responder = quickbooks if quickbooks is not None else FakeQuickBooks()
        result = UpdateResult()

        version_check = self._call("clientVersion", [("strVersion", self.client_version)])
        if isinstance(version_check, str) and version_check.startswith("E:"):
            result.last_error = version_check
            return result

        auth = self._call(
            "authenticate",
            [("strUserName", self.username), ("strPassword", self.password)],
        )
        result.ticket, status = auth[0], auth[1]
        if not result.ticket or status in ("nvu", "none"):
            result.last_error = status
            return result

        result.authenticated = True
        result.company_file = status or self.company_file

        for _ in range(self.max_round_trips):
            request = self._call(
                "sendRequestXML",
                [
                    ("ticket", result.ticket),
                    ("strHCPResponse", ""),
                    ("strCompanyFileName", result.company_file),
                    ("qbXMLCountry", self.country),
                    ("qbXMLMajorVers", str(self.qbxml_version[0])),
                    ("qbXMLMinorVers", str(self.qbxml_version[1])),
                ],
            )
            if not request:
                break

            result.requests.append(request)
            response = responder(request)
            result.responses.append(response)

            percent = int(
                self._call(
                    "receiveResponseXML",
                    [
                        ("ticket", result.ticket),
                        ("response", response),
                        ("hresult", ""),
                        ("message", ""),
                    ],
                )
            )
            result.progress.append(percent)
            if percent < 0:
                result.last_error = self._call("getLastError", [("ticket", result.ticket)])
                break
            if percent >= 100:
                break
        else:
            raise AssertionError(
                f"session did not terminate within {self.max_round_trips} round trips; "
                "a task is probably looping on an iterator"
            )

        result.close_message = self._call("closeConnection", [("ticket", result.ticket)])
        return result


class Responder(Protocol):
    def __call__(self, request_xml: str) -> str: ...


#: qbXML leads with an XML declaration and a ``<?qbxml ...?>`` instruction.
#: ElementTree copes with those, but stripping them keeps the fake tolerant of
#: hand-written fragments too.
_PROCESSING_INSTRUCTION = re.compile(r"^\s*(?:<\?.*?\?>\s*)+")


def _status_attrs(code: int, message: str, severity: str | None = None) -> str:
    if severity is None:
        severity = "Info" if code in (0, 1) else "Error"
    return f'statusCode="{code}" statusSeverity="{severity}" statusMessage="{escape(message)}"'


def _aggregate_to_dict(node: ET.Element | None) -> dict[str, Any]:
    """Turn an ``<EntityAdd>`` / ``<EntityMod>`` aggregate into a record.

    Nested aggregates (``BillAddress``, ``CustomerRef``) are kept as nested
    dicts so that what comes back out of a Query looks like what went in.
    """
    if node is None:
        return {}
    record: dict[str, Any] = {}
    for child in node:
        value: Any = _aggregate_to_dict(child) if len(child) else (child.text or "").strip()
        if child.tag in record:
            existing = record[child.tag]
            record[child.tag] = (
                [*existing, value]
                if isinstance(existing, list)
                else [
                    existing,
                    value,
                ]
            )
        else:
            record[child.tag] = value
    return record


def _render_record(entity: str, record: dict[str, Any]) -> str:
    """Render a record as an ``<EntityRet>`` aggregate.

    A list repeats its own tag rather than nesting its items, because that is
    how QuickBooks returns line items: two ``<InvoiceLineRet>`` siblings, not
    one element holding both.
    """

    def render(name: str, value: Any) -> str:
        if isinstance(value, list):
            return "".join(render(name, item) for item in value)
        if isinstance(value, dict):
            return f"<{name}>{fields(value)}</{name}>"
        return f"<{name}>{escape(value)}</{name}>"

    def fields(mapping: dict[str, Any]) -> str:
        return "".join(render(key, value) for key, value in mapping.items())

    return f"<{entity}Ret>{fields(record)}</{entity}Ret>"


@dataclass
class FakeQuickBooks:
    """A qbXML responder backed by in-memory records.

    Supports the behaviour that actually matters when testing a sync: paged
    iterators, ``MaxReturned``, status 1 for an empty result, status 3100 for
    an unsupported request, monotonically increasing ListIDs on Add, and the
    optimistic-concurrency check on Mod - a stale ``EditSequence`` is refused
    with status 3200 exactly as QuickBooks refuses it.
    """

    entities: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    supported: set[str] | None = None
    unsupported_message: str = "This feature is not available in this edition"

    _iterators: dict[str, int] = field(default_factory=dict, init=False)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), init=False)
    _iterator_ids: itertools.count = field(default_factory=lambda: itertools.count(1), init=False)

    #: Every request this responder has seen, in order. Useful for asserting
    #: that a task actually sent what it claimed to.
    seen: list[str] = field(default_factory=list, init=False)

    def __call__(self, request_xml: str) -> str:
        self.seen.append(request_xml)
        root = fromstring(_PROCESSING_INSTRUCTION.sub("", request_xml).lstrip())
        msgs = root.find("QBXMLMsgsRq")
        if msgs is None:
            raise AssertionError("request has no QBXMLMsgsRq")

        bodies = [self._handle(node) for node in msgs]
        return (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<QBXML><QBXMLMsgsRs>" + "".join(bodies) + "</QBXMLMsgsRs></QBXML>"
        )

    def _handle(self, node: ET.Element) -> str:
        name = node.tag
        response_name = name[:-2] + "Rs" if name.endswith("Rq") else name + "Rs"
        request_id = node.get("requestID")
        attrs = f' requestID="{request_id}"' if request_id else ""

        entity = self._entity_of(name)
        if self.supported is not None and entity not in self.supported:
            return f"<{response_name}{attrs} {_status_attrs(3100, self.unsupported_message)}/>"

        if name.endswith("QueryRq"):
            return self._query(response_name, attrs, entity, node)
        if name.endswith("AddRq"):
            return self._add(response_name, attrs, entity, node)
        if name.endswith("ModRq"):
            return self._mod(response_name, attrs, entity, node)

        return f"<{response_name}{attrs} {_status_attrs(3100, self.unsupported_message)}/>"

    @staticmethod
    def _entity_of(request_name: str) -> str:
        for suffix in ("QueryRq", "AddRq", "ModRq", "DelRq", "Rq"):
            if request_name.endswith(suffix):
                return request_name[: -len(suffix)]
        return request_name

    def _query(self, response_name: str, attrs: str, entity: str, node: ET.Element) -> str:
        records = self.entities.get(entity, [])
        max_returned_node = node.find("MaxReturned")
        max_returned = int(max_returned_node.text or 0) if max_returned_node is not None else None

        iterator = node.get("iterator")
        iterator_id = node.get("iteratorID")
        offset = 0
        if iterator == "Continue" and iterator_id is not None:
            offset = self._iterators.get(iterator_id, 0)
        elif iterator == "Start":
            iterator_id = f"iter-{next(self._iterator_ids)}"

        page = records[offset:]
        if max_returned is not None:
            page = page[:max_returned]

        if not page and offset == 0:
            return f"<{response_name}{attrs} {_status_attrs(1, 'No matching records')}/>"

        extra = ""
        if iterator is not None and iterator_id is not None:
            remaining = max(len(records) - (offset + len(page)), 0)
            self._iterators[iterator_id] = offset + len(page)
            extra = f' iteratorRemainingCount="{remaining}" iteratorID="{iterator_id}"'

        body = "".join(_render_record(entity, record) for record in page)
        return f"<{response_name}{attrs}{extra} {_status_attrs(0, 'Status OK')}>{body}</{response_name}>"

    def _add(self, response_name: str, attrs: str, entity: str, node: ET.Element) -> str:
        record = _aggregate_to_dict(node.find(f"{entity}Add"))
        record.setdefault("ListID", f"{entity.upper()}-{next(self._ids)}")
        record.setdefault("EditSequence", "1")
        self.entities.setdefault(entity, []).append(record)
        body = _render_record(entity, record)
        return f"<{response_name}{attrs} {_status_attrs(0, 'Status OK')}>{body}</{response_name}>"

    def _mod(self, response_name: str, attrs: str, entity: str, node: ET.Element) -> str:
        changes = _aggregate_to_dict(node.find(f"{entity}Mod"))
        key = "TxnID" if "TxnID" in changes else "ListID"
        identifier = changes.get(key)

        records = self.entities.setdefault(entity, [])
        target = next((r for r in records if r.get(key) == identifier), None)
        if target is None:
            message = f"There is no {entity} with {key} {identifier}"
            return f"<{response_name}{attrs} {_status_attrs(STATUS_OBJECT_NOT_FOUND, message)}/>"

        # Optimistic concurrency: QuickBooks refuses a modification carrying an
        # EditSequence that is not the current one, rather than clobbering the
        # edit that bumped it.
        if changes.get("EditSequence") != target.get("EditSequence"):
            message = "The object you are trying to modify has been changed by another user"
            return f"<{response_name}{attrs} {_status_attrs(STATUS_STALE_EDIT_SEQUENCE, message)}/>"

        target.update({k: v for k, v in changes.items() if k != "EditSequence"})
        target["EditSequence"] = str(int(target.get("EditSequence", "1")) + 1)
        body = _render_record(entity, target)
        return f"<{response_name}{attrs} {_status_attrs(0, 'Status OK')}>{body}</{response_name}>"
