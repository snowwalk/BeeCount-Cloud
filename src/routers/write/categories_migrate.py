"""POST /api/v1/write/ledgers/{ledger_id}/categories/migrate — 分类迁移。

把本账本里引用源分类的交易全部改挂到目标分类,对齐 app 端"先迁移交易、
再删空分类"的口径。move-only:不删源分类、不碰预算(category 预算按
categoryId 挂,mutator 不允许改 categoryId;删源分类由前端迁完后走既有
DELETE 端点,此时校验天然通过)。

挂在账本下而非 user-global 路由,是为了整套复用 per-ledger 的权限/锁/
emit/broadcast 机制;跨账本由前端按账本逐个调用(读端可用
category_sync_id 过滤发现受影响的账本)。

匹配口径跟 delete_category 的拒删检查保持一致:
`categoryId == source_id OR (categoryName == source_name AND categoryKind == source_kind)`
——后者兼容旧 app 只记了名字、没有 categoryId 的存量交易。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import snapshot_builder
from ...concurrency import lock_ledger_for_materialize
from ...database import get_db
from ...deps import get_current_user
from ...models import AuditLog, SyncPushIdempotency, User
from ...snapshot_mutator import ensure_snapshot_v2, update_transaction
from ._shared import (
    _TRANSACTION_WRITE_ROLES,
    _WRITE_RESPONSES,
    _WRITE_SCOPE_DEP,
    _emit_entity_diffs,
    _hash_request,
    _load_idempotent_response,
    _payload_with_actor,
    _prepare_write,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class CategoryMigrateRequest(BaseModel):
    source_category_id: str
    target_category_id: str
    base_change_id: int = 0


class CategoryMigrateFailure(BaseModel):
    tx_id: str
    reason: Literal["permission_denied", "conflict", "kind_mismatch"]
    message: str | None = None


class CategoryMigrateResponse(BaseModel):
    ledger_id: str
    base_change_id: int
    new_change_id: int
    server_timestamp: datetime
    moved_count: int
    moved_tx_ids: list[str] = Field(default_factory=list)
    failed: list[CategoryMigrateFailure] = Field(default_factory=list)


@router.post(
    "/ledgers/{ledger_id}/categories/migrate",
    response_model=CategoryMigrateResponse,
    responses=_WRITE_RESPONSES,
)
async def migrate_category(
    ledger_id: str,
    req: CategoryMigrateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    device_id: str = Header(default="web-console", alias="X-Device-ID"),
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CategoryMigrateResponse:
    payload_for_ide = req.model_dump(mode="json")
    ledger, replay = _prepare_write(
        db=db,
        current_user=current_user,
        ledger_external_id=ledger_id,
        required_roles=_TRANSACTION_WRITE_ROLES,
        idempotency_key=idempotency_key,
        device_id=device_id,
        method=request.method,
        path=request.url.path,
        payload=payload_for_ide,
    )
    if replay:
        row = db.scalar(
            select(SyncPushIdempotency).where(
                SyncPushIdempotency.user_id == current_user.id,
                SyncPushIdempotency.device_id == device_id,
                SyncPushIdempotency.idempotency_key == idempotency_key,
            )
        )
        if row is not None and row.response_json:
            return CategoryMigrateResponse.model_validate(row.response_json)
        return CategoryMigrateResponse(
            ledger_id=ledger.external_id,
            base_change_id=req.base_change_id,
            new_change_id=replay.new_change_id,
            server_timestamp=replay.server_timestamp,
            moved_count=0,
        )

    lock_ledger_for_materialize(db, ledger.id)
    snapshot = snapshot_builder.build(db, ledger)
    # 归一成 UTC 再拷 prev,避免全账本误 emit(同 _commit_write / batch_update)。
    snapshot = ensure_snapshot_v2(snapshot)

    categories = snapshot.get("categories") or []
    source = _find_category(categories, req.source_category_id)
    target = _find_category(categories, req.target_category_id)
    if source is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="source category not found")
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="target category not found")
    if req.source_category_id == req.target_category_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="source and target category are the same")
    source_name = str(source.get("name") or "").strip()
    source_kind = str(source.get("kind") or "").strip()
    target_name = str(target.get("name") or "").strip()
    target_kind = str(target.get("kind") or "").strip()
    if not source_name or not source_kind:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="source category has empty name or kind")
    if source_kind != target_kind:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"category kind mismatch: {source_kind} -> {target_kind}",
        )
    # 跟 delete_category 同口径:有子分类拒绝(子分类里的交易不在本次迁移范围,
    # 迁完源分类依然删不掉,直接提前拒绝避免半成品状态)。
    child_count = sum(
        1
        for row in categories
        if str(row.get("syncId") or "") != req.source_category_id
        and str(row.get("parentName") or "").strip() == source_name
        and str(row.get("kind") or "").strip() == source_kind
    )
    if child_count > 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail=f"category has {child_count} child categories",
        )

    # 深拷贝快照用于 diff(跟 batch 端点同模式)
    prev_snapshot = {**snapshot}
    for _k in ("items", "accounts", "categories", "tags", "budgets"):
        arr = snapshot.get(_k)
        if isinstance(arr, list):
            prev_snapshot[_k] = [dict(e) if isinstance(e, dict) else e for e in arr]

    # 匹配源分类的交易(categoryId 优先,名字+kind 兜底 legacy 数据)
    matched = [
        item
        for item in snapshot.get("items") or []
        if isinstance(item, dict)
        and (
            str(item.get("categoryId") or "") == req.source_category_id
            or (item.get("categoryName") == source_name and item.get("categoryKind") == source_kind)
        )
    ]

    category_payload = _payload_with_actor(
        {
            "category_id": req.target_category_id,
            "category_name": target_name,
            "category_kind": target_kind,
        },
        current_user,
    )

    moved_ids: list[str] = []
    failed: list[CategoryMigrateFailure] = []
    for item in matched:
        tx_id = str(item.get("syncId") or "")
        try:
            if str(item.get("type") or "") != source_kind:
                failed.append(
                    CategoryMigrateFailure(
                        tx_id=tx_id,
                        reason="kind_mismatch",
                        message=f"tx type {item.get('type') or 'unknown'} != category kind {source_kind}",
                    )
                )
                continue
            snapshot = update_transaction(snapshot, tx_id, category_payload)
            moved_ids.append(tx_id)
        except PermissionError as exc:
            failed.append(CategoryMigrateFailure(tx_id=tx_id, reason="permission_denied", message=str(exc)))
        except ValueError as exc:
            failed.append(CategoryMigrateFailure(tx_id=tx_id, reason="conflict", message=str(exc)))

    now = datetime.now(timezone.utc)
    if moved_ids:
        emitted_change_ids = _emit_entity_diffs(
            db,
            ledger=ledger,
            current_user=current_user,
            device_id=device_id,
            prev=prev_snapshot,
            next_snapshot=snapshot,
            now=now,
        )
        new_change_id = max(emitted_change_ids) if emitted_change_ids else (
            snapshot_builder.latest_change_id(db, ledger.id)
        )
    else:
        new_change_id = snapshot_builder.latest_change_id(db, ledger.id)

    db.add(
        AuditLog(
            user_id=current_user.id,
            ledger_id=ledger.id,
            action="web_category_migrate",
            metadata_json={
                "ledgerId": ledger.external_id,
                "baseChangeId": req.base_change_id,
                "newChangeId": new_change_id,
                "sourceCategoryId": req.source_category_id,
                "targetCategoryId": req.target_category_id,
                "movedCount": len(moved_ids),
                "movedIds": moved_ids,
                "failedCount": len(failed),
                "failedIds": [f.tx_id for f in failed],
            },
        )
    )

    response = CategoryMigrateResponse(
        ledger_id=ledger.external_id,
        base_change_id=req.base_change_id,
        new_change_id=new_change_id,
        server_timestamp=now,
        moved_count=len(moved_ids),
        moved_tx_ids=moved_ids,
        failed=failed,
    )

    request_hash = _hash_request(request.method, request.url.path, payload_for_ide)
    if idempotency_key:
        db.add(
            SyncPushIdempotency(
                user_id=current_user.id,
                device_id=device_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_json=response.model_dump(mode="json"),
                created_at=now,
                expires_at=now + timedelta(hours=24),
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            replay = _load_idempotent_response(
                db,
                user_id=current_user.id,
                device_id=device_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return CategoryMigrateResponse(**replay.model_dump()) if hasattr(replay, "model_dump") else replay  # type: ignore[return-value]
        raise

    logger.info(
        "category.migrate ledger=%s source=%s target=%s moved=%d failed=%d change_id=%d device=%s user=%s",
        ledger.external_id, req.source_category_id, req.target_category_id,
        len(moved_ids), len(failed), new_change_id, device_id, current_user.id,
    )

    if moved_ids:
        from ...websocket_manager import broadcast_to_ledger
        await broadcast_to_ledger(
            db=db,
            ws_manager=request.app.state.ws_manager,
            ledger_id=ledger.id,
            payload={
                "type": "sync_change",
                "ledgerId": ledger.external_id,
                "serverCursor": new_change_id,
                "serverTimestamp": now.isoformat(),
            },
        )
    return response


def _find_category(categories: list, category_id: str) -> dict | None:
    for row in categories:
        if isinstance(row, dict) and str(row.get("syncId")) == category_id:
            return row
    return None
