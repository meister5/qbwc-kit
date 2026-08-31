"""The SOAP endpoint is reachable from the network, so the parsers have to be.

``xml.etree`` expands internal entities, which turns a few hundred bytes of
request body into gigabytes of memory. Both parsers refuse documents that
declare a DTD, which is the only place an entity can be declared.
"""

import pytest

from qbwc_kit import soap
from qbwc_kit._xml import fromstring
from qbwc_kit.qbxml import QBXMLParseError, parse_response

BILLION_LAUGHS = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
]>
<QBXML><QBXMLMsgsRs>&lol2;</QBXMLMsgsRs></QBXML>"""

XXE = (
    '<?xml version="1.0"?>'
    '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
    "<QBXML><QBXMLMsgsRs>&x;</QBXMLMsgsRs></QBXML>"
)


def test_entity_expansion_is_refused_by_the_qbxml_parser():
    with pytest.raises(QBXMLParseError, match="document type"):
        parse_response(BILLION_LAUGHS)


def test_external_entity_is_refused_by_the_qbxml_parser():
    with pytest.raises(QBXMLParseError, match="document type"):
        parse_response(XXE)


def test_entity_expansion_is_refused_by_the_soap_parser():
    envelope = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE e [<!ENTITY a "aaaaaaaaaa">]>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><getLastError><ticket>&a;</ticket></getLastError></soap:Body>"
        "</soap:Envelope>"
    )
    with pytest.raises(soap.SoapError, match="document type"):
        soap.parse_request(envelope)


def test_a_dtd_hidden_behind_a_comment_is_still_caught():
    payload = (
        "<!-- <QBXML> looks like a root element but is not -->"
        '<!DOCTYPE q [<!ENTITY a "x">]>'
        "<QBXML><QBXMLMsgsRs/></QBXML>"
    )
    with pytest.raises(QBXMLParseError, match="document type"):
        parse_response(payload)


def test_a_service_fault_is_returned_rather_than_a_crash():
    from qbwc_kit.service import QBWCService
    from qbwc_kit.session import StaticAuthenticator

    service = QBWCService(authenticator=StaticAuthenticator("u", "p", []))
    envelope = service.dispatch(BILLION_LAUGHS)
    assert "soap:Client" in envelope


def test_ordinary_documents_still_parse():
    assert fromstring("<a><b>x</b></a>")[0].text == "x"
    assert fromstring(b'<?xml version="1.0" encoding="utf-8"?><a/>').tag == "a"
    # A DOCTYPE-looking string inside element text is data, not a declaration.
    assert fromstring("<a>&lt;!DOCTYPE fake&gt;</a>").text == "<!DOCTYPE fake>"
