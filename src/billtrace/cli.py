"""Local administration: keys are returned only by explicit provisioning."""

import argparse
import json
import os

import uvicorn

from billtrace.db import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="BillTrace administration")
    parser.add_argument("--db", default=os.getenv("BILLTRACE_DB", "data/billtrace.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    tenant = sub.add_parser("tenant")
    tenant.add_argument("name")
    revoke = sub.add_parser("revoke")
    revoke.add_argument("tenant_id")
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    db = Database(args.db)
    db.initialize()
    if args.command == "tenant":
        tenant_id, key = db.provision(args.name)
        print(json.dumps({"tenant_id": tenant_id, "api_key": key}))
    elif args.command == "revoke":
        if not db.revoke(args.tenant_id):
            parser.error("Tenant does not exist")
        print("Key revoked")
    elif args.command == "serve":
        from billtrace.api import create_app

        uvicorn.run(create_app(db), host="127.0.0.1", port=args.port)
    else:
        print("Database initialized")


if __name__ == "__main__":
    main()
