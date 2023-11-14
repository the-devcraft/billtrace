import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from billtrace.db import Database
from billtrace.models import (
    CustomerInput,
    InvoiceInput,
    Period,
    ReconcileInput,
    StatementInput,
    UsageInput,
)
from billtrace.service import Billing, DomainError


class BodyLimit:
    """Bound request buffering even when Content-Length is absent or untrusted."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ]
            await send(message)

        if scope["method"] in ("GET", "HEAD"):
            await self.app(scope, receive, secured_send)
            return
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            size += len(body)
            if size > 1_048_576:
                await JSONResponse({"error": "body_too_large"}, 413)(scope, receive, secured_send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break
        delivered = False

        async def buffered() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, buffered, secured_send)


def create_app(db: Database | None = None) -> FastAPI:
    database = db or Database(os.getenv("BILLTRACE_DB", "data/billtrace.sqlite3"))
    billing = Billing(database)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database.initialize()
        yield

    app = FastAPI(
        title="BillTrace",
        version="0.1.0",
        lifespan=lifespan,
        description="Tenant-scoped usage invoicing and reproducible payment reconciliation.",
    )
    app.add_middleware(BodyLimit)
    bearer = HTTPBearer(auto_error=False)

    def tenant(
        credential: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        found = database.authenticate(credential.credentials) if credential else None
        if not found:
            raise HTTPException(401, "invalid_api_key", headers={"WWW-Authenticate": "Bearer"})
        return found

    # Keep errors stable and avoid echoing secrets or database implementation details.
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"error": exc.code}, exc.status)

    @app.exception_handler(sqlite3.OperationalError)
    async def unavailable(request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        return JSONResponse({"error": "storage_unavailable"}, 503, headers={"Retry-After": "1"})

    @app.get("/health", tags=["Operations"])
    def health() -> dict[str, str]:
        with database.transaction(write=False) as conn:
            conn.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/v1/customers", tags=["Billing"])
    def add_customer(data: CustomerInput, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        return billing.add_customer(tenant_id, data)

    @app.post("/v1/usage", tags=["Billing"])
    def add_usage(data: UsageInput, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        return billing.add_usage(tenant_id, data)

    @app.post("/v1/invoices", tags=["Billing"])
    def issue(data: InvoiceInput, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        return billing.issue_invoice(tenant_id, data)

    @app.get("/v1/invoices/{invoice_id}", tags=["Billing"])
    def invoice(invoice_id: UUID, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        with database.transaction(write=False) as conn:
            return billing.invoice(conn, tenant_id, str(invoice_id))

    @app.get("/v1/invoices", tags=["Billing"])
    def invoices(
        period: Annotated[Period, Query()],
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
        tenant_id: str = Depends(tenant),
    ) -> dict[str, Any]:
        with database.transaction(write=False) as conn:
            ids = conn.execute(
                "SELECT id FROM invoices WHERE tenant_id=? AND period=? "
                "ORDER BY id LIMIT ? OFFSET ?",
                (tenant_id, period, limit, offset),
            ).fetchall()
            return {
                "items": [billing.invoice(conn, tenant_id, x["id"]) for x in ids],
                "limit": limit,
                "offset": offset,
            }

    @app.post("/v1/statements", tags=["Reconciliation"])
    def statement(data: StatementInput, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        return billing.import_statement(tenant_id, data)

    @app.post("/v1/reconciliations", tags=["Reconciliation"])
    def reconcile(data: ReconcileInput, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        return billing.reconcile(tenant_id, data.period)

    @app.get("/v1/reconciliations/{run_id}", tags=["Reconciliation"])
    def run(run_id: UUID, tenant_id: str = Depends(tenant)) -> dict[str, Any]:
        return billing.get_run(tenant_id, str(run_id))

    @app.get("/v1/audit", tags=["Operations"])
    def audit(
        after: Annotated[int, Query(ge=0)] = 0,
        tenant_id: str = Depends(tenant),
    ) -> dict[str, Any]:
        with database.transaction(write=False) as conn:
            rows = conn.execute(
                "SELECT seq, action, object_id, recorded_at FROM audit_events "
                "WHERE tenant_id=? AND seq>? ORDER BY seq LIMIT 100",
                (tenant_id, after),
            ).fetchall()
            return {
                "items": [dict(x) for x in rows],
                "next_after": rows[-1]["seq"] if rows else after,
            }

    return app
