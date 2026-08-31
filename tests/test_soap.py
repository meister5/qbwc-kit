import pytest

from qbwc_kit import soap


def envelope(inner: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        f"<soap:Body>{inner}</soap:Body></soap:Envelope>"
    )


def test_parses_method_and_params():
    call = soap.parse_request(
        envelope(
            '<authenticate xmlns="http://developer.intuit.com/">'
            "<strUserName>bob</strUserName><strPassword>hunter2</strPassword>"
            "</authenticate>"
        )
    )
    assert call.method == "authenticate"
    assert call.params == {"strUserName": "bob", "strPassword": "hunter2"}


def test_preserves_parameter_order():
    call = soap.parse_request(
        envelope(
            "<sendRequestXML><ticket>t</ticket><strHCPResponse/>"
            "<strCompanyFileName>C:\\books.QBW</strCompanyFileName></sendRequestXML>"
        )
    )
    assert call.order == ("ticket", "strHCPResponse", "strCompanyFileName")
    assert call.positional(0) == "t"
    assert call.positional(2) == "C:\\books.QBW"


def test_empty_element_becomes_empty_string():
    call = soap.parse_request(envelope("<getLastError><ticket/></getLastError>"))
    assert call.get("ticket") == ""


def test_positional_out_of_range_returns_default():
    call = soap.parse_request(envelope("<getLastError><ticket>t</ticket></getLastError>"))
    assert call.positional(7, "fallback") == "fallback"


@pytest.mark.parametrize(
    "body",
    [
        "not xml at all",
        "<Envelope><Body/></Envelope>",  # no method element
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"/>',  # no Body
        "<html><body>hello</body></html>",  # something else entirely
    ],
)
def test_rejects_non_qbwc_payloads(body):
    with pytest.raises(soap.SoapError):
        soap.parse_request(body)


def test_rejects_multi_method_body():
    with pytest.raises(soap.SoapError):
        soap.parse_request(envelope("<a/><b/>"))


def test_accepts_bytes():
    call = soap.parse_request(envelope("<serverVersion/>").encode("utf-8"))
    assert call.method == "serverVersion"


def test_string_array_response_uses_string_elements():
    xml = soap.build_response("authenticate", ["ticket-1", ""])
    assert "<authenticateResponse" in xml
    assert xml.count("<string>") == 2
    assert "<string>ticket-1</string>" in xml


def test_int_response_is_inlined():
    xml = soap.build_response("receiveResponseXML", 42)
    assert "<receiveResponseXMLResult>42</receiveResponseXMLResult>" in xml


def test_response_escapes_markup():
    xml = soap.build_response("sendRequestXML", "<QBXML>&</QBXML>")
    assert "&lt;QBXML&gt;&amp;&lt;/QBXML&gt;" in xml
    # Round-trips through a parser without corrupting the payload.
    round_tripped = soap.parse_request(
        envelope("<sendRequestXML><req>&lt;QBXML&gt;&amp;&lt;/QBXML&gt;</req></sendRequestXML>")
    )
    assert round_tripped.get("req") == "<QBXML>&</QBXML>"


def test_booleans_are_rejected():
    # QBWC has no boolean type, and bool is an int subclass, so a stray True
    # would otherwise serialize as the integer 1.
    with pytest.raises(TypeError):
        soap.build_response("clientVersion", True)


def test_text_split_around_a_comment_is_not_truncated():
    # Taking only element.text would silently drop everything after the
    # comment, which for receiveResponseXML means half a qbXML document.
    call = soap.parse_request(
        envelope(
            "<receiveResponseXML><response>&lt;QBXML&gt;"
            "<!-- injected -->&lt;/QBXML&gt;</response></receiveResponseXML>"
        )
    )
    assert call.get("response") == "<QBXML></QBXML>"


def test_comments_are_not_counted_as_method_elements():
    call = soap.parse_request(envelope("<!-- hello --><serverVersion/><!-- bye -->"))
    assert call.method == "serverVersion"


def test_repeated_parameter_names_keep_the_last_value_and_stay_positional():
    call = soap.parse_request(
        envelope("<getLastError><ticket>a</ticket><ticket>b</ticket></getLastError>")
    )
    assert call.get("ticket") == "b"
    assert call.positional(1) == "b"


def test_fault_carries_message():
    xml = soap.build_fault("nope")
    assert "<faultstring>nope</faultstring>" in xml
    assert "soap:Server" in xml
