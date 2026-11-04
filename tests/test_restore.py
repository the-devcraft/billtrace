import sqlite3
from contextlib import closing

from billtrace.db import Database
from billtrace.service import Billing

from .test_billing import invoice, usage
from .test_reconciliation import payment, report, statement


def test_online_backup_restores_invoice_and_snapshot_evidence(
    client, tenant, customer, db, tmp_path
):
    client.post("/v1/usage", headers=tenant[1], json=usage(customer))
    issued = invoice(client, tenant, customer).json()
    statement(client, tenant, [payment(issued["id"])])
    before = report(client, tenant).json()

    backup_path = tmp_path / "restore.sqlite3"
    with closing(db.connect()) as source, closing(sqlite3.connect(backup_path)) as target:
        source.backup(target)

    restored = Database(backup_path)
    restored.initialize()
    billing = Billing(restored)
    with restored.transaction(write=False) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert billing.invoice(conn, tenant[0], issued["id"]) == issued
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
    assert billing.get_run(tenant[0], before["id"]) == before
    assert billing.reconcile(tenant[0], "2023-11") == before

    # A restore is isolated: later writes to the source do not alter restored evidence.
    statement(client, tenant, [payment(issued["id"], "later", amount=1)], "later-batch")
    assert report(client, tenant).json()["digest"] != before["digest"]
    assert billing.reconcile(tenant[0], "2023-11") == before
