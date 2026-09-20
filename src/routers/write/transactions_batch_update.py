"""POST /api/v1/write/ledgers/{ledger_id}/transactions/batch/update — 批量修改交易分类。

跟 transactions_batch_delete.py 同模式:单 snapshot lock + 循环 mutator +
一次 SyncChange broadcast + 一次 idempotency。区别:

- 跑 `update_transaction` mutator,只传 category 三字段(presence 语义,
  不碰其它字段)
- `category_kind` 只允许 expense/income(transfer 交易没有分类);tx.type
  与 category_kind 不一致的条目记 `failed(kind_mismatch)` 跳过,防止误改
  transfer/跨类型交易 — 前端会预过滤,这里是服务端兜底
- 部分失败按 tx 粒度返回 `failed[]`,只在事务级错误才 500
- 不开放 base_change_id 严格校验(跟批量删除同口径)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, status
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


class BatchTxUpdateRequest(BaseModel):
    tx_ids: list[str] = Field(..., min_length=1, max_length=200)
    category_id: str
    category_name: str = Field(..., min_length=1)
    category_kind: Literal["expense", "income"]
    base_change_id: int = 0


class BatchTxUpdateFailure(BaseModel):
    tx_id: str
    reason: Literal["not_found", "permission_denied", "conflict", "kind_mismatch"]
    message: str | None = None


class BatchTxUpdateResponse(BaseModel):
    ledger_id: str
    base_change_id: int
    new_change_id: int
    server_timestamp: datetime
    updated_tx_ids: list[str] = Field(default_factory=list)
    failed: list[BatchTxUpdateFailure] = Field(default_factory=list)


@router.post(
    "/ledgers/{ledger_id}/transactions/batch/update",
    response_model=BatchTxUpdateResponse,
    responses=_WRITE_RESPONSES,
)
async def update_tx_batch_category(
    ledger_id: str,
    req: BatchTxUpdateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    device_id: str = Header(default="web-console", alias="X-Device-ID"),
    _scopes: set[str] = Depends(_WRITE_SCOPE_DEP),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> BatchTxUpdateResponse:
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
        # 同 batch/delete:从 DB 拿原始 response_json 重建完整响应。
        row = db.scalar(
            select(SyncPushIdempotency).where(
                SyncPushIdempotency.user_id == current_user.id,
                SyncPushIdempotency.device_id == device_id,
                SyncPushIdempotency.idempotency_key == idempotency_key,
            )
        )
        if row is not None and row.response_json:
            return BatchTxUpdateResponse.model_validate(row.response_json)
        return BatchTxUpdateResponse(
            ledger_id=ledger.external_id,
            base_change_id=req.base_change_id,
            new_change_id=replay.new_change_id,
            server_timestamp=replay.server_timestamp,
            updated_tx_ids=[],
            failed=[],
        )

    # 去重 —— 同一 sync_id 只改一次
    unique_ids: list[str] = []
    seen: set[str] = set()
    for tx_id in req.tx_ids:
        if tx_id and tx_id not in seen:
            unique_ids.append(tx_id)
            seen.add(tx_id)

    lock_ledger_for_materialize(db, ledger.id)
    snapshot = snapshot_builder.build(db, ledger)
    # 归一成 UTC 再拷 prev,避免 happenedAt 时区格式差异导致 _emit_entity_diffs
    # 把未变更的交易误判为 changed(全账本误 emit)。同 _commit_write。
    snapshot = ensure_snapshot_v2(snapshot)
    # 深拷贝快照用于 diff(跟 batch/delete 同模式)
    prev_snapshot = {**snapshot}
    for _k in ("items", "accounts", "categories", "tags", "budgets"):
        arr = snapshot.get(_k)
        if isinstance(arr, list):
            prev_snapshot[_k] = [dict(e) if isinstance(e, dict) else e for e in arr]

    category_payload = _payload_with_actor(
        {
            "category_id": req.category_id,
            "category_name": req.category_name,
            "category_kind": req.category_kind,
        },
        current_user,
    )

    updated_ids: list[str] = []
    failed: list[BatchTxUpdateFailure] = []
    tx_by_id = _tx_index(snapshot)

    for tx_id in unique_ids:
        try:
            item = tx_by_id.get(tx_id)
            if item is None:
                raise KeyError(tx_id)
            if str(item.get("type") or "") != req.category_kind:
                failed.append(
                    BatchTxUpdateFailure(
                        tx_id=tx_id,
                        reason="kind_mismatch",
                        message=f"tx type {item.get('type') or 'unknown'} != category kind {req.category_kind}",
                    )
                )
                continue
            snapshot = update_transaction(snapshot, tx_id, category_payload)
            updated_ids.append(tx_id)
        except KeyError:
            failed.append(
                BatchTxUpdateFailure(tx_id=tx_id, reason="not_found", message="transaction not in ledger")
            )
        except PermissionError as exc:
            failed.append(
                BatchTxUpdateFailure(tx_id=tx_id, reason="permission_denied", message=str(exc))
            )
        except ValueError as exc:
            failed.append(BatchTxUpdateFailure(tx_id=tx_id, reason="conflict", message=str(exc)))

    # diff + emit changes(只针对实际变更的 items)
    now = datetime.now(timezone.utc)
    if updated_ids:
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
            action="web_tx_batch_update",
            metadata_json={
                "ledgerId": ledger.external_id,
                "baseChangeId": req.base_change_id,
                "newChangeId": new_change_id,
                "categoryId": req.category_id,
                "categoryName": req.category_name,
                "categoryKind": req.category_kind,
                "updatedCount": len(updated_ids),
                "updatedIds": updated_ids,
                "failedCount": len(failed),
                "failedIds": [f.tx_id for f in failed],
            },
        )
    )

    response = BatchTxUpdateResponse(
        ledger_id=ledger.external_id,
        base_change_id=req.base_change_id,
        new_change_id=new_change_id,
        server_timestamp=now,
        updated_tx_ids=updated_ids,
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
                return BatchTxUpdateResponse(**replay.model_dump()) if hasattr(replay, "model_dump") else replay  # type: ignore[return-value]
        raise

    logger.info(
        "tx.batch_update ledger=%s updated=%d failed=%d change_id=%d device=%s user=%s",
        ledger.external_id, len(updated_ids), len(failed), new_change_id, device_id, current_user.id,
    )

    if updated_ids:
        # 共享账本:fan-out 给所有 LedgerMember,Editor 端 mobile 实时收到。
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


def _tx_index(snapshot: dict) -> dict[str, dict]:
    """syncId → item 索引;找不到的 id 在调用侧通过 .get() 判 None。"""
    out: dict[str, dict] = {}
    for item in snapshot.get("items") or []:
        if isinstance(item, dict) and item.get("syncId"):
            out[str(item["syncId"])] = item
    return out
