"""Shared qbXML vocabulary."""

from __future__ import annotations

from enum import Enum


class OnError(str, Enum):
    """``QBXMLMsgsRq/@onError``.

    ``STOP`` aborts the batch at the first failing request. ``CONTINUE`` runs
    every request and reports per-request status, which is what you want for a
    read-only sync where one unsupported entity should not blank the rest.
    """

    STOP = "stopOnError"
    CONTINUE = "continueOnError"


class Severity(str, Enum):
    INFO = "Info"
    WARN = "Warn"
    ERROR = "Error"


#: Status codes worth branching on. QuickBooks defines several hundred; these
#: are the ones that change control flow rather than just being logged.
STATUS_OK = 0
STATUS_NOTHING_FOUND = 1
STATUS_UNSUPPORTED_REQUEST = 3100
STATUS_INSUFFICIENT_PERMISSION = 3260
STATUS_STALE_EDIT_SEQUENCE = 3200
STATUS_OBJECT_NOT_FOUND = 500

#: Query requests known to accept ``iterator``/``iteratorID``. Not exhaustive:
#: qbXML grew iterator support request by request, and an entity missing from
#: this set is *not* assumed to be unsupported (see :func:`iterator_supported`).
ITERATOR_ENTITIES = frozenset(
    {
        "AccountQueryRq",
        "ARRefundCreditCardQueryRq",
        "BillPaymentCheckQueryRq",
        "BillPaymentCreditCardQueryRq",
        "BillQueryRq",
        "BuildAssemblyQueryRq",
        "ChargeQueryRq",
        "CheckQueryRq",
        "ClassQueryRq",
        "CreditCardChargeQueryRq",
        "CreditCardCreditQueryRq",
        "CreditMemoQueryRq",
        "CurrencyQueryRq",
        "CustomerMsgQueryRq",
        "CustomerQueryRq",
        "CustomerTypeQueryRq",
        "DateDrivenTermsQueryRq",
        "DepositQueryRq",
        "EmployeeQueryRq",
        "EstimateQueryRq",
        "InventoryAdjustmentQueryRq",
        "InvoiceQueryRq",
        "ItemDiscountQueryRq",
        "ItemGroupQueryRq",
        "ItemInventoryAssemblyQueryRq",
        "ItemInventoryQueryRq",
        "ItemNonInventoryQueryRq",
        "ItemOtherChargeQueryRq",
        "ItemPaymentQueryRq",
        "ItemQueryRq",
        "ItemReceiptQueryRq",
        "ItemSalesTaxGroupQueryRq",
        "ItemSalesTaxQueryRq",
        "ItemServiceQueryRq",
        "ItemSubtotalQueryRq",
        "JobTypeQueryRq",
        "JournalEntryQueryRq",
        "OtherNameQueryRq",
        "PaymentMethodQueryRq",
        "PriceLevelQueryRq",
        "PurchaseOrderQueryRq",
        "ReceivePaymentQueryRq",
        "SalesOrderQueryRq",
        "SalesReceiptQueryRq",
        "SalesRepQueryRq",
        "SalesTaxCodeQueryRq",
        "SalesTaxPaymentCheckQueryRq",
        "ShipMethodQueryRq",
        "StandardTermsQueryRq",
        "TimeTrackingQueryRq",
        "ToDoQueryRq",
        "TransactionQueryRq",
        "TransferQueryRq",
        "VehicleMileageQueryRq",
        "VehicleQueryRq",
        "VendorCreditQueryRq",
        "VendorQueryRq",
        "VendorTypeQueryRq",
    }
)

#: Requests that definitely reject an iterator. Asking for one produces an
#: opaque parse error from QuickBooks, so it is worth catching at build time.
#: These are the singleton/config queries that return at most a handful of
#: rows, plus the report requests, which page a different way entirely.
NON_ITERATOR_ENTITIES = frozenset(
    {
        "BillToPayQueryRq",
        "CompanyActivityQueryRq",
        "CompanyQueryRq",
        "DataExtDefQueryRq",
        "HostQueryRq",
        "PreferencesQueryRq",
        "SalesTaxPayableQueryRq",
        "TemplateQueryRq",
        "TermsQueryRq",
    }
)

#: Transaction queries. They differ from list queries in one way that matters
#: to :func:`qbwc_kit.qbxml.query`: the modified-date filter is spelled
#: ``<ModifiedDateRangeFilter>`` here and as bare ``<FromModifiedDate>`` /
#: ``<ToModifiedDate>`` children on a list query. Using the wrong one is a
#: schema violation that QuickBooks rejects with an unhelpful parse error.
TRANSACTION_ENTITIES = frozenset(
    {
        "ARRefundCreditCard",
        "Bill",
        "BillPaymentCheck",
        "BillPaymentCreditCard",
        "BuildAssembly",
        "Charge",
        "Check",
        "CreditCardCharge",
        "CreditCardCredit",
        "CreditMemo",
        "Deposit",
        "Estimate",
        "InventoryAdjustment",
        "InventoryTransfer",
        "Invoice",
        "ItemReceipt",
        "JournalEntry",
        "LiabilityAdjustment",
        "PurchaseOrder",
        "ReceivePayment",
        "SalesOrder",
        "SalesReceipt",
        "SalesTaxPaymentCheck",
        "TimeTracking",
        "Transaction",
        "Transfer",
        "VehicleMileage",
        "VendorCredit",
    }
)


def iterator_supported(request_name: str) -> bool:
    """Whether ``request_name`` may carry ``iterator``/``iteratorID``.

    Unknown requests are allowed through. Refusing to build a request that
    QuickBooks would have accepted leaves the caller with no way forward short
    of bypassing the builder, which is a worse failure than the parse error
    they would have got from QuickBooks anyway.
    """
    return request_name not in NON_ITERATOR_ENTITIES


def is_transaction(entity: str) -> bool:
    """Whether ``entity`` (``"Invoice"``, ``"Customer"``, ...) is a transaction."""
    return entity in TRANSACTION_ENTITIES
