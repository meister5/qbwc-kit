"""FastAPI adapter.

Kept deliberately thin: the protocol lives in :mod:`qbwc_kit.service`, and this
module only maps HTTP verbs onto it. FastAPI is an optional dependency, so
importing this module without it raises a clear error instead of an
``ImportError`` from three frames down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .service import QBWCService
from .wsdl import build_wsdl

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

try:
    from fastapi import FastAPI, Request, Response
    from starlette.concurrency import run_in_threadpool
except ModuleNotFoundError as exc:  # pragma: no cover - exercised by install shape, not tests
    raise ModuleNotFoundError(
        "qbwc_kit.server needs FastAPI. Install it with: pip install fastapi"
    ) from exc

#: QBWC sends this exact content type and expects it back.
SOAP_CONTENT_TYPE = "text/xml; charset=utf-8"


def create_app(
    service: QBWCService,
    *,
    endpoint_url: str,
    path: str = "/qbwc",
    app: FastAPI | None = None,
    service_name: str = "QBWebConnectorSvc",
) -> FastAPI:
    """Mount ``service`` on a FastAPI app.

    ``endpoint_url`` is the externally reachable URL of ``path``. QBWC reads it
    out of the WSDL and posts there, so behind a reverse proxy it must be the
    public URL, not ``http://localhost``.
    """
    app = app or FastAPI(title="QuickBooks Web Connector service", docs_url=None, redoc_url=None)
    wsdl = build_wsdl(endpoint_url, service_name=service_name)

    @app.get(path)
    async def get_wsdl(request: Request) -> Response:
        # QBWC asks for the WSDL as "?wsdl"; browsers and health checks hit the
        # bare path. Serving the WSDL for both is harmless and saves a support
        # round trip when someone pastes the URL into a browser to check it.
        return Response(content=wsdl, media_type=SOAP_CONTENT_TYPE)

    @app.post(path)
    async def handle_soap(request: Request) -> Response:
        body = await request.body()
        # Tasks are ordinary blocking code - they hit databases and HTTP APIs -
        # and a multi-megabyte qbXML response is not free to parse either.
        # Running dispatch on the event loop would stall every other request
        # for the duration, including a second connector's poll.
        content = await run_in_threadpool(service.dispatch, body)
        return Response(content=content, media_type=SOAP_CONTENT_TYPE)

    return app
