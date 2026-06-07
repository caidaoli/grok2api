"""
Admin Token 管理路由
"""

import asyncio
from typing import Any

import orjson
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.core.auth import verify_app_key
from app.core.config import get_config
from app.core.logger import logger
from app.core.storage import get_storage
from app.services.token.account_settings import (
    refresh_account_settings_for_tokens,
    normalize_sso_token as normalize_refresh_token,
)
from app.services.token import get_token_manager
from app.services.token.models import DEFAULT_QUOTA, TokenInfo, TokenStatus
from app.api.v1.admin.common import _safe_int

router = APIRouter()


def _pool_to_token_type(pool_name: str) -> str:
    return "ssoSuper" if str(pool_name or "").strip() == "ssoSuper" else "sso"


def _parse_quota_value(v: Any) -> tuple[int, bool]:
    if v is None or v == "":
        return -1, False
    try:
        n = int(v)
    except Exception:
        return -1, False
    if n < 0:
        return -1, False
    return n, True


def _normalize_token_status(raw_status: Any) -> str:
    s = str(raw_status or "active").strip().lower()
    if s in ("expired", "invalid"):
        return "expired"
    if s in ("active", "cooling", "disabled"):
        return s
    return "active"


def _normalize_admin_token_item(pool_name: str, item: Any) -> dict | None:
    token_type = _pool_to_token_type(pool_name)

    if isinstance(item, str):
        token = item.strip()
        if not token:
            return None
        if token.startswith("sso="):
            token = token[4:]
        return {
            "token": token,
            "status": "active",
            "quota": 0,
            "quota_known": False,
            "heavy_quota": -1,
            "heavy_quota_known": False,
            "token_type": token_type,
            "note": "",
            "fail_count": 0,
            "use_count": 0,
        }

    if not isinstance(item, dict):
        return None

    token = str(item.get("token") or "").strip()
    if not token:
        return None
    if token.startswith("sso="):
        token = token[4:]

    quota, quota_known = _parse_quota_value(item.get("quota"))
    heavy_quota, heavy_quota_known = _parse_quota_value(item.get("heavy_quota"))

    return {
        "token": token,
        "status": _normalize_token_status(item.get("status")),
        "quota": quota if quota_known else 0,
        "quota_known": quota_known,
        "heavy_quota": heavy_quota,
        "heavy_quota_known": heavy_quota_known,
        "token_type": token_type,
        "note": str(item.get("note") or ""),
        "fail_count": _safe_int(item.get("fail_count") or 0, 0),
        "use_count": _safe_int(item.get("use_count") or 0, 0),
    }


def _collect_tokens_from_pool_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []

    collected: list[str] = []
    seen: set[str] = set()
    for raw_items in payload.values():
        if not isinstance(raw_items, list):
            continue
        for item in raw_items:
            token_raw = item if isinstance(item, str) else (item.get("token") if isinstance(item, dict) else "")
            token = normalize_refresh_token(str(token_raw or "").strip())
            if not token or token in seen:
                continue
            seen.add(token)
            collected.append(token)
    return collected


def _collect_tokens_from_delete_payload(payload: Any) -> list[str]:
    candidates: list[str] = []
    if isinstance(payload, dict):
        if isinstance(payload.get("token"), str):
            candidates.append(payload["token"])
        if isinstance(payload.get("tokens"), list):
            candidates.extend(item for item in payload["tokens"] if isinstance(item, str))

    collected: list[str] = []
    seen: set[str] = set()
    for raw in candidates:
        token = normalize_refresh_token(str(raw or "").strip())
        if not token or token in seen:
            continue
        seen.add(token)
        collected.append(token)
    return collected


def _collect_tokens_from_refresh_payload(payload: Any) -> list[str]:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        if "token" in payload:
            candidates.append(payload["token"])
        if isinstance(payload.get("tokens"), list):
            candidates.extend(payload["tokens"])

    return list(dict.fromkeys(
        token for token in (normalize_refresh_token(str(t or "")) for t in candidates) if token
    ))


