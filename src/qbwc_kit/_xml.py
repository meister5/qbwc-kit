"""Hardened XML parsing.

The SOAP endpoint is reachable from the network, and :mod:`xml.etree` expands
internal entities, so a document that declares ``<!ENTITY>`` recursively (the
"billion laughs" attack) turns a few hundred bytes of request body into
gigabytes of memory before the parse finishes.

Neither SOAP-from-QBWC nor qbXML-from-QuickBooks ever carries a DTD, so the
cheapest complete defence is to refuse documents that declare one. The C
accelerator behind ``ET.XMLParser`` exposes no handle on the expat handlers, so
the check runs over the prolog instead of inside the parser.
"""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

#: How much of the document to scan for a DOCTYPE. The prolog is tiny in
#: practice, and scanning a multi-megabyte response for one is wasted work.
_PROLOG_WINDOW = 8192

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_ROOT_START = re.compile(r"<[A-Za-z_]")


def _prolog(text: str) -> str:
    """The markup before the root element, comments removed."""
    head = _COMMENT.sub("", text[:_PROLOG_WINDOW])
    match = _ROOT_START.search(head)
    if match is not None:
        return head[: match.start()]
    # No root element inside the window: an unusually long prolog, or a
    # document that is not XML at all. Fall back to scanning everything.
    head = _COMMENT.sub("", text)
    match = _ROOT_START.search(head)
    return head[: match.start()] if match is not None else head


def fromstring(text: str | bytes) -> ET.Element:
    """Parse ``text``, rejecting any document that declares a DTD.

    Bytes are handed to the parser unchanged so that the encoding declared in
    the XML prolog wins, which is not always UTF-8: QBWC is a Windows service
    and some builds announce a code page.

    Raises :class:`xml.etree.ElementTree.ParseError` for malformed input and
    for a forbidden DTD, so callers can treat both as "this is not a document
    we accept" without a second except clause.
    """
    # The DOCTYPE scan only needs to recognise ASCII markup, so a lossy decode
    # is enough and cannot fail on a mislabelled body.
    scanned = text.decode("utf-8", "replace") if isinstance(text, bytes) else text
    if "<!DOCTYPE" in _prolog(scanned):
        raise ET.ParseError("document type declarations are not allowed")
    return ET.fromstring(text)
