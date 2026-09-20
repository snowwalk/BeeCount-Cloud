"""回归:web 写路径 diff 前,prev 快照必须先做 ensure_snapshot_v2 归一。

生产事故:PG 上删除一个分类恒定 ~5s。snapshot_builder.build() 原样输出
投影里的 happenedAt(保留原始时区,如 +08:00),而 mutator 内部
ensure_snapshot_v2 会归一成 UTC(+00:00)。prev/next 形状不一致导致
_emit_entity_diffs 把全账本交易误判为 changed —— 每删一个分类 emit
800+ 条 SyncChange(逐条 INSERT+flush),请求 5s。

这里 monkeypatch snapshot_builder.build 返回 build 形状(未归一)的快照,
走真实 _commit_write(delete_category),断言 SyncChange 里只有 category
delete,交易未被 emit。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src import snapshot_builder
from src.database import Base
from src.models import Ledger, SyncChange, User
from src.routers.write._shared import _commit_write
from src.snapshot_mutator import delete_category

_TX_WITH_TZ = {
    "syncId": "tx1",
    "type": "expense",
    "amount": 10.5,
    # build() 形状:保留原始时区偏移(投影里存的值原样输出)
    "happenedAt": "2026-08-31T12:10:33+08:00",
}


def _make_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _request() -> Request:
    from fastapi import FastAPI

    from src.websocket_manager import WSConnectionManager

    app = FastAPI()
    app.state.ws_manager = WSConnectionManager()
    return Request({
        "type": "http",
        "method": "DELETE",
        "path": "/api/v1/write/ledgers/lg1/categories/cat1",
        "query_string": b"",
        "headers": [],
        "app": app,
    })


def test_commit_write_diff_only_emits_real_changes(monkeypatch):
    engine, sf = _make_db()
    with sf() as db:
        db.add(User(id="u1", email="a@b.c", password_hash="x", is_enabled=True))
        db.add(Ledger(id=1, external_id="lg1", user_id="u1", name="L", currency="CNY"))
        db.commit()
        user = db.get(User, "u1")
        ledger = db.get(Ledger, 1)

        monkeypatch.setattr(snapshot_builder, "build", lambda db, ledger: {
            "ledgerName": "L",
            "currency": "CNY",
            "items": [dict(_TX_WITH_TZ)],
            "accounts": [],
            "categories": [
                {"syncId": "cat1", "name": "餐", "kind": "expense"},
                {"syncId": "cat2", "name": "饮", "kind": "expense"},
            ],
            "tags": [],
        })

        meta = asyncio.run(_commit_write(
            request=_request(),
            db=db,
            current_user=user,
            ledger=ledger,
            base_change_id=0,
            request_payload={},
            idempotency_key=None,
            device_id="web-test",
            audit_action="web_category_delete",
            mutate=lambda snap: (delete_category(snap, "cat1", {}), "cat1"),
        ))

        assert meta.entity_id == "cat1"
        rows = db.execute(
            select(SyncChange.entity_type, SyncChange.action, func.count())
            .group_by(SyncChange.entity_type, SyncChange.action)
        ).all()
        counts = {(t, a): n for t, a, n in rows}
        # 唯一允许的变更:cat1 delete。交易/其他分类未被误 emit。
        assert counts.get(("category", "delete")) == 1
        assert counts.get(("transaction", "upsert"), 0) == 0
        assert counts.get(("category", "upsert"), 0) == 0
        assert sum(counts.values()) == 1
