"""Exercise the real HTTP boundary against an isolated, disposable database."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from billtrace.api import create_app
from billtrace.db import Database


def main() -> None:
    with TemporaryDirectory() as directory:
        database = Database(Path(directory) / "demo.sqlite3")
        database.initialize()
        _, key = database.provision("Demo workspace")
        with TestClient(create_app(database)) as client:
            headers = {"Authorization": f"Bearer {key}"}

            def post(path: str, payload: dict) -> dict:
                response = client.post("/v1/" + path, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()

            customer = post(
                "customers",
                {
                    "external_id": "sample-customer",
                    "currency": "USD",
                    "rate_cents": 7,
                },
            )
            event = {
                "event_id": "usage-001",
                "customer_id": customer["id"],
                "service_date": "2023-11-10",
                "units": 100,
            }
            post("usage", event)
            post("usage", event)  # A transport retry must not double the invoice.
            invoice = post("invoices", {"customer_id": customer["id"], "period": "2023-11"})
            post(
                "statements",
                {
                    "batch_id": "statement-001",
                    "entries": [
                        {
                            "provider_ref": "receipt-001",
                            "invoice_id": invoice["id"],
                            "period": "2023-11",
                            "currency": "USD",
                            "amount_cents": 650,
                        },
                    ],
                },
            )
            snapshot = post("reconciliations", {"period": "2023-11"})
            line = snapshot["invoices"][0]
            print(
                json.dumps(
                    {
                        k: line[k]
                        for k in (
                            "currency",
                            "amount_cents",
                            "received_cents",
                            "delta_cents",
                            "status",
                        )
                    },
                    indent=2,
                )
            )


if __name__ == "__main__":
    main()
