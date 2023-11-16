# Verification notes

Executed locally on Windows with Python 3.12:

| Check | Result |
|---|---|
| Ruff lint and formatting | Passed |
| Strict mypy checks | Passed for the application source |
| Core pytest suite | 46 cases passed |
| Backup/restore regression | Online backup restores invoice, receipt and report evidence; later source writes stay isolated |
| Combined statement/branch coverage | 98.87%, above the 85% gate |
| HTTP walkthrough | Expected partial-payment report produced |
| Runtime dependency audit | No known advisories reported by pip-audit |

The suite covers actual SQLite transactions and concurrent connections, invoice/usage sealing races, payload-aware retries, all-or-nothing statement imports, tenant-scoped object access, revoked keys, invalid money, request bounds and stable historical reports. A property-based test varies event partitions and rates while repeating delivery, checking that the invoice retains the original total. CLI code is excluded from the coverage percentage; the walkthrough exercises the HTTP application directly.

Starlette currently emits a deprecation warning for its httpx-based test client. The suite passes with the pinned dependency set; a future test-client migration should be handled as a deliberate dependency update.

GitHub Actions is configured for Python 3.12/3.13 on Ubuntu and Windows. Its live results, timestamps and tested commit IDs are available in the Actions tab. Local results alone do not establish that those matrix jobs passed.

No external payment provider, deployment load test, independent security assessment or operational disaster-recovery drill was performed. The restore regression is a local database consistency check, not a backup-scheduling or failover exercise. The tests do not establish regulatory compliance or freedom from defects.
