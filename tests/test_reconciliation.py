from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from billtrace.models import StatementInput
from billtrace.service import Billing

from .test_billing import invoice, usage


def payment(invoice_id, ref="payment-1", amount=77, **extra):
    return {
        "provider_ref": ref,
        "invoice_id": invoice_id,
        "period": "2023-11",
        "currency": "USD",
        "amount_cents": amount,
        **extra,
    }


def statement(client, tenant, entries, batch="statement-1"):
    return client.post(
        "/v1/statements",
        headers=tenant[1],
        json={
            "batch_id": batch,
            "entries": entries,
        },
    )


def report(client, tenant, period="2023-11"):
    return client.post("/v1/reconciliations", headers=tenant[1], json={"period": period})


@pytest.fixture
def issued(client, tenant, customer):
    client.post("/v1/usage", headers=tenant[1], json=usage(customer))
    return invoice(client, tenant, customer).json()


@pytest.mark.parametrize(
    ("amount", "status", "delta"),
    [
        (77, "matched", 0),
        (60, "partial", -17),
        (80, "overpaid", 3),
    ],
)
def test_payment_matching(client, tenant, issued, amount, status, delta):
    assert statement(client, tenant, [payment(issued["id"], amount=amount)]).status_code == 200
    result = report(client, tenant).json()["invoices"][0]
    assert (result["status"], result["delta_cents"]) == (status, delta)


def test_unpaid_and_zero_invoice(client, tenant, issued, customer):
    assert report(client, tenant).json()["invoices"][0]["status"] == "unpaid"
    invoice(client, tenant, customer, "2023-10")
    assert report(client, tenant, "2023-10").json()["invoices"][0]["status"] == "matched"


def test_duplicate_imports_and_order_independence(client, tenant, issued):
    entries = [payment(issued["id"], "a", 50), payment(issued["id"], "b", 27)]
    first = statement(client, tenant, entries).json()
    assert first["inserted"] == 2
    assert statement(client, tenant, list(reversed(entries))).json() == first
    second = statement(client, tenant, entries, "other-batch").json()
    assert second == {"batch_id": "other-batch", "inserted": 0, "duplicates": 2}
    assert report(client, tenant).json()["invoices"][0]["received_cents"] == 77


def test_conflicting_import_rolls_back_all_rows_and_audit(client, tenant, issued, db):
    statement(client, tenant, [payment(issued["id"], "z-existing")])
    entries = [payment(issued["id"], "a-new", 1), payment(issued["id"], "z-existing", 90)]
    assert statement(client, tenant, entries, "conflict").status_code == 409
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action='statement.imported'"
            ).fetchone()[0]
            == 1
        )
    assert statement(client, tenant, [payment(issued["id"], amount=78)]).status_code == 409


@pytest.mark.parametrize("amount", [True, 0, -3, "77", 7.7, 1_000_000_000_001])
def test_payment_rejects_invalid_money(client, tenant, issued, amount):
    assert statement(client, tenant, [payment(issued["id"], amount=amount)]).status_code == 422


def test_duplicate_references_and_batch_bounds(client, tenant, issued):
    entry = payment(issued["id"])
    assert statement(client, tenant, [entry, entry]).status_code == 422
    assert statement(client, tenant, []).status_code == 422
    assert (
        statement(client, tenant, [payment(issued["id"], str(i)) for i in range(501)]).status_code
        == 422
    )


def test_mismatches_never_count_as_revenue(client, tenant, issued):
    entries = [
        payment(str(uuid4()), "unknown"),
        payment(issued["id"], "wrong-currency", currency="EUR"),
        payment(issued["id"], "wrong-period", period="2023-10"),
    ]
    assert statement(client, tenant, entries).status_code == 200
    nov = report(client, tenant).json()
    assert nov["invoices"][0]["received_cents"] == 0
    assert {x["reason"] for x in nov["exceptions"]} == {"unknown_invoice", "currency_mismatch"}
    assert report(client, tenant, "2023-10").json()["exceptions"][0]["reason"] == "period_mismatch"


def test_snapshot_is_stable_and_old_report_does_not_change(client, tenant, issued):
    before = report(client, tenant).json()
    assert report(client, tenant).json() == before
    statement(client, tenant, [payment(issued["id"])])
    after = report(client, tenant).json()
    assert before["id"] != after["id"]
    assert before["digest"] != after["digest"]
    assert client.get("/v1/reconciliations/" + before["id"], headers=tenant[1]).json() == before
    assert client.get("/v1/reconciliations/" + after["id"], headers=tenant[1]).json() == after


def test_usage_without_invoice_is_explicit(client, tenant, customer):
    client.post("/v1/usage", headers=tenant[1], json=usage(customer))
    assert report(client, tenant).json()["unbilled_usage"] == [
        {"customer_id": customer["id"], "units": 11},
    ]


def test_concurrent_statement_delivery_is_deduplicated(db, tenant):
    billing = Billing(db)
    entry = payment(str(uuid4()))
    data = StatementInput(batch_id="delivery", entries=[entry])
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: billing.import_statement(tenant[0], data), range(12)))
    assert all(x == results[0] for x in results)
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