def _collect_import_token_records(pool_name: str, payload: Any) -> list[dict]:
    raw_items: Any = []
    if isinstance(payload, dict):
        raw_items = payload.get("tokens")
    if not isinstance(raw_items, list):
        return []

    records: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, str):
            token = normalize_refresh_token(item)
            source: dict[str, Any] = {}
        elif isinstance(item, dict):
            token = normalize_refresh_token(str(item.get("token") or ""))
            source = item
        else:
            continue

        if not token or token in seen:
            continue
        seen.add(token)

        quota, quota_known = _parse_quota_value(source.get("quota"))
        heavy_quota, heavy_quota_known = _parse_quota_value(source.get("heavy_quota"))
        records.append({
            "token": token,
            "status": _normalize_token_status(source.get("status")),
            "quota": quota if quota_known else DEFAULT_QUOTA,
            "heavy_quota": heavy_quota if heavy_quota_known else -1,
            "note": str(source.get("note") or "")[:50],
            "fail_count": _safe_int(source.get("fail_count") or 0, 0),
            "use_count": _safe_int(source.get("use_count") or 0, 0),
        })
    return records


def _token_info_to_admin_record(pool_name: str, info: TokenInfo) -> dict | None:
    if hasattr(info, "model_dump"):
        data = info.model_dump(mode="json")
    else:
        data = dict(getattr(info, "__dict__", {}))
    data.pop("inflight_map", None)
    obj = _normalize_admin_token_item(pool_name, data)
    if obj:
        obj["pool"] = pool_name
    return obj


def _find_manager_token_record(mgr: Any, token: str) -> dict | None:
    raw = normalize_refresh_token(token)
    if not raw:
        return None
    for pool_name, pool in getattr(mgr, "pools", {}).items():
        info = pool.get(raw)
        if info:
            return _token_info_to_admin_record(str(pool_name), info)
    return None


def _resolve_nsfw_refresh_concurrency(override: Any = None) -> int:
    source = override if override is not None else get_config("token.nsfw_refresh_concurrency", 10)
    try:
        value = int(source)
    except Exception:
        value = 10
    return max(1, value)


def _resolve_nsfw_refresh_retries(override: Any = None) -> int:
    source = override if override is not None else get_config("token.nsfw_refresh_retries", 3)
    try:
        value = int(source)
    except Exception:
        value = 3
    return max(0, value)


def _trigger_account_settings_refresh_background(
    tokens: list[str],
    concurrency: int,
    retries: int,
) -> None:
    if not tokens:
        return

    async def _run() -> None:
        try:
            result = await refresh_account_settings_for_tokens(
                tokens=tokens,
                concurrency=concurrency,
                retries=retries,
            )
            summary = result.get("summary") or {}
            logger.info(
                "Background account-settings refresh finished: total={} success={} failed={} invalidated={}",
                summary.get("total", 0),
                summary.get("success", 0),
                summary.get("failed", 0),
                summary.get("invalidated", 0),
            )
        except Exception as exc:
            logger.warning("Background account-settings refresh failed: {}", exc)

    asyncio.create_task(_run())


async def _refresh_one_token_usage(mgr: Any, token: str) -> bool:
    ok = await mgr.sync_usage(
        token,
        "grok-3",
        consume_on_fail=False,
        is_usage=False,
        retry=False,
    )
    if ok:
        token_info, _ = mgr._find_token_info(token)
        if token_info and token_info.status != TokenStatus.ACTIVE:
            token_info.status = TokenStatus.ACTIVE
    return bool(ok)


def _encode_refresh_sse_event(payload: dict) -> str:
    return f"data: {orjson.dumps(payload).decode()}\n\n"


