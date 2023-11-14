CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);
CREATE TABLE IF NOT EXISTS customers (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency IN ('USD', 'EUR', 'GBP')),
    rate_cents INTEGER NOT NULL CHECK (rate_cents BETWEEN 1 AND 1000000),
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, external_id)
);
CREATE TABLE IF NOT EXISTS usage_events (
    tenant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    service_date TEXT NOT NULL,
    period TEXT NOT NULL,
    units INTEGER NOT NULL CHECK (units BETWEEN 1 AND 1000000),
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (tenant_id, event_id),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id)
);
CREATE INDEX IF NOT EXISTS usage_by_period ON usage_events(tenant_id, customer_id, period);
CREATE TABLE IF NOT EXISTS invoices (
    tenant_id TEXT NOT NULL,
    id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    period TEXT NOT NULL,
    currency TEXT NOT NULL,
    rate_cents INTEGER NOT NULL,
    units INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents BETWEEN 0 AND 1000000000000),
    issued_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, customer_id, period),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, id)
);
CREATE INDEX IF NOT EXISTS invoice_period ON invoices(tenant_id, period);
CREATE TABLE IF NOT EXISTS statements (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    batch_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (tenant_id, batch_id)
);
CREATE TABLE IF NOT EXISTS payments (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    provider_ref TEXT NOT NULL,
    invoice_id TEXT NOT NULL,
    period TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents BETWEEN 1 AND 1000000000000),
    fingerprint TEXT NOT NULL,
    PRIMARY KEY (tenant_id, provider_ref)
);
CREATE INDEX IF NOT EXISTS payment_period ON payments(tenant_id, period);
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    id TEXT NOT NULL,
    period TEXT NOT NULL,
    digest TEXT NOT NULL,
    report TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, period, digest)
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    action TEXT NOT NULL,
    object_id TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
PRAGMA user_version = 1;
