import pytest

from qbwc_kit.qbxml import QBXMLParseError, QBXMLStatusError, parse_response

OK_RESPONSE = """<?xml version="1.0" ?><QBXML><QBXMLMsgsRs>
<CustomerQueryRs requestID="1" statusCode="0" statusSeverity="Info" statusMessage="Status OK">
  <CustomerRet><ListID>80000001-1</ListID><Name>Acme</Name>
    <BillAddress><Addr1>1 Main St</Addr1><City>Wayne</City></BillAddress></CustomerRet>
  <CustomerRet><ListID>80000002-1</ListID><Name>Globex</Name></CustomerRet>
</CustomerQueryRs></QBXMLMsgsRs></QBXML>"""


def test_parses_records_and_status():
    response = parse_response(OK_RESPONSE).first()
    assert response.status_code == 0
    assert response.ok
    assert len(response) == 2
    assert response.records[0]["Name"] == "Acme"


def test_nested_aggregates_become_nested_dicts():
    record = parse_response(OK_RESPONSE).first().records[0]
    assert record["BillAddress"] == {"Addr1": "1 Main St", "City": "Wayne"}


def test_repeated_tags_collapse_into_a_list():
    payload = """<QBXML><QBXMLMsgsRs><InvoiceQueryRs statusCode="0">
    <InvoiceRet><TxnID>1</TxnID>
      <InvoiceLineRet><Amount>10.00</Amount></InvoiceLineRet>
      <InvoiceLineRet><Amount>20.00</Amount></InvoiceLineRet>
    </InvoiceRet></InvoiceQueryRs></QBXMLMsgsRs></QBXML>"""
    lines = parse_response(payload).first().records[0]["InvoiceLineRet"]
    assert [line["Amount"] for line in lines] == ["10.00", "20.00"]


def test_status_1_is_empty_but_still_ok():
    # "Nothing found" is a valid answer, not a failure. Conflating the two is
    # how an incremental sync quietly starts reporting zero rows.
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="1" statusSeverity="Info" '
        'statusMessage="No matching records"/></QBXMLMsgsRs></QBXML>'
    )
    response = parse_response(payload).first()
    assert response.ok and response.empty and len(response) == 0
    response.raise_for_status()


def test_unsupported_request_is_not_ok_despite_a_successful_envelope():
    payload = (
        '<QBXML><QBXMLMsgsRs><SalesOrderQueryRs statusCode="3100" statusSeverity="Error" '
        'statusMessage="Feature not available"/></QBXMLMsgsRs></QBXML>'
    )
    response = parse_response(payload).first()
    assert not response.ok
    with pytest.raises(QBXMLStatusError, match="3100"):
        response.raise_for_status()


def test_iterator_metadata():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="0" iteratorRemainingCount="7" '
        'iteratorID="i-1"><CustomerRet><ListID>1</ListID></CustomerRet>'
        "</CustomerQueryRs></QBXMLMsgsRs></QBXML>"
    )
    response = parse_response(payload).first()
    assert response.has_more
    assert response.iterator_remaining_count == 7
    assert response.iterator_id == "i-1"


def test_no_more_pages_when_remaining_is_zero():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="0" iteratorRemainingCount="0" '
        'iteratorID="i-1"/></QBXMLMsgsRs></QBXML>'
    )
    assert not parse_response(payload).first().has_more


def test_entity_name_is_derived_from_the_response_tag():
    assert parse_response(OK_RESPONSE).first().entity == "Customer"


def test_lookup_by_request_id():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs requestID="a" statusCode="0"/>'
        '<VendorQueryRs requestID="b" statusCode="0"/></QBXMLMsgsRs></QBXML>'
    )
    result = parse_response(payload)
    assert result.by_request_id("b").name == "VendorQueryRs"
    assert result.by_request_id("zz") is None


