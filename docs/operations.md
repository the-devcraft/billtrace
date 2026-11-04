# Operations

The default CLI binds to 127.0.0.1. Keep it local for evaluation. For managed deployment, use a TLS reverse proxy and run `uvicorn billtrace.api:create_app --factory --host 0.0.0.0 --port 8000` as an unprivileged service. Set `BILLTRACE_DB` to an absolute path on a local persistent disk. Do not put WAL databases on network filesystems or start multiple container replicas against a shared file.

Restrict filesystem access: possession of the database bypasses the API's tenant boundary. Keep keys in a secret manager, not shell history or screenshots. Provisioning prints a key once by explicit operator request. Key revocation is tenant-wide; granular permissions and overlapping key rotation are not implemented.

Apply per-key rate limits, an ingress body limit and timeouts at the proxy. Monitor 503 counts, disk space, write latency and backup age. App responses do not expose database errors or API keys. Review proxy/access-log configuration to keep authorization headers out of logs.

## Backup and restore

Use SQLite's online backup API from a process with access to the file:

```python
import sqlite3
with sqlite3.connect("data/billtrace.sqlite3") as source:
    with sqlite3.connect("backup.sqlite3") as target:
        source.backup(target)
```

Do not copy only the main file while WAL writes are active. Store encrypted backups separately. Test a restore into an isolated directory, run `PRAGMA integrity_check`, and compare stored reconciliation digests and invoice counts. Back up before any future schema migration. Automated backup scheduling and disaster-recovery drills are outside this repository's executed checks.

`python -m pytest tests/test_restore.py --no-cov` exercises the online backup and checks restored invoice amounts, receipts, report digests and isolation from subsequent source writes.

## Recovery expectations

A transaction rolls back when its connection fails before commit. A client can lose the response after a successful commit; replay the same reference and payload to recover the existing result. Do not rewrite immutable records to force reconciliation to match. Investigate mismatch evidence and correct the upstream source through an agreed correction process.
