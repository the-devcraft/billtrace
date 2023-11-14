"""Billing transitions. Every mutation and its audit record share one transaction."""

import hashlib
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

from billtrace.db import Database
from billtrace.models import CustomerInput, InvoiceInput, StatementInput, UsageInput


class DomainError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


class Billing:
    def __init__(self, db: Database, today: Callable[[], date] | None = None):
        self.db = db
        self.today = today or (lambda: datetime.now(UTC).date())

    @staticmethod
    def audit(conn: sqlite3.Connection, tenant: str, action: str, object_id: str) -> None:
        conn.execute(
            "INSERT INTO audit_events(tenant_id, action, object_id, recorded_at) VALUES(?,?,?,?)",
            (tenant, action, object_id, now()),
        )

    @staticmethod
    def customer(conn: sqlite3.Connection, tenant: str, customer_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT id, external_id, currency, rate_cents FROM customers "
            "WHERE tenant_id=? AND id=?",
            (tenant, customer_id),
        ).fetchone()
        if not row:
            raise DomainError("customer_not_found", 404)
        return dict(row)

    def add_customer(self, tenant: str, data: CustomerInput) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM customers WHERE tenant_id=? AND external_id=?",
                (tenant, data.external_id),
            ).fetchone()
            if row:
                existing = self.customer(conn, tenant, row["id"])
                if {k: existing[k] for k in CustomerInput.model_fields} != data.model_dump():
                    raise DomainError("customer_reference_conflict")
                return existing
            customer_id = str(uuid4())
            conn.execute(
                "INSERT INTO customers VALUES(?,?,?,?,?)",
                (tenant, customer_id, data.external_id, data.currency, data.rate_cents),
            )
            self.audit(conn, tenant, "customer.created", customer_id)
            return self.customer(conn, tenant, customer_id)

    def add_usage(self, tenant: str, data: UsageInput) -> dict[str, Any]:
        customer_id, period = str(data.customer_id), data.service_date.strftime("%Y-%m")
        payload = data.model_dump(mode="json")
        fingerprint = digest(payload)
        with self.db.transaction() as conn:
            self.customer(conn, tenant, customer_id)
            row = conn.execute(
                "SELECT fingerprint FROM usage_events WHERE tenant_id=? AND event_id=?",
                (tenant, data.event_id),
            ).fetchone()
            if row:
                if row["fingerprint"] != fingerprint:
                    raise DomainError("event_reference_conflict")
                return {"event_id": data.event_id, "replayed": True}
            if data.service_date > self.today():
                raise DomainError("future_usage", 422)
            if conn.execute(
                "SELECT 1 FROM invoices WHERE tenant_id=? AND customer_id=? AND period=?",
                (tenant, customer_id, period),
            ).fetchone():
                raise DomainError("period_already_invoiced")
            # Reject an invoice overflow at ingestion rather than accepting unbillable usage.
            total = conn.execute(
                "SELECT COALESCE(SUM(units),0) FROM usage_events "
                "WHERE tenant_id=? AND customer_id=? AND period=?",
                (tenant, customer_id, period),
            ).fetchone()[0]
            rate = self.customer(conn, tenant, customer_id)["rate_cents"]
            if (total + data.units) * rate > 1_000_000_000_000:
                raise DomainError("invoice_amount_limit", 422)
            conn.execute(
                "INSERT INTO usage_events VALUES(?,?,?,?,?,?,?)",
                (
                    tenant,
                    data.event_id,
                    customer_id,
                    str(data.service_date),
                    period,
                    data.units,
                    fingerprint,
                ),
            )
            self.audit(conn, tenant, "usage.accepted", data.event_id)
            return {"event_id": data.event_id, "replayed": False}

    def issue_invoice(self, tenant: str, data: InvoiceInput) -> dict[str, Any]:
        customer_id = str(data.customer_id)
        with self.db.transaction() as conn:
            customer = self.customer(conn, tenant, customer_id)
            existing = conn.execute(
                "SELECT id FROM invoices WHERE tenant_id=? AND customer_id=? AND period=?",
                (tenant, customer_id, data.period),
            ).fetchone()
            if existing:
                return self.invoice(conn, tenant, existing["id"])
            if data.period >= self.today().strftime("%Y-%m"):
                raise DomainError("period_not_closed", 422)
            units = conn.execute(
                "SELECT COALESCE(SUM(units),0) FROM usage_events "
                "WHERE tenant_id=? AND customer_id=? AND period=?",
                (tenant, customer_id, data.period),
            ).fetchone()[0]
            invoice_id = str(uuid4())
            conn.execute(
                "INSERT INTO invoices VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    invoice_id,
                    customer_id,
                    data.period,
                    customer["currency"],
                    customer["rate_cents"],
                    units,
                    units * customer["rate_cents"],
                    now(),
                ),
            )
            self.audit(conn, tenant, "invoice.issued", invoice_id)
            return self.invoice(conn, tenant, invoice_id)

    @staticmethod
    def invoice(conn: sqlite3.Connection, tenant: str, invoice_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT id, customer_id, period, currency, rate_cents, units, amount_cents, issued_at "
            "FROM invoices WHERE tenant_id=? AND id=?",
            (tenant, invoice_id),
        ).fetchone()
        if not row:
            raise DomainError("invoice_not_found", 404)
        return dict(row)

    def import_statement(self, tenant: str, data: StatementInput) -> dict[str, Any]:
        entries = sorted(data.entries, key=lambda x: x.provider_ref)
        fingerprint = digest([x.model_dump(mode="json") for x in entries])
        with self.db.transaction() as conn:
            prior = conn.execute(
                "SELECT fingerprint, result FROM statements WHERE tenant_id=? AND batch_id=?",
                (tenant, data.batch_id),
            ).fetchone()
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise DomainError("batch_reference_conflict")
                return dict(json.loads(prior["result"]))
            inserted = 0
            for entry in entries:
                entry_digest = digest(entry.model_dump(mode="json"))
                previous = conn.execute(
                    "SELECT fingerprint FROM payments WHERE tenant_id=? AND provider_ref=?",
                    (tenant, entry.provider_ref),
                ).fetchone()
                if previous:
                    if previous["fingerprint"] != entry_digest:
                        raise DomainError("payment_reference_conflict")
                    continue
                conn.execute(
                    "INSERT INTO payments VALUES(?,?,?,?,?,?,?)",
                    (
                        tenant,
                        entry.provider_ref,
                        str(entry.invoice_id),
                        entry.period,
                        entry.currency,
                        entry.amount_cents,
                        entry_digest,
                    ),
                )
                inserted += 1
            result = {
                "batch_id": data.batch_id,
                "inserted": inserted,
                "duplicates": len(entries) - inserted,
            }
            conn.execute(
                "INSERT INTO statements VALUES(?,?,?,?)",
                (tenant, data.batch_id, fingerprint, canonical(result)),
            )
            self.audit(conn, tenant, "statement.imported", data.batch_id)
            return result

    def reconcile(self, tenant: str, period: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            invoices = [
                dict(x)
                for x in conn.execute(
                    "SELECT id, customer_id, currency, amount_cents FROM invoices "
                    "WHERE tenant_id=? AND period=? ORDER BY id",
                    (tenant, period),
                )
            ]
            payments = [
                dict(x)
                for x in conn.execute(
                    "SELECT provider_ref, invoice_id, currency, amount_cents FROM payments "
                    "WHERE tenant_id=? AND period=? ORDER BY provider_ref",
                    (tenant, period),
                )
            ]
            by_id = {x["id"]: x for x in invoices}
            allocated: dict[str, list[dict[str, Any]]] = {x["id"]: [] for x in invoices}
            exceptions = []
            for payment in payments:
                invoice = by_id.get(payment["invoice_id"])
                reason = None
                if invoice is None:
                    known = conn.execute(
                        "SELECT period FROM invoices WHERE tenant_id=? AND id=?",
                        (tenant, payment["invoice_id"]),
                    ).fetchone()
                    reason = "period_mismatch" if known else "unknown_invoice"
                elif invoice["currency"] != payment["currency"]:
                    reason = "currency_mismatch"
                if reason:
                    exceptions.append({**payment, "reason": reason})
                else:
                    allocated[payment["invoice_id"]].append(payment)
            for invoice in invoices:
                assigned = allocated[invoice["id"]]
                paid = sum(x["amount_cents"] for x in assigned)
                expected = invoice["amount_cents"]
                status = (
                    "matched"
                    if paid == expected
                    else ("overpaid" if paid > expected else "unpaid" if paid == 0 else "partial")
                )
                invoice.update(
                    received_cents=paid,
                    delta_cents=paid - expected,
                    status=status,
                    payments=assigned,
                )
            unbilled = [
                dict(x)
                for x in conn.execute(
                    "SELECT u.customer_id, SUM(u.units) AS units FROM usage_events u "
                    "WHERE u.tenant_id=? AND u.period=? AND NOT EXISTS "
                    "(SELECT 1 FROM invoices i WHERE i.tenant_id=u.tenant_id "
                    "AND i.customer_id=u.customer_id AND i.period=u.period) "
                    "GROUP BY u.customer_id ORDER BY u.customer_id",
                    (tenant, period),
                )
            ]
            report = {
                "period": period,
                "invoices": invoices,
                "exceptions": exceptions,
                "unbilled_usage": unbilled,
            }
            report_digest = digest(report)
            previous = conn.execute(
                "SELECT id, created_at FROM reconciliation_runs "
                "WHERE tenant_id=? AND period=? AND digest=?",
                (tenant, period, report_digest),
            ).fetchone()
            if previous:
                return {
                    "id": previous["id"],
                    "digest": report_digest,
                    "created_at": previous["created_at"],
                    **report,
                }
            run_id, created = str(uuid4()), now()
            conn.execute(
                "INSERT INTO reconciliation_runs VALUES(?,?,?,?,?,?)",
                (tenant, run_id, period, report_digest, canonical(report), created),
            )
            self.audit(conn, tenant, "reconciliation.created", run_id)
            return {"id": run_id, "digest": report_digest, "created_at": created, **report}

    def get_run(self, tenant: str, run_id: str) -> dict[str, Any]:
        with self.db.transaction(write=False) as conn:
            row = conn.execute(
                "SELECT * FROM reconciliation_runs WHERE tenant_id=? AND id=?",
                (tenant, run_id),
            ).fetchone()
            if not row:
                raise DomainError("reconciliation_not_found", 404)
            return {
                "id": row["id"],
                "digest": row["digest"],
                "created_at": row["created_at"],
                **json.loads(row["report"]),
            }
