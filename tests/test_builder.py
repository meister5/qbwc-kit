from xml.etree import ElementTree as ET

import pytest

from qbwc_kit import qbxml
from qbwc_kit.qbxml import OnError, QBXMLRequest, Request


def test_document_has_qbxml_processing_instruction():
    xml = QBXMLRequest([qbxml.query("Customer")], version="13.0").render()
    assert xml.startswith('<?xml version="1.0" encoding="utf-8"?><?qbxml version="13.0"?>')


def test_on_error_attribute():
    xml = QBXMLRequest([qbxml.query("Customer")], on_error=OnError.CONTINUE).render()
    assert 'onError="continueOnError"' in xml


def test_empty_batch_is_rejected():
    with pytest.raises(ValueError):
        QBXMLRequest([]).render()


def test_max_returned_precedes_other_filters():
    xml = qbxml.query("Invoice", max_returned=50, active_status="ActiveOnly").render()
    assert xml.index("MaxReturned") < xml.index("ActiveStatus")


def test_transaction_queries_wrap_the_date_filter():
    xml = qbxml.query("Invoice", modified_after="2026-01-01T00:00:00").render()
    assert "<ModifiedDateRangeFilter><FromModifiedDate>2026-01-01T00:00:00" in xml


def test_list_queries_take_a_bare_date_filter():
    # CustomerQueryRq has FromModifiedDate as a direct child; wrapping it in a
    # ModifiedDateRangeFilter the way a transaction query does is a schema
    # violation that QuickBooks rejects with an opaque parse error.
    xml = qbxml.query("Customer", modified_after="2026-01-01T00:00:00").render()
    assert "ModifiedDateRangeFilter" not in xml
    assert "<FromModifiedDate>2026-01-01T00:00:00</FromModifiedDate>" in xml


def test_list_query_element_order_follows_the_schema_sequence():
    xml = qbxml.query(
        "Vendor",
        max_returned=10,
        active_status="ActiveOnly",
        modified_after="2026-01-01",
        modified_before="2026-02-01",
    ).render()
    order = ["MaxReturned", "ActiveStatus", "FromModifiedDate", "ToModifiedDate"]
    positions = [xml.index(name) for name in order]
    assert positions == sorted(positions)


def test_owner_id_trails_include_ret_element():
    xml = qbxml.query("Customer", include_fields=["ListID"], owner_id="0").render()
    assert xml.index("IncludeRetElement") < xml.index("OwnerID")


@pytest.mark.parametrize(
    "entity,transaction,expected",
    [
        ("Invoice", None, True),
        ("Customer", None, False),
        # An entity this library has never heard of, forced either way.
        ("Widget", True, True),
        ("Widget", False, False),
    ],
)
def test_the_date_filter_family_can_be_forced(entity, transaction, expected):
    xml = qbxml.query(entity, modified_after="2026-01-01", transaction=transaction).render()
    assert ("ModifiedDateRangeFilter" in xml) is expected


def test_iterator_attributes():
    xml = qbxml.query("Customer", iterator="Continue", iterator_id="abc").render()
    assert 'iterator="Continue"' in xml
    assert 'iteratorID="abc"' in xml


def test_iterator_on_unsupported_entity_fails_at_build_time():
    # QuickBooks answers this with an opaque parse error, so it is caught here.
    with pytest.raises(ValueError, match="iterator"):
        qbxml.query("Company", iterator="Start").render()


def test_a_bare_iterator_id_is_checked_too():
    # Resuming a page sends only iteratorID, so the guard cannot key on
    # `iterator` alone or the second request slips through.
    with pytest.raises(ValueError, match="iterator"):
        qbxml.query("Preferences", iterator_id="i-1").render()


@pytest.mark.parametrize("entity", ["Employee", "Class", "ItemService", "PaymentMethod"])
def test_entities_that_do_support_iterators_are_not_blocked(entity):
    # Refusing to build a request QuickBooks would have accepted leaves the
    # caller with no way forward short of bypassing the builder.
    assert 'iterator="Start"' in qbxml.query(entity, iterator="Start").render()


