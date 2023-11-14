import sqlite3
from uuid import uuid4

import pytest

from billtrace.db import Database

from .test_billing import invoice, usage
from .test_reconciliation import payment, report, statement


def test_missing_invalid_and_revoked_keys(client, tenant, db):
    assert client.get("/v1/audit").status_code == 401
    assert client.get("/v1/audit", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert db.revoke(tenant[0])
    assert client.get("/v1/audit", headers=tenant[1]).status_code == 401
    assert not db.revoke(str(uuid4()))


def test_all_boundaries_are_tenant_scoped(client, tenant, customer, db):
    _, other_key = db.provision("Other")
    other = {"Authorization": f"Bearer {other_key}"}
    client.post("/v1/usage", headers=tenant[1], json=usage(customer))
    issued = invoice(client, tenant, customer).json()
    snapshot = report(client, tenant).json()
    assert client.get("/v1/invoices/" + issued["id"], headers=other).status_code == 404
    assert client.post("/v1/usage", headers=other, json=usage(customer)).status_code == 404
    assert (
        client.post(
            "/v1/invoices",
            headers=other,
            json={
                "customer_id": customer["id"],
                "period": "2023-11",
            },
        ).status_code
        == 404
    )
    assert client.get("/v1/invoices?period=2023-11", headers=other).json()["items"] == []
    assert client.get("/v1/audit", headers=other).json()["items"] == []
    assert client.get("/v1/reconciliations/" + snapshot["id"], headers=other).status_code == 404
    statement(client, (None, other), [payment(issued["id"])])
    foreign = report(client, (None, other)).json()
    assert foreign["exceptions"][0]["reason"] == "unknown_invoice"
    assert report(client, tenant).json()["invoices"][0]["received_cents"] == 0


def test_schema_and_sql_injection_boundaries(client, tenant, customer):
    data = {"external_id": "x'); DROP TABLE invoices;--", "currency": "USD", "rate_cents": 2}
    assert client.post("/v1/customers", headers=tenant[1], json=data).status_code == 422
    assert (
        client.post(
            "/v1/usage", headers=tenant[1], json={**usage(customer), "tenant_id": "other"}
        ).status_code
        == 422
    )
    assert client.get("/v1/invoices?period=2023-11&limit=101", headers=tenant[1]).status_code == 422
    assert client.get("/v1/invoices?period=2023-11&offset=-1", headers=tenant[1]).status_code == 422


def test_request_size_is_bounded(client, tenant):
    response = client.post("/v1/statements", headers=tenant[1], content=b"x" * 1_048_577)
    assert response.status_code == 413


def test_audit_pagination_and_response_headers(client, tenant, customer):
    result = client.get("/v1/audit", headers=tenant[1])
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.json()["items"][0]["action"] == "customer.created"
    assert (
        client.get(
            "/v1/audit", headers=tenant[1], params={"after": result.json()["next_after"]}
        ).json()["items"]
        == []
    )
    assert client.get("/health").json() == {"status": "ok"}
    schema = client.get("/openapi.json").json()
    assert schema["paths"]["/v1/statements"]["post"]["security"] == [{"HTTPBearer": []}]


def test_database_rollback_and_immutable_records(db, tenant):
    with pytest.raises(RuntimeError), db.transaction() as conn:
        conn.execute(
            "INSERT INTO audit_events(tenant_id,action,object_id,recorded_at) VALUES(?,?,?,?)",
            (tenant[0], "test", "x", "test"),
        )
        raise RuntimeError("interrupted")
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == 0
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO audit_events(tenant_id,action,object_id,recorded_at) VALUES(?,?,?,?)",
            (tenant[0], "test", "x", "test"),
        )
    for sql in ("UPDATE audit_events SET action='tamper'", "DELETE FROM audit_events"):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"), db.transaction() as conn:
            conn.execute(sql)


def test_audit_failure_rolls_back_business_change(client, tenant, db):
    with db.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        client.post(
            "/v1/customers",
            headers=tenant[1],
            json={
                "external_id": "rollback",
                "currency": "USD",
                "rate_cents": 1,
            },
        )
    with db.transaction(write=False) as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_database_version_and_missing_tenant(db):
    with pytest.raises(ValueError, match="file database"):
        Database(":memory:")
    with pytest.raises(ValueError):
        db.provision("")
    with db.transaction() as conn:
        conn.execute("PRAGMA user_version = 99")
    with pytest.raises(ValueError, match="Unsupported schema"):
        db.initialize()


def test_database_busy_is_retryable(client, tenant, monkeypatch, db):
    def busy(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "connect", busy)
    response = client.get("/health")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert "locked" not in response.text