async def _iter_refresh_stream_events(mgr: Any, tokens: list[str], concurrency: int = 10):
    total = len(tokens)
    current = 0
    success = 0
    failed = 0
    save_scheduled = False
    refreshed_records: list[dict] = []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run(token: str) -> tuple[str, bool, str | None]:
        async with sem:
            try:
                return token, await _refresh_one_token_usage(mgr, token), None
            except Exception as exc:
                logger.warning("Admin token stream refresh failed for {}: {}", token[-8:], exc)
                return token, False, str(exc)

    tasks = [asyncio.create_task(_run(token)) for token in tokens]
    try:
        for task in asyncio.as_completed(tasks):
            token, ok, error = await task
            current += 1
            if ok:
                success += 1
            else:
                failed += 1

            event = {
                "type": "progress",
                "token": token,
                "ok": ok,
                "current": current,
                "total": total,
                "success": success,
                "failed": failed,
            }
            if error:
                event["error"] = error
            if ok:
                record = _find_manager_token_record(mgr, token)
                if record:
                    refreshed_records.append(record)
                    event["record"] = record
            yield _encode_refresh_sse_event(event)

        if success > 0:
            mgr._schedule_save()
            save_scheduled = True

        yield _encode_refresh_sse_event({
            "type": "complete",
            "current": current,
            "total": total,
            "success": success,
            "failed": failed,
            "tokens": refreshed_records,
        })
    finally:
        if success > 0 and not save_scheduled:
            mgr._schedule_save()
        for task in tasks:
            if not task.done():
                task.cancel()
        pending = [task for task in tasks if not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


# ==================== Routes ====================


@router.get("/api/v1/admin/tokens", dependencies=[Depends(verify_app_key)])
async def get_tokens_api():
    """获取所有 Token"""
    storage = get_storage()
    tokens = await storage.load_tokens()
    data = tokens if isinstance(tokens, dict) else {}
    out: dict[str, list[dict]] = {}
    for pool_name, raw_items in data.items():
        arr = raw_items if isinstance(raw_items, list) else []
        normalized: list[dict] = []
        for item in arr:
            obj = _normalize_admin_token_item(pool_name, item)
            if obj:
                normalized.append(obj)
        out[str(pool_name)] = normalized
    return out


@router.post("/api/v1/admin/tokens", dependencies=[Depends(verify_app_key)])
async def update_tokens_api(data: dict):
    """Update token payload and trigger background account-settings refresh for new tokens."""
    storage = get_storage()
    try:
        mgr = await get_token_manager()

        posted_data = data if isinstance(data, dict) else {}
        existing_tokens: list[str] = []
        added_tokens: list[str] = []

        # Cancel any background save to avoid competing for the same MySQL lock.
        await mgr.cancel_pending_save()

        async with storage.acquire_lock("tokens_save", timeout=30):
            old_data = await storage.load_tokens()
            existing_tokens = _collect_tokens_from_pool_payload(
                old_data if isinstance(old_data, dict) else {}
            )

            await storage.save_tokens(posted_data)
            await mgr.reload()

            new_tokens = _collect_tokens_from_pool_payload(posted_data)
            existing_set = set(existing_tokens)
            added_tokens = [token for token in new_tokens if token not in existing_set]

        concurrency = _resolve_nsfw_refresh_concurrency()
        retries = _resolve_nsfw_refresh_retries()
        _trigger_account_settings_refresh_background(
            tokens=added_tokens,
            concurrency=concurrency,
            retries=retries,
        )

        return {
            "status": "success",
            "message": "Token updated",
            "nsfw_refresh": {
                "mode": "background",
                "triggered": len(added_tokens),
                "concurrency": concurrency,
                "retries": retries,
            },
        }
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/v1/admin/tokens/import", dependencies=[Depends(verify_app_key)])
async def import_tokens_api(data: dict):
    """Import missing tokens without rewriting the full token set."""
    payload = data if isinstance(data, dict) else {}
    pool_name = str(payload.get("pool") or "ssoBasic").strip() or "ssoBasic"
    records = _collect_import_token_records(pool_name, payload)
    if not records:
        raise HTTPException(status_code=400, detail="No tokens provided")

    storage = get_storage()
    try:
        mgr = await get_token_manager()
        await mgr.cancel_pending_save()

        async with storage.acquire_lock("tokens_save", timeout=30):
            added_tokens = await storage.add_tokens(pool_name, records)
            by_token = {record["token"]: record for record in records}
            added_records = [by_token[token] for token in added_tokens if token in by_token]
            if added_records and hasattr(mgr, "add_token_records"):
                mgr.add_token_records(pool_name, added_records)

        concurrency = _resolve_nsfw_refresh_concurrency()
        retries = _resolve_nsfw_refresh_retries()
        _trigger_account_settings_refresh_background(
            tokens=added_tokens,
            concurrency=concurrency,
            retries=retries,
        )

        response_records: list[dict] = []
        for token in added_tokens:
            record = _find_manager_token_record(mgr, token)
            if record:
                response_records.append(record)
            elif token in by_token:
                fallback = _normalize_admin_token_item(pool_name, by_token[token])
                if fallback:
                    fallback["pool"] = pool_name
                    response_records.append(fallback)

        return {
            "status": "success",
            "added": len(added_tokens),
            "skipped": len(records) - len(added_tokens),
            "tokens": response_records,
            "nsfw_refresh": {
                "mode": "background",
                "triggered": len(added_tokens),
                "concurrency": concurrency,
                "retries": retries,
            },
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("Admin token import API error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/v1/admin/tokens/delete", dependencies=[Depends(verify_app_key)])
async def delete_tokens_api(data: dict):
    """Delete selected tokens without rewriting the full token set."""
    tokens = _collect_tokens_from_delete_payload(data)
    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    storage = get_storage()
    try:
        mgr = await get_token_manager()

        # Prevent a delayed full save from resurrecting rows after targeted delete.
        await mgr.cancel_pending_save()

        async with storage.acquire_lock("tokens_save", timeout=30):
            deleted = await storage.delete_tokens(tokens)
            if deleted and hasattr(mgr, "remove_tokens"):
                mgr.remove_tokens(tokens)

        return {"status": "success", "deleted": deleted}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Admin token delete API error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/v1/admin/tokens/refresh", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_api(data: dict):
    """刷新 Token 状态"""
    try:
        mgr = await get_token_manager()
        unique_tokens = _collect_tokens_from_refresh_payload(data)

        if not unique_tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        sem = asyncio.Semaphore(10)

        async def _refresh_one(t):
            async with sem:
                return t, await _refresh_one_token_usage(mgr, t)

        results_list = await asyncio.gather(*[_refresh_one(t) for t in unique_tokens])
        results = dict(results_list)
        refreshed = [
            record for record in (_find_manager_token_record(mgr, token) for token, ok in results.items() if ok)
            if record
        ]

        any_changed = any(results.values())
        if any_changed:
            mgr._schedule_save()

        return {"status": "success", "results": results, "tokens": refreshed}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/v1/admin/tokens/refresh/stream", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_stream_api(data: dict):
    """刷新 Token 状态，并通过长连接返回逐项进度。"""
    mgr = await get_token_manager()
    unique_tokens = _collect_tokens_from_refresh_payload(data)
    if not unique_tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    return StreamingResponse(
        _iter_refresh_stream_events(mgr, unique_tokens),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/v1/admin/tokens/reset", dependencies=[Depends(verify_app_key)])
async def reset_tokens_api(data: dict):
    """重置 Token 状态（将 disabled/expired 恢复为 active）"""
    try:
        mgr = await get_token_manager()
        tokens: list[str] = []
        if "token" in data:
            tokens.append(data["token"])
        if "tokens" in data and isinstance(data["tokens"], list):
            tokens.extend(data["tokens"])

        if not tokens:
            raise HTTPException(status_code=400, detail="No tokens provided")

        unique_tokens = list(dict.fromkeys(
            token for token in (normalize_refresh_token(str(t or "")) for t in tokens) if token
        ))
        results = {}
        for t in unique_tokens:
            results[t] = await mgr.reset_token(t)
        reset_records = [
            record for record in (_find_manager_token_record(mgr, token) for token, ok in results.items() if ok)
            if record
        ]

        return {"status": "success", "results": results, "tokens": reset_records}
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/api/v1/admin/tokens/nsfw/refresh", dependencies=[Depends(verify_app_key)])
async def refresh_tokens_nsfw_api(data: dict):
    """Refresh account settings (TOS + birth date + NSFW) for selected/all tokens."""
    payload = data if isinstance(data, dict) else {}
    mgr = await get_token_manager()

    tokens: list[str] = []
    seen: set[str] = set()

    if bool(payload.get("all")):
        for pool in mgr.pools.values():
            for info in pool.list():
                token = normalize_refresh_token(str(info.token or "").strip())
                if not token or token in seen:
                    continue
                seen.add(token)
                tokens.append(token)
    else:
        candidates: list[str] = []
        single = payload.get("token")
        if isinstance(single, str):
            candidates.append(single)
        batch = payload.get("tokens")
        if isinstance(batch, list):
            candidates.extend([item for item in batch if isinstance(item, str)])

        for raw in candidates:
            token = normalize_refresh_token(str(raw or "").strip())
            if not token or token in seen:
                continue
            seen.add(token)
            tokens.append(token)

    if not tokens:
        raise HTTPException(status_code=400, detail="No tokens provided")

    concurrency = _resolve_nsfw_refresh_concurrency(payload.get("concurrency"))
    retries = _resolve_nsfw_refresh_retries(payload.get("retries"))
    result = await refresh_account_settings_for_tokens(
        tokens=tokens,
        concurrency=concurrency,
        retries=retries,
    )
    return {
        "status": "success",
        "summary": result.get("summary") or {},
        "failed": result.get("failed") or [],
    }
