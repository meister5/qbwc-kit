"""The eight QBWC callbacks, as a framework-agnostic service.

:class:`QBWCService` takes a raw SOAP body and returns a raw SOAP body. It has
no web framework dependency at all, which keeps the protocol logic testable
without spinning up a server and lets the same service be mounted under
FastAPI, Flask, or a bare WSGI callable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from . import soap
from .session import (
    CURRENT_COMPANY_FILE,
    INVALID_USER,
    NO_WORK,
    Authenticator,
    Session,
    SessionStore,
    UnknownTicket,
)

logger = logging.getLogger("qbwc_kit")

#: Version this service reports to QBWC.
SERVER_VERSION = "1.0.0"
#: QBWC builds older than this are rejected outright. 2.0.x predates the
#: version negotiation that everything below relies on.
MIN_CLIENT_VERSION = (2, 1, 0, 30)


def _parse_version(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in text.strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _as_int(text: str, default: int) -> int:
    """Parse an integer callback parameter, tolerating whatever QBWC sent.

    The qbXML version numbers arrive as strings and are only used to fill in
    :class:`~qbwc_kit.session.TaskContext`. Faulting the whole call because one
    of them was blank or malformed would make QBWC retry the update forever.
    """
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


@dataclass
class QBWCService:
    """Implements the QBWC contract on top of an :class:`Authenticator`.

    ``on_session_end`` fires exactly once per session, whether it ended
    cleanly, through ``connectionError``, or by QBWC dropping the connection
    and the ticket ageing out.
    """

    authenticator: Authenticator
    store: SessionStore = field(default_factory=SessionStore)
    server_version: str = SERVER_VERSION
    min_client_version: tuple[int, ...] = MIN_CLIENT_VERSION
    on_session_end: Callable[[Session], None] | None = None

    def __post_init__(self) -> None:
        if self.store is None:  # explicit store=None used to mean "give me one"
            self.store = SessionStore()
        # A session that ages out of the store never sees closeConnection, so
        # route evictions through the same hook to keep the "exactly once"
        # promise above honest.
        if self.store.on_evict is None:
            self.store.on_evict = self._end_session

    # -- SOAP entry point -------------------------------------------------

    def dispatch(self, body: str | bytes) -> str:
        """Handle one SOAP request and return the response envelope."""
        try:
            call = soap.parse_request(body)
        except soap.SoapError as exc:
            logger.warning("rejected non-QBWC request: %s", exc)
            return soap.build_fault(str(exc), code="soap:Client")

        handler = getattr(self, f"_do_{call.method}", None)
        if handler is None:
            logger.warning("unknown QBWC method %r", call.method)
            return soap.build_fault(f"unsupported method {call.method!r}", code="soap:Client")

        try:
            result = handler(call)
        except UnknownTicket as exc:
            # An unknown ticket is normal after a restart. Tell QBWC the
            # session is over rather than faulting, so it stops cleanly.
            logger.info("unknown ticket %s", exc)
            return soap.build_response(call.method, self._unknown_ticket_result(call.method))
        except Exception as exc:  # noqa: BLE001 - a fault is the protocol's error channel
            logger.exception("error handling %s", call.method)
            return soap.build_fault(f"{type(exc).__name__}: {exc}")

        return soap.build_response(call.method, result)

    @staticmethod
    def _unknown_ticket_result(method: str) -> str | int | list[str]:
        if method == "sendRequestXML":
            return ""
        if method == "receiveResponseXML":
            return -1
        if method == "getLastError":
            return "session expired"
        return "done"

    # -- callbacks --------------------------------------------------------

    def _do_serverVersion(self, call: soap.SoapCall) -> str:
        return self.server_version

    def _do_clientVersion(self, call: soap.SoapCall) -> str:
        """Vet the connector build.

        The return value is a tagged string: ``""`` accepts, ``W:`` warns and
        continues, ``E:`` refuses the update outright.
        """
        raw = call.get("strVersion") or call.positional(0)
        if not raw:
            return ""
        if _parse_version(raw) < self.min_client_version:
            wanted = ".".join(str(p) for p in self.min_client_version)
            return f"E:Web Connector {raw} is too old; {wanted} or newer is required"
        return ""

    def _do_authenticate(self, call: soap.SoapCall) -> list[str]:
        """Return ``[ticket, company-file-or-status]``.

        Slot 1 carries three different meanings, which is why it is spelled out
        with named constants: a path or ``""`` starts work on that company
        file, ``"none"`` means authenticated-but-idle, and ``"nvu"`` means the
        credentials were rejected.
        """
        username = call.get("strUserName") or call.positional(0)
        password = call.get("strPassword") or call.positional(1)

        if not self.authenticator.authenticate(username, password):
            logger.info("authentication failed for %r", username)
            return ["", INVALID_USER]

        tasks = list(self.authenticator.tasks_for(username))
        if not tasks:
            # Nothing queued. Returning "none" lets QBWC finish its poll
            # quickly instead of opening the company file for no reason.
            return ["", NO_WORK]

        session = self.store.create(username, tasks)
        logger.info(
            "session %s opened for %r with %d task(s)", session.ticket[:8], username, len(tasks)
        )
        return [session.ticket, CURRENT_COMPANY_FILE]

    def _do_sendRequestXML(self, call: soap.SoapCall) -> str:
        session = self.store.get(call.get("ticket") or call.positional(0))
        ctx = session.context
        ctx.company_file = call.get("strCompanyFileName", ctx.company_file)
        ctx.country = call.get("qbXMLCountry") or ctx.country
        ctx.major_version = _as_int(call.get("qbXMLMajorVers"), ctx.major_version)
        ctx.minor_version = _as_int(call.get("qbXMLMinorVers"), ctx.minor_version)

        # A task that fails while building its request has retired itself, so
        # keep asking: one broken task should not cancel the ones behind it.
        # The bound is the task count, since every failure consumes one.
        for _ in range(len(session.tasks) - session.index + 1):
            attempted = session.index
            try:
                request = session.next_request()
            except Exception as exc:  # noqa: BLE001 - surface via getLastError, not a fault
                session.record_error(f"{type(exc).__name__}: {exc}")
                logger.exception("task %s failed while building a request", attempted)
                if session.index != attempted:
                    continue
                break  # the session itself is wedged; retrying would repeat it
            if request:
                return request
            break

        logger.info("session %s has no further requests", session.ticket[:8])
        return ""

    def _do_receiveResponseXML(self, call: soap.SoapCall) -> int:
        """Return percent complete; 100 ends the session, negative aborts it."""
        session = self.store.get(call.get("ticket") or call.positional(0))
        response = call.get("response") or call.positional(1)
        hresult = call.get("hresult")
        message = call.get("message")

        if hresult:
            # QuickBooks itself failed (company file locked, permission
            # revoked). The response body is meaningless in this case.
            session.record_error(f"QuickBooks error {hresult}: {message}")
            logger.error("QuickBooks error %s: %s", hresult, message)
            return -1

        try:
            session.submit_response(response)
        except Exception as exc:  # noqa: BLE001
            session.record_error(f"{type(exc).__name__}: {exc}")
            logger.exception("task %s failed while handling a response", session.index)
            return -1

        return session.progress()

    def _do_connectionError(self, call: soap.SoapCall) -> str:
        """QBWC could not reach QuickBooks.

        Returning ``"done"`` gives up; returning a company file path asks QBWC
        to retry against that file. Giving up is the honest answer when the
        server has no way to know which file the user meant.
        """
        ticket = call.get("ticket") or call.positional(0)
        hresult = call.get("hresult")
        message = call.get("message")
        try:
            session = self.store.get(ticket)
        except UnknownTicket:
            return "done"
        session.record_error(f"connection error {hresult}: {message}")
        logger.error("connection error on session %s: %s %s", ticket[:8], hresult, message)
        return "done"

    def _do_getLastError(self, call: soap.SoapCall) -> str:
        session = self.store.get(call.get("ticket") or call.positional(0))
        return session.last_error() or "no error recorded"

    def _end_session(self, session: Session) -> None:
        """Fire ``on_session_end``, never letting it break the callback.

        This runs while answering ``closeConnection`` (or while pruning), and a
        reporting hook that raises must not turn a finished sync into a SOAP
        fault that QBWC will retry.
        """
        if self.on_session_end is None:
            return
        try:
            self.on_session_end(session)
        except Exception:  # noqa: BLE001 - the sync itself already succeeded
            logger.exception("on_session_end failed for session %s", session.ticket[:8])

    def _do_closeConnection(self, call: soap.SoapCall) -> str:
        ticket = call.get("ticket") or call.positional(0)
        session = self.store.close(ticket)
        if session is None:
            return "OK"
        self._end_session(session)
        if session.errors:
            logger.warning("session %s closed with %d error(s)", ticket[:8], len(session.errors))
            return f"Completed with {len(session.errors)} error(s)"
        logger.info("session %s closed cleanly", ticket[:8])
        return "OK"
