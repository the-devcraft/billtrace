# Architecture decisions

## A narrow financial boundary

BillTrace rates whole usage units, seals monthly invoices, ingests payment evidence and reports differences. Keeping collections, tax and refunds outside this boundary makes it possible to state exactly which amounts are being compared.

Each customer has one immutable currency and rate. The invoice amount is `sum(units) * rate_cents`; no floats, currency conversion or per-line rounding are involved. USD, EUR and GBP are the only supported currencies. An invoice is capped at 10^12 minor units, enforced during ingestion and by a database constraint at invoice insertion.

## Transaction model

Every mutation opens its own connection and `BEGIN IMMEDIATE` transaction. Validation, reads used for the decision, the business insert and the audit insert all occur under that transaction. SQLite permits one writer: two invoice issuers see either the same existing invoice or one insert, never two invoices. A usage event racing with sealing is either included or rejected; it cannot be accepted but omitted.

The HTTP layer is synchronous for database work so FastAPI executes those handlers in its worker thread pool. A busy database produces a retryable 503. WAL permits readers during a writer transaction. Foreign keys are enabled on every connection and refer to composite tenant/object keys. Identifiers from callers are always bound parameters.

This trades write throughput for a straightforward correctness model. It is not a horizontal scaling architecture. Move to a server database with equivalent locking and isolation tests before adding application replicas.

## Replay semantics

References are unique within a tenant. SHA-256 fingerprints cover normalized Pydantic payloads. A duplicate event is checked before the sealed-period rule, so a successful event's retry remains safe after billing. Statement entries are sorted by provider reference before hashing; an order-only change does not create a conflict. Repeated receipts in a different batch are counted as duplicates, not inserted twice. Changed receipt content aborts the whole batch.

Invoice retries use `(tenant, customer, period)` uniqueness and return the original invoice. Customer retries compare the rate and currency as well as the external reference.

## Reconciliation snapshots

For a requested billing month, load that tenant's invoices and statement entries in deterministic order. Allocate a receipt only when its invoice ID, period and currency agree. A foreign-tenant invoice ID behaves exactly like an unknown ID. Compare each invoice's expected and received minor units, without combining currencies. Usage for customers not yet invoiced is reported separately.

Reports include individual allocated receipt references and amounts, as well as exceptions. Hash the canonical report to identify an unchanged input result. Persist its content and audit event in the same write transaction. New payments leave existing snapshots intact.

Statements are trusted operator evidence, not proof that a payment provider actually settled funds. Their `period` field identifies the billing period being reconciled; settlement timing and bank fees are not represented.

## Schema evolution

The initial schema uses `PRAGMA user_version=1`. Startup is repeatable for this schema and refuses unknown newer versions. No upgrade migration is needed in the initial release. Future changes require an explicit versioned migration and restore test; editing CREATE TABLE statements is not a migration strategy.
