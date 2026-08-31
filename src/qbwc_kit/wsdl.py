"""WSDL generation.

The Web Connector fetches the WSDL before its first call and will not proceed
without one, so serving a correct document is part of implementing the
protocol rather than an optional nicety. The schema below is the QBWC contract:
eight operations, string and int parts only, one string-array return type.
"""

from __future__ import annotations

import re
from xml.sax.saxutils import escape, quoteattr

from .soap import QBWC_NS

# ``serverVersion`` takes no arguments; every other operation is named after
# the parts Intuit's own QBWebConnectorSvc.wsdl declares.
_OPERATIONS: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = (
    ("serverVersion", (), "s:string"),
    ("clientVersion", (("strVersion", "s:string"),), "s:string"),
    (
        "authenticate",
        (("strUserName", "s:string"), ("strPassword", "s:string")),
        "tns:ArrayOfString",
    ),
    (
        "sendRequestXML",
        (
            ("ticket", "s:string"),
            ("strHCPResponse", "s:string"),
            ("strCompanyFileName", "s:string"),
            ("qbXMLCountry", "s:string"),
            ("qbXMLMajorVers", "s:int"),
            ("qbXMLMinorVers", "s:int"),
        ),
        "s:string",
    ),
    (
        "receiveResponseXML",
        (
            ("ticket", "s:string"),
            ("response", "s:string"),
            ("hresult", "s:string"),
            ("message", "s:string"),
        ),
        "s:int",
    ),
    (
        "connectionError",
        (("ticket", "s:string"), ("hresult", "s:string"), ("message", "s:string")),
        "s:string",
    ),
    ("getLastError", (("ticket", "s:string"),), "s:string"),
    ("closeConnection", (("ticket", "s:string"),), "s:string"),
)


def _schema_elements() -> str:
    parts: list[str] = []
    for name, params, return_type in _OPERATIONS:
        request_body = "".join(
            f'<s:element minOccurs="0" maxOccurs="1" name="{param}" type="{ptype}"/>'
            if ptype == "s:string"
            else f'<s:element minOccurs="1" maxOccurs="1" name="{param}" type="{ptype}"/>'
            for param, ptype in params
        )
        parts.append(
            f'<s:element name="{name}"><s:complexType><s:sequence>'
            f"{request_body}"
            "</s:sequence></s:complexType></s:element>"
        )
        occurs = (
            'minOccurs="0" maxOccurs="1"'
            if return_type != "s:int"
            else 'minOccurs="1" maxOccurs="1"'
        )
        parts.append(
            f'<s:element name="{name}Response"><s:complexType><s:sequence>'
            f'<s:element {occurs} name="{name}Result" type="{return_type}"/>'
            "</s:sequence></s:complexType></s:element>"
        )
    return "".join(parts)


def _port_type() -> str:
    return "".join(
        f'<wsdl:operation name="{name}">'
        f'<wsdl:input message="tns:{name}SoapIn"/>'
        f'<wsdl:output message="tns:{name}SoapOut"/>'
        "</wsdl:operation>"
        for name, _params, _ret in _OPERATIONS
    )


def _messages() -> str:
    return "".join(
        f'<wsdl:message name="{name}SoapIn">'
        f'<wsdl:part name="parameters" element="tns:{name}"/></wsdl:message>'
        f'<wsdl:message name="{name}SoapOut">'
        f'<wsdl:part name="parameters" element="tns:{name}Response"/></wsdl:message>'
        for name, _params, _ret in _OPERATIONS
    )


def _binding() -> str:
    return "".join(
        f'<wsdl:operation name="{name}">'
        f'<soap:operation soapAction="{QBWC_NS}{name}" style="document"/>'
        '<wsdl:input><soap:body use="literal"/></wsdl:input>'
        '<wsdl:output><soap:body use="literal"/></wsdl:output>'
        "</wsdl:operation>"
        for name, _params, _ret in _OPERATIONS
    )


#: A WSDL service name lands in element names and QName references, so it has
#: to be an XML NCName rather than free text.
_NCNAME = re.compile(r"^[A-Za-z_][\w.\-]*$")


def build_wsdl(endpoint_url: str, service_name: str = "QBWebConnectorSvc") -> str:
    """Render the WSDL for a service mounted at ``endpoint_url``.

    The endpoint in ``soap:address`` must be the URL QBWC actually POSTs to,
    including scheme and port; a mismatch there produces a connector error
    that gives no hint about the cause.
    """
    if not _NCNAME.match(service_name):
        raise ValueError(f"service_name must be an XML name, got {service_name!r}")
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<wsdl:definitions xmlns:s="http://www.w3.org/2001/XMLSchema"'
        f' xmlns:soap="http://schemas.xmlsoap.org/wsdl/soap/"'
        f' xmlns:tns="{QBWC_NS}"'
        f' xmlns:wsdl="http://schemas.xmlsoap.org/wsdl/"'
        f' targetNamespace="{QBWC_NS}">'
        "<wsdl:types>"
        f'<s:schema elementFormDefault="qualified" targetNamespace="{QBWC_NS}">'
        f"{_schema_elements()}"
        '<s:complexType name="ArrayOfString"><s:sequence>'
        '<s:element minOccurs="0" maxOccurs="unbounded" name="string" nillable="true" type="s:string"/>'
        "</s:sequence></s:complexType>"
        "</s:schema>"
        "</wsdl:types>"
        f"{_messages()}"
        f'<wsdl:portType name="{service_name}Soap">{_port_type()}</wsdl:portType>'
        f'<wsdl:binding name="{service_name}Soap" type="tns:{service_name}Soap">'
        '<soap:binding transport="http://schemas.xmlsoap.org/soap/http"/>'
        f"{_binding()}"
        "</wsdl:binding>"
        f'<wsdl:service name="{service_name}">'
        f'<wsdl:port name="{service_name}Soap" binding="tns:{service_name}Soap">'
        f"<soap:address location={quoteattr(endpoint_url)}/>"
        "</wsdl:port></wsdl:service>"
        "</wsdl:definitions>"
    )


def build_qwc(
    *,
    app_name: str,
    app_id: str,
    app_url: str,
    app_description: str,
    username: str,
    owner_id: str,
    file_id: str,
    run_every_n_seconds: int = 900,
    support_url: str | None = None,
) -> str:
    """Render the ``.qwc`` file a user imports into the Web Connector.

    ``OwnerID`` and ``FileID`` are GUIDs that identify the integration to
    QuickBooks; regenerating them forces every user to re-authorise, so they
    belong in configuration, not in code.

    Everything is XML-escaped: an ampersand in an app name or a query string in
    the URL would otherwise produce a file the Web Connector refuses to import,
    with "invalid file" as the entire explanation.
    """
    support = support_url or app_url
    return (
        '<?xml version="1.0"?>'
        "<QBWCXML>"
        f"<AppName>{escape(app_name)}</AppName>"
        f"<AppID>{escape(app_id)}</AppID>"
        f"<AppURL>{escape(app_url)}</AppURL>"
        f"<AppDescription>{escape(app_description)}</AppDescription>"
        f"<AppSupport>{escape(support)}</AppSupport>"
        f"<UserName>{escape(username)}</UserName>"
        f"<OwnerID>{escape(owner_id)}</OwnerID>"
        f"<FileID>{escape(file_id)}</FileID>"
        "<QBType>QBFS</QBType>"
        f"<Scheduler><RunEveryNSeconds>{int(run_every_n_seconds)}</RunEveryNSeconds></Scheduler>"
        "</QBWCXML>"
    )
