from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from billtrace.db import Database
from billtrace.models import CustomerInput, InvoiceInput, UsageInput
from billtrace.service import Billing, DomainError


def usage(customer, event_id="event-1", **extra):
    return {
        "event_id": event_id,
        "customer_id": customer["id"],
        "service_date": "2023-11-10",
        "units": 11,
        **extra,
    }


def invoice(client, tenant, customer, period="2023-11"):
    return client.post(
        "/v1/invoices",
        headers=tenant[1],
        json={
            "customer_id": customer["id"],
            "period": period,
        },
    )


def test_invoice_arithmetic_replay_and_late_usage(client, tenant, customer):
    first = client.post("/v1/usage", headers=tenant[1], json=usage(customer))
    assert first.json()["replayed"] is False
    original = invoice(client, tenant, customer)
    assert original.status_code == 200
    assert original.json()["amount_cents"] == 77
    assert invoice(client, tenant, customer).json() == original.json()
    # Exact transport retries remain valid even after the billing period is sealed.
    assert client.post("/v1/usage", headers=tenant[1], json=usage(customer)).json()["replayed"]
    assert (
        client.post("/v1/usage", headers=tenant[1], json=usage(customer, "late")).status_code == 409
    )
    assert (
        client.get("/v1/invoices/" + original.json()["id"], headers=tenant[1]).json()
        == original.json()
    )


def test_event_conflict_is_atomic(client, db, tenant, customer):
    client.post("/v1/usage", headers=tenant[1], json=usage(customer))
    conflict = client.post("/v1/usage", headers=tenant[1], json=usage(customer, units=12))
    assert conflict.status_code == 409
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE action='usage.accepted'"
            ).fetchone()[0]
            == 1
        )


def test_customer_reference_is_payload_aware(client, tenant, customer):
    data = {"external_id": "acme", "currency": "USD", "rate_cents": 7}
    assert client.post("/v1/customers", headers=tenant[1], json=data).json() == customer
    assert (
        client.post("/v1/customers", headers=tenant[1], json={**data, "rate_cents": 8}).status_code
        == 409
    )


@pytest.mark.parametrize("units", [0, -1, 1.5, True, "12", 1_000_001])
def test_strict_units(client, tenant, customer, units):
    assert (
        client.post("/v1/usage", headers=tenant[1], json=usage(customer, units=units)).status_code
        == 422
    )


@pytest.mark.parametrize("period", ["2023-00", "2023-13", "0000-01", "23-01", "2023-01' OR 1=1--"])
def test_invalid_period(client, tenant, customer, period):
    assert invoice(client, tenant, customer, period).status_code == 422


def test_future_usage_and_open_period(client, tenant, customer):
    assert (
        client.post(
            "/v1/usage", headers=tenant[1], json=usage(customer, service_date="9999-01-01")
        ).status_code
        == 422
    )
    assert invoice(client, tenant, customer, "9999-01").status_code == 422


def test_zero_invoice_and_missing_customer(client, tenant, customer):
    assert invoice(client, tenant, customer).json()["amount_cents"] == 0
    assert invoice(client, tenant, {"id": str(uuid4())}).status_code == 404


def test_utc_month_boundary_and_limits(db):
    tenant, _ = db.provision("Limits")
    billing = Billing(db, today=lambda: date(2024, 1, 1))
    c = billing.add_customer(
        tenant, CustomerInput(external_id="limit", currency="USD", rate_cents=1_000_000)
    )
    billing.add_usage(tenant, UsageInput(**usage(c, service_date="2023-12-31", units=1_000_000)))
    with pytest.raises(DomainError, match="invoice_amount_limit"):
        billing.add_usage(
            tenant, UsageInput(**usage(c, "overflow", service_date="2023-12-31", units=1))
        )
    issued = billing.issue_invoice(tenant, InvoiceInput(customer_id=c["id"], period="2023-12"))
    assert issued["amount_cents"] == 1_000_000_000_000
    with pytest.raises(DomainError, match="period_not_closed"):
        billing.issue_invoice(tenant, InvoiceInput(customer_id=c["id"], period="2024-01"))


def test_concurrent_retries_issue_only_one_invoice(db):
    tenant, _ = db.provision("Concurrent")
    billing = Billing(db)
    c = billing.add_customer(
        tenant, CustomerInput(external_id="concurrent", currency="USD", rate_cents=5)
    )
    event = UsageInput(**usage(c))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: billing.add_usage(tenant, event), range(12)))
    assert sum(not x["replayed"] for x in results) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        issued = list(
            pool.map(
                lambda _: billing.issue_invoice(
                    tenant, InvoiceInput(customer_id=c["id"], period="2023-11")
                ),
                range(12),
            )
        )
    assert len({x["id"] for x in issued}) == 1
    assert issued[0]["amount_cents"] == 55


def test_invoice_races_with_usage_without_lost_billing(db):
    tenant, _ = db.provision("Race")
    billing = Billing(db)
    c = billing.add_customer(
        tenant, CustomerInput(external_id="race", currency="USD", rate_cents=3)
    )

    def ingest():
        try:
            billing.add_usage(tenant, UsageInput(**usage(c)))
            return "accepted"
        except DomainError as exc:
            assert exc.code == "period_already_invoiced"
            return "sealed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        event = pool.submit(ingest)
        inv = pool.submit(
            billing.issue_invoice, tenant, InvoiceInput(customer_id=c["id"], period="2023-11")
        )
        accepted, issued = event.result(), inv.result()
    assert issued["amount_cents"] == (33 if accepted == "accepted" else 0)


@settings(max_examples=15, deadline=None)
@given(a=st.integers(1, 100_000), b=st.integers(1, 100_000), rate=st.integers(1, 1000))
def test_split_usage_and_retries_preserve_invoice_total(a, b, rate):
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "property.sqlite3")
        database.initialize()
        tenant_id, _ = database.provision("Property")
        billing = Billing(database)
        customer = billing.add_customer(
            tenant_id, CustomerInput(external_id="property", currency="USD", rate_cents=rate)
        )
        for event_id, amount in (("a", a), ("b", b), ("a", a)):
            billing.add_usage(tenant_id, UsageInput(**usage(customer, event_id, units=amount)))
        result = billing.issue_invoice(
            tenant_id, InvoiceInput(customer_id=customer["id"], period="2023-11")
        )
        assert result["units"] == a + b
        assert result["amount_cents"] == (a + b) * rate
