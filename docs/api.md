# API semantics

Use `Content-Type: application/json` and `Authorization: Bearer <tenant-key>`. The OpenAPI document at `/openapi.json` is generated from the request models. `/docs` provides an interactive client.

Create a customer:

```json
{"external_id":"acme","currency":"USD","rate_cents":7}
```

Submit usage with the returned customer UUID:

```json
{"event_id":"usage-001","customer_id":"<customer-uuid>","service_date":"2023-11-10","units":100}
```

Issue the closed billing month:

```json
{"customer_id":"<customer-uuid>","period":"2023-11"}
```

Import provider evidence using the returned invoice UUID:

```json
{
  "batch_id":"statement-001",
  "entries":[
    {"provider_ref":"receipt-001","invoice_id":"<invoice-uuid>","period":"2023-11","currency":"USD","amount_cents":650}
  ]
}
```

Reconcile with `{"period":"2023-11"}`. `delta_cents` is received minus expected. A zero-value invoice with no receipts is matched. Positive invoice states are unpaid, partial, matched, or overpaid. Exceptions are `unknown_invoice`, `period_mismatch`, and `currency_mismatch`. The example period is sample business data, not a development timeline.

## Response and retry rules

Successful POSTs return 200, including safe retries. API-key failures return 401. Tenant-scoped missing objects return 404. Payload validation returns 422. Business conflicts return 409 with a stable `error` code. Retrying a 409 with the same conflicting payload will not succeed; resolve the reference conflict first.

Storage operational errors return 503 with `Retry-After: 1`. Retry the identical payload with bounded exponential backoff and jitter. Never generate a new provider reference just to retry a failed delivery.

Requests are capped at 1 MiB and 500 entries per statement. Pagination returns at most 100 invoices; audit pagination accepts the previous response's `next_after`. An empty audit page returns its supplied cursor unchanged. Rate limiting, request timeouts and aggregate storage quotas belong at the deployment boundary in this release.

Customer rates, usage, invoices, payments and reports cannot be edited or deleted through the API. A mistaken source payment cannot yet be corrected: do not use real accounting data until a correction workflow is implemented. Revoking a key blocks subsequent authentication; it does not cancel requests already authenticated.