def test_on_error_accepts_the_plain_string():
    assert 'onError="continueOnError"' in (
        QBXMLRequest([qbxml.query("Customer")], on_error="continueOnError").render()
    )


def test_include_ret_elements_trim_the_response():
    xml = qbxml.query("Customer", include_fields=["ListID", "Name"]).render()
    assert xml.count("<IncludeRetElement>") == 2


def test_add_wraps_the_aggregate():
    xml = qbxml.add("Customer", {"Name": "Acme", "IsActive": True}).render()
    assert "<CustomerAddRq><CustomerAdd><Name>Acme</Name><IsActive>true</IsActive>" in xml


def test_nested_mappings_become_nested_aggregates():
    xml = qbxml.add("Customer", {"BillAddress": {"Addr1": "1 Main St", "City": "Wayne"}}).render()
    assert "<BillAddress><Addr1>1 Main St</Addr1><City>Wayne</City></BillAddress>" in xml


def test_a_list_repeats_the_element_rather_than_nesting_it():
    # Line items are siblings in qbXML; wrapping them in one element is a
    # schema violation QuickBooks rejects with an opaque parse error.
    xml = qbxml.add(
        "Invoice", {"InvoiceLineAdd": [{"Amount": "10.00"}, {"Amount": "5.00"}]}
    ).render()
    assert xml.count("<InvoiceLineAdd>") == 2
    assert "<InvoiceLineAdd><Amount>10.00</Amount></InvoiceLineAdd>" in xml


def test_a_string_that_looks_like_markup_stays_text():
    # Otherwise any field fed from user data could rewrite the request.
    xml = qbxml.add("Customer", {"Name": "</Name><ListID>evil</ListID><Name>"}).render()
    assert "<ListID>" not in xml
    root = ET.fromstring(xml)
    assert root.find("CustomerAdd/Name").text == "</Name><ListID>evil</ListID><Name>"


def test_none_valued_fields_are_omitted():
    xml = qbxml.add("Customer", {"Name": "Acme", "Phone": None}).render()
    assert "Phone" not in xml


def test_field_order_is_preserved():
    fields = {"Name": "A", "CompanyName": "B", "Phone": "C"}
    xml = qbxml.add("Customer", fields).render()
    assert xml.index("Name") < xml.index("CompanyName") < xml.index("Phone")


def test_mod_requires_an_identifier():
    with pytest.raises(ValueError):
        qbxml.mod("Customer", {"Name": "x"}, edit_sequence="1")


def test_mod_carries_edit_sequence_for_optimistic_concurrency():
    xml = qbxml.mod("Customer", {"Name": "x"}, list_id="80000001-1", edit_sequence="17").render()
    assert "<ListID>80000001-1</ListID><EditSequence>17</EditSequence>" in xml


def test_escaping_of_ampersands_and_angle_brackets():
    xml = qbxml.add("Customer", {"Name": "Smith & Sons <NJ>"}).render()
    assert "Smith &amp; Sons &lt;NJ&gt;" in xml
    # And the result is still well-formed.
    ET.fromstring(xml.split("?>")[-1])


def test_ref_accepts_either_key():
    assert (
        qbxml.ref("CustomerRef", full_name="Acme")
        == "<CustomerRef><FullName>Acme</FullName></CustomerRef>"
    )
    assert qbxml.ref("CustomerRef", list_id="1") == "<CustomerRef><ListID>1</ListID></CustomerRef>"
    assert qbxml.ref("CustomerRef") == ""


def test_request_id_round_trips():
    xml = QBXMLRequest([Request("CustomerQueryRq", request_id="q1")]).render()
    assert 'requestID="q1"' in xml


def test_rendered_batch_is_well_formed_xml():
    batch = QBXMLRequest(
        [
            qbxml.query("Customer", max_returned=10, request_id="1"),
            qbxml.add("Vendor", {"Name": "Supplier"}, request_id="2"),
        ]
    )
    root = ET.fromstring(batch.render().split("?>")[-1])
    assert root.tag == "QBXML"
    assert len(root.find("QBXMLMsgsRq")) == 2
