"""POST /write/ledgers/{lid}/transactions/batch/update — 批量修改交易分类。

1. happy path:同 kind 多笔全更新 → projection 分类字段更新,sync_change
   数量恰好 = 更新数(不误 emit 全账本)
2. kind_mismatch:混合 expense/income 选中,只更新 kind 匹配的,其余记 failed
3. not_found:伪造/跨 ledger id 记 failed 不 500
4. 上限校验:201 条 → 422
5. idempotency:同 key 重发 → replay 第一份响应
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.database import Base, get_db
from src.main import app
from src.models import ReadTxProjection, SyncChange


def _make_client() -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def _register_and_login(client: TestClient, email: str) -> str:
    client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "password": "Pa$$word1!",
            "device_id": "d-web",
            "client_type": "web",
            "device_name": "pytest-web",
            "platform": "test",
        },
    )
    r = client.post(
        "/api/v1/auth/login",
        json={
            "email": email,
            "password": "Pa$$word1!",
            "device_id": "d-web",
            "client_type": "web",
            "device_name": "pytest-web",
            "platform": "test",
        },
    )
    return r.json()["access_token"]


def _create_ledger(client: TestClient, token: str, name: str = "default") -> str:
    r = client.post(
        "/api/v1/write/ledgers",
        json={"ledger_name": name, "currency": "CNY"},
        headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
    )
    assert r.status_code == 200, r.text
    return r.json()["entity_id"]


def _create_category(client: TestClient, token: str, ledger_id: str, name: str, kind: str) -> str:
    r = client.post(
        f"/api/v1/write/ledgers/{ledger_id}/categories",
        json={"base_change_id": 0, "name": name, "kind": kind},
        headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
    )
    assert r.status_code == 200, r.text
    return r.json()["entity_id"]


def _seed_txs(
    client: TestClient,
    token: str,
    ledger_id: str,
    n: int,
    tx_type: str = "expense",
    category: dict | None = None,
) -> list[str]:
    """通过 batch create 一次造 n 笔交易,返回 sync_ids。"""
    items = []
    for i in range(n):
        item = {
            "tx_type": tx_type,
            "amount": 10.0 + i,
            "happened_at": "2026-05-06T12:30:00Z",
            "note": f"item {i}",
            "tags": [],
        }
        if category:
            item.update(category)
        items.append(item)
    r = client.post(
        f"/api/v1/write/ledgers/{ledger_id}/transactions/batch",
        json={"base_change_id": 0, "transactions": items, "auto_ai_tag": False, "locale": "zh"},
        headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
    )
    assert r.status_code == 200, r.text
    return r.json()["created_sync_ids"]


def _tx_upsert_change_count() -> int:
    db = next(app.dependency_overrides[get_db]())
    try:
        return len(
            db.scalars(
                select(SyncChange).where(
                    SyncChange.entity_type == "transaction",
                    SyncChange.action == "upsert",
                )
            ).all()
        )
    finally:
        db.close()


def _batch_update(
    client: TestClient,
    token: str,
    ledger_id: str,
    tx_ids: list[str],
    category_id: str,
    category_name: str,
    category_kind: str,
    extra_headers: dict | None = None,
):
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"}
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        f"/api/v1/write/ledgers/{ledger_id}/transactions/batch/update",
        json={
            "tx_ids": tx_ids,
            "category_id": category_id,
            "category_name": category_name,
            "category_kind": category_kind,
            "base_change_id": 0,
        },
        headers=headers,
    )


# ──────────────────────────────────────────────────────────────────────


def test_batch_update_happy_path_emits_exact_changes():
    client = _make_client()
    try:
        token = _register_and_login(client, "bu1@test.com")
        ledger_id = _create_ledger(client, token)
        cat_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        tx_ids = _seed_txs(client, token, ledger_id, n=3)
        changes_before = _tx_upsert_change_count()

        r = _batch_update(client, token, ledger_id, tx_ids, cat_id, "餐饮", "expense")
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["updated_tx_ids"]) == sorted(tx_ids)
        assert body["failed"] == []
        assert body["new_change_id"] > 0

        # projection 分类字段已更新
        db = next(app.dependency_overrides[get_db]())
        try:
            rows = db.scalars(
                select(ReadTxProjection).where(ReadTxProjection.sync_id.in_(tx_ids))
            ).all()
            assert len(rows) == 3
            for row in rows:
                assert row.category_name == "餐饮"
                assert row.category_kind == "expense"
                assert row.category_sync_id == cat_id
        finally:
            db.close()

        # sync_change 恰好 +3(不误 emit 未变更的交易)
        assert _tx_upsert_change_count() == changes_before + 3
    finally:
        app.dependency_overrides.clear()


def test_batch_update_kind_mismatch_skipped():
    client = _make_client()
    try:
        token = _register_and_login(client, "bu2@test.com")
        ledger_id = _create_ledger(client, token)
        cat_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        expense_ids = _seed_txs(client, token, ledger_id, n=2, tx_type="expense")
        income_ids = _seed_txs(client, token, ledger_id, n=1, tx_type="income")

        r = _batch_update(
            client, token, ledger_id, expense_ids + income_ids, cat_id, "餐饮", "expense"
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["updated_tx_ids"]) == sorted(expense_ids)
        assert len(body["failed"]) == 1
        assert body["failed"][0]["tx_id"] == income_ids[0]
        assert body["failed"][0]["reason"] == "kind_mismatch"

        # income 那笔分类应该还是空
        db = next(app.dependency_overrides[get_db]())
        try:
            row = db.scalar(
                select(ReadTxProjection).where(ReadTxProjection.sync_id == income_ids[0])
            )
            assert row.category_name is None
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()


def test_batch_update_fake_ids_return_not_found():
    client = _make_client()
    try:
        token = _register_and_login(client, "bu3@test.com")
        ledger_id = _create_ledger(client, token)
        cat_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        real_ids = _seed_txs(client, token, ledger_id, n=2)
        fake_ids = ["tx_nope_1", "tx_nope_2"]

        r = _batch_update(client, token, ledger_id, real_ids + fake_ids, cat_id, "餐饮", "expense")
        assert r.status_code == 200, r.text
        body = r.json()
        assert sorted(body["updated_tx_ids"]) == sorted(real_ids)
        assert len(body["failed"]) == 2
        assert all(f["reason"] == "not_found" for f in body["failed"])
    finally:
        app.dependency_overrides.clear()


def test_batch_update_max_size_limit():
    client = _make_client()
    try:
        token = _register_and_login(client, "bu4@test.com")
        ledger_id = _create_ledger(client, token)
        cat_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        too_many = [f"tx_{i}" for i in range(201)]
        r = _batch_update(client, token, ledger_id, too_many, cat_id, "餐饮", "expense")
        assert r.status_code == 422, r.text
    finally:
        app.dependency_overrides.clear()


def test_batch_update_idempotency_replay():
    client = _make_client()
    try:
        token = _register_and_login(client, "bu5@test.com")
        ledger_id = _create_ledger(client, token)
        cat_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        tx_ids = _seed_txs(client, token, ledger_id, n=2)
        idem_key = "test-idem-update-1"

        headers = {"Idempotency-Key": idem_key}
        r1 = _batch_update(client, token, ledger_id, tx_ids, cat_id, "餐饮", "expense", headers)
        assert r1.status_code == 200, r1.text
        first_change_id = r1.json()["new_change_id"]

        r2 = _batch_update(client, token, ledger_id, tx_ids, cat_id, "餐饮", "expense", headers)
        assert r2.status_code == 200, r2.text
        assert r2.json()["new_change_id"] == first_change_id
        assert sorted(r2.json()["updated_tx_ids"]) == sorted(tx_ids)
    finally:
        app.dependency_overrides.clear()
