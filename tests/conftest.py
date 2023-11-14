from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from billtrace.api import create_app
from billtrace.db import Database


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def client(db) -> Iterator[TestClient]:
    with TestClient(create_app(db)) as test_client:
        yield test_client


@pytest.fixture
def tenant(db):
    tenant_id, key = db.provision("Example")
    return tenant_id, {"Authorization": f"Bearer {key}"}


@pytest.fixture
def customer(client, tenant):
    response = client.post(
        "/v1/customers",
        headers=tenant[1],
        json={
            "external_id": "acme",
            "currency": "USD",
            "rate_cents": 7,
        },
    )
    assert response.status_code == 200
    return response.json()
