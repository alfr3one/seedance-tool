"""Dola Pool: OpenAI-compatible Video API (FastAPI) and Admin Dashboard.

Endpoints (Asynchronous 2-stage):
POST /v1/videos/generations -> Create task (status=queued)
GET  /v1/videos/<id>         -> Query task status (queued/processing/completed/failed)
GET  /videos/<file>          -> Static video download server

Admin Dashboard: GET / -> web/index.html; Admin API /api/admin/*
"""
import asyncio
import hashlib
import json
import re
import shutil
import time
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

import config
from add_account import add_account_flow
from browser_pool import AllAccountsLimitedError, AllAccountsQuotaBlockedError, BrowserPool
from media import download_reference_images, validate_reference_urls
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore
from diagnostics import redact
from proxy_settings import validate_proxy, check_proxy
from video_archive import build_video_archive

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path(config.WEB_DIR).mkdir(parents=True, exist_ok=True)

app = FastAPI(title="dola-pool", version="0.4.0")

store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")

# Background jobs (add/verify), in-memory
JOBS: dict[str, dict] = {}
TASK_RUNNERS: dict[str, asyncio.Task] = {}


def schedule_task(task_id, coroutine):
    previous = TASK_RUNNERS.get(task_id)
    if previous and not previous.done():
        coroutine.close()
        raise HTTPException(409, 'Task worker is still running or stopping')
    task = asyncio.create_task(coroutine)
    TASK_RUNNERS[task_id] = task
    def finished(done):
        if TASK_RUNNERS.get(task_id) is done:
            TASK_RUNNERS.pop(task_id, None)
    task.add_done_callback(finished)
    return task

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
SUPPORTED_DURATIONS = (10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConcurrencyLimiter:
    """Concurrency limits per API Key; 0 = unlimited."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()


# ===== Authentication =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "Anonymous",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"Env Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _auth(authorization):
    """Returns client policy for caller; empty key enables dev mode."""
    if not config.API_KEYS and not store.has_enabled_keys():
        return _anonymous_client()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "missing bearer token")
    if key in config.API_KEYS:
        return _env_client(key)
    record = store.get_key(key)
    if not record or not store.is_key_valid(key):
        raise HTTPException(401, "invalid api key")
    store.touch_key(key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "Unnamed Client",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
    }


def _admin_auth(x_admin_key: str | None):
    if not config.ADMIN_KEY:
        return
    if x_admin_key != config.ADMIN_KEY:
        raise HTTPException(401, "invalid admin key")


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, "allowed_durations must be an array of 10, 15, or 30")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(422, "allowed_durations must contain at least one of 10, 15, 30")
    return normalized


# ===== Client API =====


class VideoGenRequest(BaseModel):
    model: str = "seedance-2.0"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=10, le=30)
    # Accepts durations: 10, 15, 30 seconds.
    reference_images: list[str] = Field(default_factory=list)
    target_account: str | None = None


class TaskResponse(BaseModel):
    id: str
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None
    poll_detail: str | None = None


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client, target_account=None):
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_root = None
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        store.update(task_id, status="processing", started_at=time.time())

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now, detail=None):
            store.update(task_id, last_poll_at=now, poll_detail=detail)

        reference_root, reference_paths = await download_reference_images(
            reference_images or [], task_id)
        result = await pool.generate_video(
            prompt, ratio, duration, model,
            on_conversation_id=on_conversation_id, on_poll=on_poll,
            reference_image_paths=reference_paths,
            target_account=target_account)
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError) as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     failure_code="429", finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     finished_at=time.time())
    finally:
        if reference_root:
            shutil.rmtree(reference_root, ignore_errors=True)
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row: dict):
    task_id = row["id"]
    deadline = row.get("deadline_at") or (
        time.time() + (1800 if row.get("duration") == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now, detail=None):
            store.update(task_id, last_poll_at=now, poll_detail=detail)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining,
            duration=row.get("duration"), on_poll=on_poll)
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed", error=str(e)[:500],
                     finished_at=time.time())
    finally:
        if acquired:
            await key_limiter.release(api_key_hash)


def _task_client(row: dict) -> dict:
    """Restores client context from task snapshot."""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "Historical Task",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


@app.on_event("startup")
async def resume_incomplete_tasks():
    """Recovers accepted sessions on startup and requeues pending tasks."""
    for row in store.recoverable_tasks():
        schedule_task(row['id'], _resume_task(row))
    for row in store.recoverable_queued_tasks():
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        schedule_task(row['id'], _run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
            target_account=row.get("target_account"),
        ))


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(req: VideoGenRequest, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "Currently supports durations of 10s, 15s, and 30s")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"Current API Key is not allowed to generate {duration}s videos")
    model_key = req.model.lower().replace("-", "_")
    if model_key not in (
        "seedance_2.0", "seedance_2.5", "seedance_v2.0", "seedance_v2.5",
        "seedance_20", "seedance_25", "seedance_v20", "seedance_v25",
    ):
        raise HTTPException(422, "Supported models are seedance-2.0 and seedance-2.5")
    # Dola's 15s path is only reliable in normal Pro chat, which is Seedance 2.5.
    effective_model = "seedance-2.5" if duration == 15 else req.model
    try:
        reference_images = await validate_reference_urls(req.reference_images)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Queue task when accounts are busy; reject only when pool is fully exhausted.
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "Rate limited: All accounts reached Dola daily video limit, please try again tomorrow")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "Insufficient credits: All accounts lack points, waiting for refresh")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    if req.target_account:
        target = next((a for a in pool.list_accounts() if a["name"] == req.target_account), None)
        if not target:
            raise HTTPException(422, f"Target account '{req.target_account}' not found")
        if not target["scheduling"]:
            raise HTTPException(422, f"Target account '{req.target_account}' is disabled")
        if target["login_ok"] is False:
            raise HTTPException(422, f"Target account '{req.target_account}' session is expired")
        if (target["cooling"] or target["rate_limited"] or target["quota_blocked"]
                or target["used_today"] >= target["limit"]
                or (target["credit_balance"] is not None and target["credit_balance"] < 2)):
            raise HTTPException(429, f"Target account '{req.target_account}' is currently unavailable")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    try:
        store.create(
            task_id,
            effective_model,
            req.prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            target_account=req.target_account,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    schedule_task(task_id, _run_task(
        task_id, effective_model, req.prompt, ratio, duration, reference_images, client,
        target_account=req.target_account
    ))
    return TaskResponse(id=task_id, status="queued", model=effective_model, prompt=req.prompt)


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=row["video_url"], error=row["error"],
        poll_detail=row.get('poll_detail'),
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "accounts": pool.account_status(),
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


# ===== Admin Dashboard API =====


class AdminLogin(BaseModel):
    key: str


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None


class AccountAdd(BaseModel):
    name: str
    email: str
    password: str
    totp: str


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    if not config.ADMIN_KEY:
        return {"ok": True, "auth_required": False}
    if body.key == config.ADMIN_KEY:
        return {"ok": True, "auth_required": True}
    raise HTTPException(401, "wrong admin key")


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"accounts": pool.list_accounts()}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    try:
        pool.delete_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        ok = await pool.verify_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"ok": ok}



@app.post("/api/admin/tasks/{task_id}/resume")
async def admin_task_resume(task_id: str, x_admin_key: str | None = Header(default=None)):
    """Resumes a failed task that still has a conversation_id (e.g. timed out)."""
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, "task not found")
    if not row.get("conversation_id"):
        raise HTTPException(422, "task has no conversation_id to resume")
    if not row.get("account"):
        raise HTTPException(422, "task has no account assigned")
    if row.get('status') != 'failed' or task_id in TASK_RUNNERS:
        raise HTTPException(409, 'Only a stopped failed task can be resumed')
    # Reset to processing so resume can run
    store.update(task_id, status="processing", error=None, finished_at=None,
                 failure_code=None, poll_detail=None, last_poll_at=time.time())
    schedule_task(task_id, _resume_task(row))
    return {"ok": True, "task_id": task_id}


@app.post("/api/admin/tasks/{task_id}/cancel")
async def admin_task_cancel(task_id: str, x_admin_key: str | None = Header(default=None)):
    """Marks a stuck processing/queued task as cancelled."""
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, "task not found")
    if row.get("status") not in ("processing", "queued"):
        raise HTTPException(422, "only queued or processing tasks can be cancelled")
    store.update(task_id, status="failed", error="Cancelled by admin", failure_code='cancelled',
                 poll_detail='Local worker cancelled; upstream video may still finish.', finished_at=time.time())
    worker = TASK_RUNNERS.get(task_id)
    if worker and not worker.done():
        worker.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=15)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            return {"ok": True, "worker_stopping": True}
    return {"ok": True}


@app.delete("/api/admin/tasks/failed")
async def admin_tasks_clear_failed(x_admin_key: str | None = Header(default=None)):
    """Deletes all failed tasks."""
    _admin_auth(x_admin_key)
    store.delete_failed_tasks()
    return {"ok": True}


async def _retry_task(row: dict):
    """Re-runs a failed task from scratch."""
    task_id = row["id"]
    store.update(task_id, status="processing", error=None, account=None,
                 conversation_id=None, deadline_at=None, finished_at=None,
                 failure_code=None, poll_detail=None,
                 started_at=time.time(), last_poll_at=time.time())
    client = _task_client(row)
    ratio = row.get("ratio")
    if ratio == "default":
        ratio = None
    await _run_task(task_id, row["model"], row["prompt"], ratio,
                    row["duration"], _task_reference_images(row.get("reference_images")), client,
                    target_account=row.get("target_account"))


@app.post("/api/admin/tasks/{task_id}/retry")
async def admin_task_retry(task_id: str, x_admin_key: str | None = Header(default=None)):
    """Re-generates a failed task from scratch (new browser session)."""
    _admin_auth(x_admin_key)
    row = store.get(task_id)
    if not row:
        raise HTTPException(404, "task not found")
    if row.get("status") not in ("failed",):
        raise HTTPException(422, "only failed tasks can be retried")
    schedule_task(task_id, _retry_task(row))
    return {"ok": True, "task_id": task_id}


async def _run_add_job(name: str, email: str, password: str, totp: str):
    JOBS[name] = {"kind": "add", "status": "running", "error": "", "started_at": time.time()}
    try:
        await add_account_flow(name, email, password, totp)
        pool.set_email(name, email)
        pool.set_login_status(name, True)
        JOBS[name] = {**JOBS[name], "status": "success"}
    except Exception as e:
        JOBS[name] = {**JOBS[name], "status": "failed", "error":
            redact(f'{type(e).__name__}: {e}', (email,password,totp))[:1500],
            "log_file": "logs/account-login.log"}


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "account exists")
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
    asyncio.create_task(_run_add_job(body.name, body.email, body.password, body.totp))
    return {"ok": True, "job": "running"}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": JOBS}


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = 50, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"tasks": store.recent_tasks(min(max(limit, 1), 200))}


@app.post('/api/admin/videos/download-latest')
async def admin_download_latest(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    names = set(pool.accounts)
    rows = [row for row in store.latest_completed_videos() if row['account'] in names]
    try:
        archive, count, skipped = await asyncio.to_thread(build_video_archive, rows, config.DOWNLOAD_DIR)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(archive, media_type='application/zip',
        filename=f'dola-videos-{time.strftime("%Y%m%d-%H%M%S")}.zip',
        headers={'X-Video-Count':str(count),'X-Skipped-Count':str(skipped)},
        background=BackgroundTask(archive.unlink, missing_ok=True))


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = store.stats()
    accs = pool.list_accounts()
    sched = [a for a in accs if a["scheduling"] and not a["cooling"]]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if a["remaining"] > 0)
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
    return st


@app.get("/api/admin/settings")
async def admin_settings_get(x_admin_key: str | None = Header(default=None)):
    """Returns current runtime settings."""
    _admin_auth(x_admin_key)
    return {
        "max_concurrency": config.MAX_CONCURRENCY,
        "max_concurrency_current": config.MAX_CONCURRENCY,
        "semaphore_value": pool.semaphore._value,
        "video_timeout": config.VIDEO_TIMEOUT,
        "conversation_id_timeout": config.CONVERSATION_ID_TIMEOUT,
        "daily_limit_per_account": 10,
        "proxy": config.PROXY,
        "proxy_enabled": bool(config.PROXY),
    }


class SettingsPatch(BaseModel):
    max_concurrency: int | None = Field(None, ge=1, le=20)
    proxy: str | None = None


@app.patch("/api/admin/settings")
async def admin_settings_patch(body: SettingsPatch,
                               x_admin_key: str | None = Header(default=None)):
    """Live-updates concurrency limit without server restart."""
    _admin_auth(x_admin_key)
    if body.proxy is not None:
        try:
            value = validate_proxy(body.proxy)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        _update_env_local('DOLA_PROXY', value)
        config.PROXY = value
    if body.max_concurrency is not None:
        diff = body.max_concurrency - config.MAX_CONCURRENCY
        # Adjust the semaphore value live
        if diff > 0:
            for _ in range(diff):
                pool.semaphore.release()
        elif diff < 0:
            for _ in range(abs(diff)):
                await pool.semaphore.acquire()
        config.MAX_CONCURRENCY = body.max_concurrency
        # Persist to .env.local
        _update_env_local("DOLA_MAX_CONCURRENCY", str(body.max_concurrency))
    return {"ok": True, "max_concurrency": config.MAX_CONCURRENCY,
            "proxy": config.PROXY, "message": "Đã lưu. Áp dụng cho trình duyệt mở mới; task đang chạy giữ kết nối cũ."}


@app.post('/api/admin/settings/proxy-test')
async def admin_proxy_test(body: SettingsPatch, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        value = validate_proxy(body.proxy if body.proxy is not None else config.PROXY)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return await check_proxy(value)


def _update_env_local(key: str, value: str):
    """Updates or adds a key in .env.local file."""
    env_path = Path(".env.local")
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    new_lines = []
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"{key} ="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    keys = []
    for key in store.list_keys():
        usage = store.key_usage(store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {"keys": keys, "env_keys": len(config.API_KEYS)}


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    store.update_key(key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


# Dashboard single-file frontend
app.mount("/", StaticFiles(directory=config.WEB_DIR, html=True), name="web")

