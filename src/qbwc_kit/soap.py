"""Minimal SOAP 1.1 handling for the QuickBooks Web Connector.

The Web Connector is a SOAP client and nothing else. It speaks a fixed set of
eight methods against the ``http://developer.intuit.com/`` namespace, always
with simple string/int payloads. Pulling in a general purpose SOAP stack for
that is overkill, so this module implements exactly the slice that QBWC uses.

Everything here is stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from ._xml import fromstring

QBWC_NS = "http://developer.intuit.com/"
SOAP_ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"

_LOCALNAME = re.compile(r"^(?:\{[^}]*\})?(.+)$")


class SoapError(ValueError):
    """Raised when an incoming envelope is not something QBWC could have sent."""


def localname(tag: str) -> str:
    match = _LOCALNAME.match(tag)
    if match is None:  # pragma: no cover - ElementTree never produces this
        raise SoapError(f"uninterpretable tag: {tag!r}")
    return match.group(1)


def _is_element(node: ET.Element) -> bool:
    """False for comments and processing instructions, whose tag is a callable."""
    return isinstance(node.tag, str)


def _flatten_text(node: ET.Element) -> str:
    """All character data under ``node``, comments excluded.

    An escaped qbXML payload normally arrives as a single text node, but a body
    that passed through something that reformatted it can come back split
    around a comment, and taking only ``node.text`` would silently truncate the
    response document.
    """
    parts = [node.text or ""]
    for child in node:
        if _is_element(child):
            parts.append(_flatten_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


@dataclass(frozen=True)
class SoapCall:
    """A decoded QBWC method call.

    ``params`` preserves document order because two QBWC methods
    (``authenticate`` and ``sendRequestXML``) are positional in practice even
    though the WSDL names every part.
    """

    method: str
    params: dict[str, str]
    order: tuple[str, ...]

    def get(self, name: str, default: str = "") -> str:
        return self.params.get(name, default)

    def positional(self, index: int, default: str = "") -> str:
        try:
            return self.params[self.order[index]]
        except (IndexError, KeyError):
            return default


def parse_request(body: str | bytes) -> SoapCall:
    """Decode a SOAP envelope into a :class:`SoapCall`.

    Raises :class:`SoapError` for anything that is not a single-method
    envelope, which is all the Web Connector ever sends.
    """
    try:
        root = fromstring(body)
    except ET.ParseError as exc:
        raise SoapError(f"malformed XML: {exc}") from exc

    if localname(root.tag) != "Envelope":
        raise SoapError(f"expected a SOAP Envelope, got {localname(root.tag)!r}")

    soap_body = None
    for child in root:
        if _is_element(child) and localname(child.tag) == "Body":
            soap_body = child
            break
    if soap_body is None:
        raise SoapError("envelope has no Body")

    calls = [child for child in soap_body if _is_element(child)]
    if len(calls) != 1:
        raise SoapError(f"expected exactly one method element, got {len(calls)}")

    call = calls[0]
    params: dict[str, str] = {}
    order: list[str] = []
    for param in call:
        if not _is_element(param):
            continue
        name = localname(param.tag)
        params[name] = _flatten_text(param)
        order.append(name)

    return SoapCall(method=localname(call.tag), params=params, order=tuple(order))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_response(method: str, result: str | int | Sequence[str]) -> str:
    """Serialize a return value into the envelope shape QBWC expects.

    QBWC is strict about the wrapper names: a call to ``authenticate`` must come
    back as ``authenticateResponse`` containing ``authenticateResult``. String
    arrays are wrapped in ``<string>`` elements; scalars are inlined.
    """
    if isinstance(result, (list, tuple)):
        inner = "".join(f"<string>{_escape(str(item))}</string>" for item in result)
    elif isinstance(result, bool):  # bool is an int subclass; reject it early
        raise TypeError("QBWC has no boolean return type")
    elif isinstance(result, int):
        inner = str(result)
    else:
        inner = _escape(result)

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}"'
        ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
        "<soap:Body>"
        f'<{method}Response xmlns="{QBWC_NS}">'
        f"<{method}Result>{inner}</{method}Result>"
        f"</{method}Response>"
        "</soap:Body>"
        "</soap:Envelope>"
    )


def build_fault(message: str, code: str = "soap:Server") -> str:
    """Serialize a SOAP fault. QBWC surfaces the faultstring in its log."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}">'
        "<soap:Body><soap:Fault>"
        f"<faultcode>{_escape(code)}</faultcode>"
        f"<faultstring>{_escape(message)}</faultstring>"
        "</soap:Fault></soap:Body></soap:Envelope>"
    )


def build_request(method: str, params: Iterable[tuple[str, str]]) -> str:
    """Build a client-side envelope. Used by the test double in :mod:`qbwc_kit.testing`."""
    inner = "".join(f"<{name}>{_escape(value)}</{name}>" for name, value in params)
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<soap:Envelope xmlns:soap="{SOAP_ENV_NS}">'
        f'<soap:Body><{method} xmlns="{QBWC_NS}">{inner}</{method}></soap:Body>'
        "</soap:Envelope>"
    )
