"""qbXML response parsing.

The response side is where integrations quietly break. A request that returns
``statusCode="1"`` ("nothing found") looks structurally identical to one that
succeeded with zero rows, and an unsupported request comes back as a *success*
envelope carrying a non-zero status. Treating the response as "parse the XML,
take the rows" is how a cache silently degrades into empty results, so this
parser surfaces status on every response and refuses to hand back rows without
it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

from .._xml import fromstring
from .types import STATUS_NOTHING_FOUND, STATUS_OK, Severity


class QBXMLParseError(ValueError):
    pass


class QBXMLStatusError(RuntimeError):
    """Raised by :meth:`Response.raise_for_status` on a non-OK response."""

    def __init__(self, response: Response) -> None:
        super().__init__(f"{response.name}: [{response.status_code}] {response.status_message}")
        self.response = response


def _text(node: ET.Element) -> str:
    return (node.text or "").strip()


def _is_element(node: ET.Element) -> bool:
    """False for comments and processing instructions, whose tag is a callable."""
    return isinstance(node.tag, str)


def _to_dict(node: ET.Element) -> Any:
    """Convert a qbXML aggregate into plain Python.

    Repeated sibling tags become lists, which is how line items, addresses with
    multiple lines, and custom fields all arrive.
    """
    children = [child for child in node if _is_element(child)]
    if not children:
        return _text(node)

    result: dict[str, Any] = {}
    for child in children:
        value = _to_dict(child)
        tag = child.tag
        if tag in result:
            existing = result[tag]
            if isinstance(existing, list):
                existing.append(value)
            else:
                result[tag] = [existing, value]
        else:
            result[tag] = value
    return result


@dataclass
class Response:
    """One ``*Rs`` element out of the batch."""

    name: str
    status_code: int
    status_severity: str
    status_message: str
    request_id: str | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    iterator_remaining_count: int | None = None
    iterator_id: str | None = None

    @property
    def ok(self) -> bool:
        """True when QuickBooks actually processed the request.

        Status 1 ("nothing found") counts as OK: an empty result set is a valid
        answer, unlike status 3100 which means the request never ran.
        """
        return self.status_code in (STATUS_OK, STATUS_NOTHING_FOUND)

    @property
    def empty(self) -> bool:
        return self.status_code == STATUS_NOTHING_FOUND or not self.records

    @property
    def has_more(self) -> bool:
        return bool(self.iterator_remaining_count)

    @property
    def entity(self) -> str:
        """``CustomerQueryRs`` -> ``Customer``."""
        name = self.name
        for suffix in ("QueryRs", "AddRs", "ModRs", "DelRs", "VoidRs", "Rs"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return name

    def raise_for_status(self) -> Response:
        if not self.ok:
            raise QBXMLStatusError(self)
        return self

    def __iter__(self) -> Iterator[dict[str, Any]]:
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


@dataclass
class ResponseSet:
    """The whole ``QBXMLMsgsRs`` batch."""

    responses: list[Response] = field(default_factory=list)

    def __iter__(self) -> Iterator[Response]:
        return iter(self.responses)

    def __len__(self) -> int:
        return len(self.responses)

    def __getitem__(self, index: int) -> Response:
        return self.responses[index]

    @property
    def ok(self) -> bool:
        return all(response.ok for response in self.responses)

    @property
    def failures(self) -> list[Response]:
        return [response for response in self.responses if not response.ok]

    def by_request_id(self, request_id: str) -> Response | None:
        for response in self.responses:
            if response.request_id == request_id:
                return response
        return None

    def first(self, name: str | None = None) -> Response:
        """The first response, optionally the first matching a name or entity.

        Raises :class:`KeyError` rather than returning ``None``: a task that
        asked for a response and got nothing has already gone wrong, and
        failing here beats an ``AttributeError`` two lines later.
        """
        for response in self.responses:
            if name is None or response.name == name or response.entity == name:
                return response
        if name is None:
            raise KeyError("the response set is empty")
        raise KeyError(f"no response named {name!r} in {[r.name for r in self.responses]}")

    def raise_for_status(self) -> ResponseSet:
        for response in self.responses:
            response.raise_for_status()
        return self


#: Elements inside a ``*Rs`` that are metadata rather than returned records.
_NON_RECORD_TAGS = frozenset({"ErrorRecovery"})


def parse_response(payload: str | bytes) -> ResponseSet:
    """Parse a full qbXML response document.

    An empty payload is not an error: QBWC passes an empty string through
    ``receiveResponseXML`` when a request was skipped.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    payload = payload.strip()
    if not payload:
        return ResponseSet()

    try:
        root = fromstring(payload)
    except ET.ParseError as exc:
        raise QBXMLParseError(f"malformed qbXML: {exc}") from exc

    if root.tag != "QBXML":
        raise QBXMLParseError(f"expected a QBXML root, got {root.tag!r}")

    msgs = root.find("QBXMLMsgsRs")
    if msgs is None:
        raise QBXMLParseError("response has no QBXMLMsgsRs")

    responses = [_parse_one(node) for node in msgs if _is_element(node)]
    return ResponseSet(responses=responses)


def _parse_one(node: ET.Element) -> Response:
    try:
        status_code = int(node.get("statusCode", "0"))
    except ValueError as exc:
        raise QBXMLParseError(f"non-numeric statusCode on {node.tag}") from exc

    remaining = node.get("iteratorRemainingCount")
    try:
        remaining_count = int(remaining) if remaining is not None else None
    except ValueError as exc:
        raise QBXMLParseError(f"non-numeric iteratorRemainingCount on {node.tag}") from exc

    response = Response(
        name=node.tag,
        status_code=status_code,
        status_severity=node.get("statusSeverity", Severity.INFO.value),
        status_message=node.get("statusMessage", ""),
        request_id=node.get("requestID"),
        iterator_remaining_count=remaining_count,
        iterator_id=node.get("iteratorID"),
    )

    for child in node:
        if not _is_element(child) or child.tag in _NON_RECORD_TAGS:
            continue
        value = _to_dict(child)
        response.records.append(value if isinstance(value, dict) else {child.tag: value})

    return response