def test_batch_level_failure_reporting():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="0"/>'
        '<VendorQueryRs statusCode="3100" statusMessage="nope"/></QBXMLMsgsRs></QBXML>'
    )
    result = parse_response(payload)
    assert not result.ok
    assert [r.name for r in result.failures] == ["VendorQueryRs"]
    with pytest.raises(QBXMLStatusError):
        result.raise_for_status()


def test_empty_payload_is_an_empty_result_not_an_error():
    # QBWC passes "" through receiveResponseXML when a request was skipped.
    assert len(parse_response("")) == 0
    assert len(parse_response("   ")) == 0


@pytest.mark.parametrize(
    "payload",
    [
        "<<<",
        "<NotQBXML/>",
        "<QBXML><Something/></QBXML>",
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="abc"/></QBXMLMsgsRs></QBXML>',
    ],
)
def test_malformed_documents_raise(payload):
    with pytest.raises(QBXMLParseError):
        parse_response(payload)


def test_error_recovery_element_is_not_a_record():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="0">'
        "<ErrorRecovery><OwnerID>0</OwnerID></ErrorRecovery>"
        "<CustomerRet><ListID>1</ListID></CustomerRet>"
        "</CustomerQueryRs></QBXMLMsgsRs></QBXML>"
    )
    response = parse_response(payload).first()
    assert len(response) == 1
    assert response.records[0]["ListID"] == "1"


def test_iteration_protocol():
    response = parse_response(OK_RESPONSE).first()
    assert [record["Name"] for record in response] == ["Acme", "Globex"]


def test_bytes_payloads_are_accepted():
    assert len(parse_response(OK_RESPONSE.encode("utf-8")).first()) == 2


def test_comments_are_not_mistaken_for_records():
    # A comment node's tag is a callable, so treating it as an element turns
    # the record key into a function object.
    payload = (
        "<QBXML><QBXMLMsgsRs><!-- generated by QuickBooks -->"
        '<CustomerQueryRs statusCode="0"><!-- one row -->'
        "<CustomerRet><ListID>1</ListID><!-- trailing --></CustomerRet>"
        "</CustomerQueryRs></QBXMLMsgsRs></QBXML>"
    )
    result = parse_response(payload)
    assert len(result) == 1
    assert result.first().records == [{"ListID": "1"}]


def test_non_numeric_iterator_count_is_a_parse_error_not_a_value_error():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="0" '
        'iteratorRemainingCount="lots"/></QBXMLMsgsRs></QBXML>'
    )
    with pytest.raises(QBXMLParseError, match="iteratorRemainingCount"):
        parse_response(payload)


def test_first_explains_what_it_could_not_find():
    result = parse_response(OK_RESPONSE)
    with pytest.raises(KeyError, match="CustomerQueryRs"):
        result.first("Vendor")
    with pytest.raises(KeyError, match="empty"):
        parse_response("").first()


def test_first_matches_on_either_the_tag_or_the_entity():
    result = parse_response(OK_RESPONSE)
    assert result.first("CustomerQueryRs") is result.first("Customer") is result.first()


def test_indexing_and_length_of_a_batch():
    payload = (
        '<QBXML><QBXMLMsgsRs><CustomerQueryRs statusCode="0"/>'
        '<VendorQueryRs statusCode="0"/></QBXMLMsgsRs></QBXML>'
    )
    result = parse_response(payload)
    assert len(result) == 2
    assert result[1].name == "VendorQueryRs"
    assert [r.name for r in result] == ["CustomerQueryRs", "VendorQueryRs"]


def test_a_leaf_child_is_still_a_record():
    # Some responses return a scalar rather than an aggregate.
    payload = (
        '<QBXML><QBXMLMsgsRs><DataExtDelRs statusCode="0">'
        "<OwnerID>0</OwnerID></DataExtDelRs></QBXMLMsgsRs></QBXML>"
    )
    assert parse_response(payload).first().records == [{"OwnerID": "0"}]
