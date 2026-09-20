"""POST /write/ledgers/{lid}/categories/migrate — 分类迁移。

把本账本里引用源分类(categoryId 或 名字+kind 匹配,兼容 legacy 交易)
的交易全部改挂到目标分类。

1. happy path:categoryId 匹配 + legacy 名字匹配的交易都迁移,其它分类的
   交易不动;sync_change 恰好 = 迁移数
2. 校验:跨 kind → 400;源=目标 → 400;源有子分类 → 400;源/目标不存在 → 404
3. 无关联交易:moved_count=0 且 advance change_id,不产生 sync_change
4. idempotency:同 key 重发 → replay
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


def _create_category(
    client: TestClient,
    token: str,
    ledger_id: str,
    name: str,
    kind: str,
    parent_name: str | None = None,
) -> str:
    payload: dict = {"base_change_id": 0, "name": name, "kind": kind}
    if parent_name:
        payload["parent_name"] = parent_name
    r = client.post(
        f"/api/v1/write/ledgers/{ledger_id}/categories",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
    )
    assert r.status_code == 200, r.text
    return r.json()["entity_id"]


def _seed_txs(
    client: TestClient,
    token: str,
    ledger_id: str,
    n: int,
    category: dict | None = None,
) -> list[str]:
    items = []
    for i in range(n):
        item = {
            "tx_type": "expense",
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


def _patch_tx_category_legacy(
    client: TestClient,
    token: str,
    ledger_id: str,
    tx_id: str,
    category_name: str,
    category_kind: str,
) -> None:
    """模拟旧 app 数据:交易只有 categoryName/Kind,没有 categoryId。"""
    r = client.patch(
        f"/api/v1/write/ledgers/{ledger_id}/transactions/{tx_id}",
        json={
            "base_change_id": 0,
            "category_name": category_name,
            "category_kind": category_kind,
        },
        headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
    )
    assert r.status_code == 200, r.text


def _migrate(
    client: TestClient,
    token: str,
    ledger_id: str,
    source_id: str,
    target_id: str,
    extra_headers: dict | None = None,
):
    headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"}
    if extra_headers:
        headers.update(extra_headers)
    return client.post(
        f"/api/v1/write/ledgers/{ledger_id}/categories/migrate",
        json={
            "source_category_id": source_id,
            "target_category_id": target_id,
            "base_change_id": 0,
        },
        headers=headers,
    )


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


def _projection(ledger_id: str, tx_id: str) -> ReadTxProjection:
    db = next(app.dependency_overrides[get_db]())
    try:
        return db.scalar(select(ReadTxProjection).where(ReadTxProjection.sync_id == tx_id))
    finally:
        db.close()


# ──────────────────────────────────────────────────────────────────────


def test_migrate_moves_id_and_legacy_matches_only():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm1@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        dst_id = _create_category(client, token, ledger_id, "伙食", "expense")
        other_id = _create_category(client, token, ledger_id, "交通", "expense")

        with_id = _seed_txs(client, token, ledger_id, n=1, category={
            "category_id": src_id, "category_name": "餐饮", "category_kind": "expense",
        })[0]
        legacy = _seed_txs(client, token, ledger_id, n=1)[0]
        _patch_tx_category_legacy(client, token, ledger_id, legacy, "餐饮", "expense")
        control = _seed_txs(client, token, ledger_id, n=1, category={
            "category_id": other_id, "category_name": "交通", "category_kind": "expense",
        })[0]
        changes_before = _tx_upsert_change_count()

        r = _migrate(client, token, ledger_id, src_id, dst_id)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["moved_count"] == 2
        assert sorted(body["moved_tx_ids"]) == sorted([with_id, legacy])
        assert body["failed"] == []

        # 迁移两笔分类已变;control 不动
        for tx_id in (with_id, legacy):
            row = _projection(ledger_id, tx_id)
            assert row.category_name == "伙食"
            assert row.category_kind == "expense"
            assert row.category_sync_id == dst_id
        control_row = _projection(ledger_id, control)
        assert control_row.category_name == "交通"
        assert control_row.category_sync_id == other_id

        # sync_change 恰好 +2(不误 emit)
        assert _tx_upsert_change_count() == changes_before + 2
    finally:
        app.dependency_overrides.clear()


def test_migrate_kind_mismatch_rejected():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm2@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "工资", "income")
        dst_id = _create_category(client, token, ledger_id, "餐饮", "expense")

        r = _migrate(client, token, ledger_id, src_id, dst_id)
        assert r.status_code == 400, r.text
    finally:
        app.dependency_overrides.clear()


def test_migrate_same_source_target_rejected():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm3@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")

        r = _migrate(client, token, ledger_id, src_id, src_id)
        assert r.status_code == 400, r.text
    finally:
        app.dependency_overrides.clear()


def test_migrate_with_children_rejected():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm4@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        _create_category(client, token, ledger_id, "外食", "expense", parent_name="餐饮")
        dst_id = _create_category(client, token, ledger_id, "伙食", "expense")

        r = _migrate(client, token, ledger_id, src_id, dst_id)
        assert r.status_code == 400, r.text
        assert "child" in r.json()["detail"]
    finally:
        app.dependency_overrides.clear()


def test_migrate_unknown_category_returns_404():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm5@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")

        r = _migrate(client, token, ledger_id, src_id, "cat_does_not_exist")
        assert r.status_code == 404, r.text
        r = _migrate(client, token, ledger_id, "cat_does_not_exist", src_id)
        assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.clear()


def test_migrate_no_matching_txs_is_noop():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm6@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        dst_id = _create_category(client, token, ledger_id, "伙食", "expense")
        # 账本里有一笔交易,但不挂源分类
        _seed_txs(client, token, ledger_id, n=1)
        changes_before = _tx_upsert_change_count()

        r = _migrate(client, token, ledger_id, src_id, dst_id)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["moved_count"] == 0
        assert body["moved_tx_ids"] == []
        # 没有实际变更 → 不产生 sync_change
        assert _tx_upsert_change_count() == changes_before
    finally:
        app.dependency_overrides.clear()


def test_migrate_idempotency_replay():
    client = _make_client()
    try:
        token = _register_and_login(client, "cm7@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        dst_id = _create_category(client, token, ledger_id, "伙食", "expense")
        _seed_txs(client, token, ledger_id, n=1, category={
            "category_id": src_id, "category_name": "餐饮", "category_kind": "expense",
        })
        idem_key = "test-idem-migrate-1"

        headers = {"Idempotency-Key": idem_key}
        r1 = _migrate(client, token, ledger_id, src_id, dst_id, headers)
        assert r1.status_code == 200, r1.text
        first_change_id = r1.json()["new_change_id"]
        assert r1.json()["moved_count"] == 1

        r2 = _migrate(client, token, ledger_id, src_id, dst_id, headers)
        assert r2.status_code == 200, r2.text
        assert r2.json()["new_change_id"] == first_change_id
        assert r2.json()["moved_count"] == 1
    finally:
        app.dependency_overrides.clear()


def test_migrate_after_move_source_category_deletable():
    """迁完源分类下没有交易 → 走既有 DELETE 端点天然通过(对齐 app 口径)。"""
    client = _make_client()
    try:
        token = _register_and_login(client, "cm8@test.com")
        ledger_id = _create_ledger(client, token)
        src_id = _create_category(client, token, ledger_id, "餐饮", "expense")
        dst_id = _create_category(client, token, ledger_id, "伙食", "expense")
        _seed_txs(client, token, ledger_id, n=2, category={
            "category_id": src_id, "category_name": "餐饮", "category_kind": "expense",
        })

        r = _migrate(client, token, ledger_id, src_id, dst_id)
        assert r.status_code == 200, r.text

        r = client.request(
            "DELETE",
            f"/api/v1/write/ledgers/{ledger_id}/categories/{src_id}",
            json={"base_change_id": 0},
            headers={"Authorization": f"Bearer {token}", "X-Device-ID": "d-web"},
        )
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.clear()
