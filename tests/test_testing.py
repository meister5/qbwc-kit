"""The test doubles are load-bearing, so they get tested too.

A fake that answers differently from QuickBooks is worse than no fake: it makes
a broken integration look green.
"""

import pytest

from qbwc_kit import qbxml
from qbwc_kit.qbxml import QBXMLRequest, parse_response
from qbwc_kit.service import QBWCService
from qbwc_kit.session import StaticAuthenticator
from qbwc_kit.testing import FakeQuickBooks, FakeWebConnector, service_transport


@pytest.fixture
def quickbooks():
    return FakeQuickBooks()


def ask(quickbooks, request):
    return parse_response(quickbooks(QBXMLRequest([request]).render())).first()


def test_add_keeps_nested_aggregates(quickbooks):
    response = ask(
        quickbooks,
        qbxml.add(
            "Customer",
            {"Name": "Acme", "BillAddress": {"Addr1": "1 Main St", "City": "Wayne"}},
        ),
    )
    assert response.ok
    assert response.records[0]["BillAddress"] == {"Addr1": "1 Main St", "City": "Wayne"}


def test_repeated_aggregates_survive_the_round_trip(quickbooks):
    response = ask(
        quickbooks,
        qbxml.add(
            "Invoice",
            {
                "CustomerRef": {"FullName": "Acme"},
                "InvoiceLineAdd": [{"Amount": "10.00"}, {"Amount": "5.00"}],
            },
        ),
    )
    assert response.ok
    assert response.records[0]["InvoiceLineAdd"] == [{"Amount": "10.00"}, {"Amount": "5.00"}]


def test_mod_bumps_the_edit_sequence(quickbooks):
    added = ask(quickbooks, qbxml.add("Customer", {"Name": "Acme"})).records[0]

    modified = ask(
        quickbooks,
        qbxml.mod(
            "Customer",
            {"Name": "Acme Hardware"},
            list_id=added["ListID"],
            edit_sequence=added["EditSequence"],
        ),
    )
    assert modified.ok
    assert modified.records[0]["Name"] == "Acme Hardware"
    assert modified.records[0]["EditSequence"] != added["EditSequence"]


def test_a_stale_edit_sequence_is_refused(quickbooks):
    added = ask(quickbooks, qbxml.add("Customer", {"Name": "Acme"})).records[0]
    fields = {"list_id": added["ListID"], "edit_sequence": added["EditSequence"]}
    ask(quickbooks, qbxml.mod("Customer", {"Name": "First"}, **fields))

    # Second writer still holds the EditSequence from before the first write.
    stale = ask(quickbooks, qbxml.mod("Customer", {"Name": "Second"}, **fields))
    assert not stale.ok
    assert stale.status_code == 3200
    assert quickbooks.entities["Customer"][0]["Name"] == "First"


def test_modifying_something_that_is_not_there(quickbooks):
    response = ask(
        quickbooks, qbxml.mod("Customer", {"Name": "x"}, list_id="nope", edit_sequence="1")
    )
    assert not response.ok
    assert response.status_code == 500


def test_a_request_without_the_processing_instructions_is_still_understood(quickbooks):
    raw = '<QBXML><QBXMLMsgsRq onError="stopOnError"><CustomerQueryRq/></QBXMLMsgsRq></QBXML>'
    assert parse_response(quickbooks(raw)).first().status_code == 1


def test_an_unsupported_entity_answers_3100_on_a_well_formed_envelope():
    quickbooks = FakeQuickBooks(entities={"Customer": [{"ListID": "1"}]}, supported={"Vendor"})
    response = ask(quickbooks, qbxml.query("Customer"))
    assert response.status_code == 3100
    assert response.status_severity == "Error"


def test_request_ids_survive_the_round_trip(quickbooks):
    quickbooks.entities["Customer"] = [{"ListID": "1", "Name": "Acme"}]
    batch = QBXMLRequest(
        [
            qbxml.query("Customer", request_id="a"),
            qbxml.query("Vendor", request_id="b"),
        ]
    )
    result = parse_response(quickbooks(batch.render()))
    assert result.by_request_id("a").entity == "Customer"
    assert result.by_request_id("b").entity == "Vendor"


def test_max_returned_without_an_iterator_still_truncates(quickbooks):
    quickbooks.entities["Customer"] = [{"ListID": str(i)} for i in range(10)]
    assert len(ask(quickbooks, qbxml.query("Customer", max_returned=3))) == 3


def test_records_with_markup_in_them_round_trip(quickbooks):
    response = ask(quickbooks, qbxml.add("Customer", {"Name": "Smith & Sons <NJ>"}))
    assert response.records[0]["Name"] == "Smith & Sons <NJ>"


def test_the_harness_refuses_a_response_that_is_not_a_soap_envelope():
    # A transport that answers with an error page instead of SOAP should say
    # so, rather than failing somewhere deep in the element walk.
    connector = FakeWebConnector(transport=lambda body: "<nonsense/>", username="u", password="p")
    with pytest.raises(AssertionError, match="SOAP envelope"):
        connector.run_update(FakeQuickBooks())


def test_a_fault_is_reported_rather_than_silently_swallowed():
    from qbwc_kit import soap

    connector = FakeWebConnector(
        transport=lambda body: soap.build_fault("something went wrong"),
        username="u",
        password="p",
    )
    with pytest.raises(AssertionError, match="something went wrong"):
        connector.server_version()


def test_service_transport_dispatches_in_process():
    service = QBWCService(authenticator=StaticAuthenticator("u", "p", []))
    transport = service_transport(service)
    assert "serverVersionResult" in transport(
        '<?xml version="1.0"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body><serverVersion/></soap:Body></soap:Envelope>"
    )
