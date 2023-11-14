# Security

Use GitHub private vulnerability reporting when available; do not include credentials or private financial records in public issues. Otherwise contact the repository owner privately before disclosing sensitive details.

Tenant API keys are high-entropy bearer credentials stored as SHA-256 digests. The API does not provide signup, password authentication, privileged cross-tenant routes or browser cookie sessions. Any holder of a tenant key can read and mutate that tenant's data. A host administrator can read the database and bypass its triggers; immutability is an application safeguard, not a tamper-proof ledger.

Use TLS, filesystem access controls, proxy limits and tested backups before deployment. Dependency auditing is a point-in-time advisory check, not a security certification. No independent security audit or load test has been performed.
