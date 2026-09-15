"""Browser Account Pool: Manages accounts/ profiles with concurrency control and daily limits."""
import asyncio
import shutil
import sqlite3
import time
import uuid
from datetime import date, datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from dola_client import CreditError
from video_worker_ui import (
    AccountLimitedError, CreditInsufficientError, RiskControlError, SessionExpiredError,
    generate_video, resume_video,
)
import config

DAILY_LIMIT = 10
COOLDOWN_SEC = 1800  # 30-minute cooldown on risk control


class AllAccountsLimitedError(RuntimeError):
    """All active schedulable accounts have reached daily video limit."""


class AllAccountsQuotaBlockedError(RuntimeError):
    """All active schedulable accounts are known to have insufficient credits."""


class BrowserPool:
    def __init__(self, accounts_dir: str = "accounts", db_path: str = "pool_usage.db",
                 max_concurrency: int = 1):
        self.accounts_dir = Path(accounts_dir)
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self._locks: dict[str, asyncio.Lock] = {}
        self._account_condition = asyncio.Condition()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS usage (account TEXT, day TEXT, used INTEGER, "
            "PRIMARY KEY(account, day))"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts_meta (
                name TEXT PRIMARY KEY,
                scheduling INTEGER DEFAULT 1,
                note TEXT DEFAULT '',
                email TEXT DEFAULT '',
                created_at REAL,
                last_used_at REAL DEFAULT 0,
                login_ok INTEGER,
                login_checked_at REAL DEFAULT 0,
                cooldown_until REAL DEFAULT 0,
                rate_limited_until REAL DEFAULT 0,
                limit_reason TEXT DEFAULT '',
                quota_blocked_until REAL DEFAULT 0,
                quota_reason TEXT DEFAULT '',
                credit_balance INTEGER,
                credit_checked_at REAL DEFAULT 0
            )
            """
        )
        self._conn.commit()
        # Legacy migration: add metadata columns
        for column, definition in (
            ("email", "TEXT DEFAULT ''"),
            ("rate_limited_until", "REAL DEFAULT 0"),
            ("limit_reason", "TEXT DEFAULT ''"),
            ("quota_blocked_until", "REAL DEFAULT 0"),
            ("quota_reason", "TEXT DEFAULT ''"),
            ("credit_balance", "INTEGER"),
            ("credit_checked_at", "REAL DEFAULT 0"),
            ("instance_id", "TEXT"),
        ):
            try:
                self._conn.execute(f"ALTER TABLE accounts_meta ADD COLUMN {column} {definition}")
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS usage_by_instance ('
            'instance_id TEXT, day TEXT, used INTEGER, PRIMARY KEY(instance_id, day))')
        self._conn.execute('CREATE TABLE IF NOT EXISTS account_instances ('
                           'instance_id TEXT PRIMARY KEY, name TEXT, created_at REAL)')
        # Migrate existing profiles once. Preserve their counters on restart;
        # legacy name-keyed usage is retained as an audit trail, never reused.
        for row in self._conn.execute(
                "SELECT name FROM accounts_meta WHERE instance_id IS NULL").fetchall():
            instance_id = uuid.uuid4().hex
            self._conn.execute('UPDATE accounts_meta SET instance_id=? WHERE name=?',
                               (instance_id, row['name']))
            self._conn.execute(
                'INSERT INTO usage_by_instance SELECT ?, day, used FROM usage WHERE account=?',
                (instance_id, row['name']))
        self._conn.execute('INSERT OR IGNORE INTO account_instances '
                           'SELECT instance_id, name, created_at FROM accounts_meta')
        self._conn.commit()

    # ===== Account Discovery & Metadata =====

    def _ensure_meta(self, name: str) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO accounts_meta (name, created_at, instance_id) VALUES (?, ?, ?)",
            (name, time.time(), uuid.uuid4().hex),
        )
        inserted = cursor.rowcount > 0
        if inserted:
            self._conn.execute('INSERT INTO account_instances '
                               'SELECT instance_id,name,created_at FROM accounts_meta WHERE name=?', (name,))
        return inserted

    @property
    def accounts(self) -> list:
        if not self.accounts_dir.exists():
            return []
        names = sorted(d.name for d in self.accounts_dir.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
        changed = False
        for name in names:
            changed = self._ensure_meta(name) or changed
        if changed:
            self._conn.commit()
        return names

    def _meta(self, name: str):
        return self._conn.execute(
            "SELECT * FROM accounts_meta WHERE name=?", (name,)).fetchone()

    def used_today(self, account: str) -> int:
        if self._ensure_meta(account):
            self._conn.commit()
        row = self._conn.execute(
            "SELECT used FROM usage_by_instance WHERE instance_id=? AND day=?",
            (self._meta(account)['instance_id'], date.today().isoformat()),
        ).fetchone()
        return row[0] if row else 0

    def _claim(self, account: str):
        self._ensure_meta(account)
        self._conn.execute(
            "INSERT INTO usage_by_instance(instance_id, day, used) VALUES (?,?,1) "
            "ON CONFLICT(instance_id, day) DO UPDATE SET used=used+1",
            (self._meta(account)['instance_id'], date.today().isoformat()),
        )
        self._conn.commit()

    def _next_limit_reset(self) -> float:
        """Calculates next daily quota reset timestamp."""
        try:
            tz = ZoneInfo(config.LIMIT_RESET_TZ)
        except Exception:
            # Fallback to fixed offset if tzdata is not installed.
            offsets = {"Asia/Tokyo": 9, "Asia/Hong_Kong": 8, "UTC": 0}
            tz = timezone(timedelta(hours=offsets.get(config.LIMIT_RESET_TZ, 9)))
        now = datetime.now(tz)
        next_day = now.date() + timedelta(days=1)
        return datetime.combine(next_day, dt_time.min, tzinfo=tz).timestamp()

    def _clear_expired_rate_limits(self):
        now = time.time()
        cur = self._conn.execute(
            "UPDATE accounts_meta SET rate_limited_until=0, limit_reason='', "
            "quota_blocked_until=0, quota_reason='' "
            "WHERE (rate_limited_until > 0 AND rate_limited_until <= ?) "
            "OR (quota_blocked_until > 0 AND quota_blocked_until <= ?)", (now, now))
        if cur.rowcount:
            self._conn.commit()

    def _mark_quota_blocked(self, account: str, reason: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=?, last_used_at=? WHERE name=?",
            (self._next_limit_reset(), reason[:300], time.time(), account),
        )
        self._conn.commit()

    def _mark_daily_limit(self, account: str, reason: str = ""):
        """Marks account as reaching daily limit until next reset."""
        self._ensure_meta(account)
        self._conn.execute(
            "INSERT INTO usage_by_instance(instance_id, day, used) VALUES (?,?,?) "
            "ON CONFLICT(instance_id, day) DO UPDATE SET used=MAX(used, excluded.used)",
            (self._meta(account)['instance_id'], date.today().isoformat(), DAILY_LIMIT),
        )
        self._conn.execute(
            "UPDATE accounts_meta SET last_used_at=?, rate_limited_until=?, limit_reason=? WHERE name=?",
            (time.time(), self._next_limit_reset(), reason[:300], account),
        )
        self._conn.commit()

    def list_accounts(self) -> list:
        """Dashboard view: combines metadata, quota, and busy status."""
        self._clear_expired_rate_limits()
        now = time.time()
        out = []
        for a in self.accounts:
            m = self._meta(a)
            used = self.used_today(a)
            lock = self._locks.get(a)
            out.append({
                "name": a,
                "scheduling": bool(m["scheduling"]) if m else True,
                "note": m["note"] if m else "",
                "email": m["email"] if m else "",
                "created_at": m["created_at"] if m else 0,
                "last_used_at": m["last_used_at"] if m else 0,
                "login_ok": (
                    None if not m or m["login_ok"] is None
                    else bool(m["login_ok"])
                ),
                "login_checked_at": m["login_checked_at"] if m else 0,
                "cooldown_until": m["cooldown_until"] if m else 0,
                "cooling": bool(m and m["cooldown_until"] > now),
                "rate_limited_until": m["rate_limited_until"] if m and m["rate_limited_until"] else 0,
                "rate_limited": bool(m and m["rate_limited_until"] > now),
                "limit_reason": m["limit_reason"] if m else "",
                "quota_blocked_until": m["quota_blocked_until"] if m and m["quota_blocked_until"] else 0,
                "quota_blocked": bool(m and m["quota_blocked_until"] > now),
                "quota_reason": m["quota_reason"] if m else "",
                "credit_balance": m["credit_balance"] if m else None,
                "credit_checked_at": m["credit_checked_at"] if m else 0,
                "used_today": used,
                "limit": DAILY_LIMIT,
                "remaining": max(0, DAILY_LIMIT - used),
                "busy": bool(lock and lock.locked()),
            })
        return out

    def set_scheduling(self, name: str, on: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET scheduling=? WHERE name=?", (1 if on else 0, name))
        self._conn.commit()
        self._wake_account_waiters()

    def set_email(self, name: str, email: str):
        self._conn.execute(
            "UPDATE accounts_meta SET email=? WHERE name=?", (email, name))
        self._conn.commit()

    def set_login_status(self, name: str, ok: bool):
        self._conn.execute(
            "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
            (1 if ok else 0, time.time(), name),
        )
        self._conn.commit()
        self._wake_account_waiters()

    def set_note(self, name: str, note: str):
        self._conn.execute(
            "UPDATE accounts_meta SET note=? WHERE name=?", (note, name))
        self._conn.commit()

    def delete_account(self, name: str):
        lock = self._locks.get(name)
        if lock and lock.locked():
            raise RuntimeError("Account is generating video, cannot delete")
        d = self.accounts_dir / name
        if d.exists():
            shutil.rmtree(d)
        self._conn.execute("DELETE FROM accounts_meta WHERE name=?", (name,))
        self._conn.commit()

    async def verify_account(self, name: str) -> bool:
        """Verifies login state in headless mode and updates cache."""
        if name not in self.accounts:
            raise FileNotFoundError(f"Profile does not exist: {name}")
        await self._reserve_specific_account(name, wait=False)
        try:
            from browser import check_login_state
            ok = await check_login_state(name)
            self._conn.execute(
                "UPDATE accounts_meta SET login_ok=?, login_checked_at=? WHERE name=?",
                (1 if ok else 0, time.time(), name),
            )
            self._conn.commit()
            return ok
        finally:
            await self._release_account(name)

    # ===== Scheduling =====

    def _set_credit_balance(self, account: str, balance: int, source: str = ""):
        self._conn.execute(
            "UPDATE accounts_meta SET credit_balance=?, credit_checked_at=? WHERE name=?",
            (max(0, int(balance)), time.time(), account),
        )
        if balance < 2:
            self._conn.execute(
                "UPDATE accounts_meta SET quota_blocked_until=?, quota_reason=? WHERE name=?",
                (self._next_limit_reset(), source[:300] or "Insufficient credits", account),
            )
        self._conn.commit()

    def _credit_available(self, account: str, required: int = 2) -> bool:
        row = self._meta(account)
        return not row or row["credit_balance"] is None or row["credit_balance"] >= required

    def _schedulable(self, a: dict) -> bool:
        return (a["scheduling"] and not a["cooling"] and not a["rate_limited"]
                and not a["quota_blocked"] and a["used_today"] < DAILY_LIMIT
                and a["login_ok"] is not False
                and (a["credit_balance"] is None or a["credit_balance"] >= 2))

    @property
    def all_accounts_limited(self) -> bool:
        """Returns True if all active accounts have reached daily limit."""
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["rate_limited"] or a["used_today"] >= DAILY_LIMIT for a in candidates
        )

    @property
    def all_accounts_quota_blocked(self) -> bool:
        candidates = [a for a in self.list_accounts() if a["scheduling"] and not a["cooling"]]
        return bool(candidates) and all(
            a["quota_blocked"] or a["rate_limited"] or a["used_today"] >= DAILY_LIMIT
            for a in candidates
        ) and any(a["quota_blocked"] for a in candidates)

    @property
    def available(self) -> bool:
        return any(self._schedulable(a) for a in self.list_accounts())

    @property
    def cookie_count(self) -> int:  # /health compatibility
        return len(self.accounts)

    def account_status(self) -> list:
        return [{
            "account": a["name"], "used_today": a["used_today"], "limit": a["limit"],
            "rate_limited": a["rate_limited"], "rate_limited_until": a["rate_limited_until"],
            "quota_blocked": a["quota_blocked"], "quota_blocked_until": a["quota_blocked_until"],
        } for a in self.list_accounts()]

    async def _notify_account_waiters(self):
        async with self._account_condition:
            self._account_condition.notify_all()

    def _wake_account_waiters(self):
        """Wake queued schedulers after a synchronous account state update."""
        try:
            asyncio.get_running_loop().create_task(self._notify_account_waiters())
        except RuntimeError:
            pass

    async def _reserve_specific_account(self, account: str,
                                        wait: bool = True) -> None:
        """Atomically reserves one profile so two Chromium instances never collide."""
        async with self._account_condition:
            lock = self._locks.setdefault(account, asyncio.Lock())
            while lock.locked():
                if not wait:
                    raise RuntimeError("Account is generating video, please verify later")
                await self._account_condition.wait()
            await lock.acquire()

    @staticmethod
    def _account_order(account: dict) -> tuple:
        """Balance daily quota first, then prefer the least recently used profile."""
        return (
            int(account.get("used_today") or 0),
            float(account.get("last_used_at") or 0),
            account["name"],
        )

    def _availability_error(self, accounts: list[dict],
                            target_account: str | None,
                            last_err: Exception | None) -> Exception | None:
        if target_account:
            target = next(
                (item for item in accounts if item["name"] == target_account), None)
            if not target:
                return RuntimeError(f"Target account '{target_account}' not found.")
            if not target["scheduling"]:
                return RuntimeError(
                    f"Target account '{target_account}' is disabled for scheduling.")
            if target["login_ok"] is False:
                return SessionExpiredError(
                    f"Target account '{target_account}' has an expired Dola session.")
            if (target["quota_blocked"] or target["rate_limited"]
                    or target["used_today"] >= DAILY_LIMIT):
                return AllAccountsLimitedError(
                    f"Target account '{target_account}' is blocked, rate-limited, "
                    "or out of points.")
            return None

        active = [item for item in accounts if item["scheduling"]]
        if not active:
            return RuntimeError(f"No active accounts in pool: {last_err or 'No accounts'}")
        if all(item["login_ok"] is False for item in active):
            return SessionExpiredError(
                f"All active Dola account sessions are expired: "
                f"{last_err or 'No authenticated accounts'}")

        candidates = [item for item in active if not item["cooling"]]
        if candidates and all(
                item["quota_blocked"] or item["rate_limited"]
                or item["used_today"] >= DAILY_LIMIT for item in candidates
        ) and any(item["quota_blocked"] for item in candidates):
            return AllAccountsQuotaBlockedError(
                f"429: All schedulable accounts have insufficient points: "
                f"{last_err or 'No accounts'}")
        if candidates and all(
                item["rate_limited"] or item["used_today"] >= DAILY_LIMIT
                for item in candidates):
            return AllAccountsLimitedError(
                f"429: All schedulable accounts have reached Dola daily limit: "
                f"{last_err or 'No accounts'}")
        return None

    async def _reserve_schedulable_account(
            self, target_account: str | None,
            last_err: Exception | None) -> str:
        """Waits efficiently for and atomically reserves the fairest idle profile."""
        while True:
            async with self._account_condition:
                accounts = self.list_accounts()
                eligible = [
                    item for item in accounts
                    if self._schedulable(item)
                    and (not target_account or item["name"] == target_account)
                ]
                for item in sorted(eligible, key=self._account_order):
                    lock = self._locks.setdefault(item["name"], asyncio.Lock())
                    if not lock.locked():
                        await lock.acquire()
                        return item["name"]

                error = self._availability_error(
                    accounts, target_account, last_err)
                if error:
                    raise error
                try:
                    await asyncio.wait_for(
                        self._account_condition.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    # Re-check timed cooldowns and externally changed metadata.
                    pass

    async def _release_account(self, account: str) -> None:
        async with self._account_condition:
            lock = self._locks.get(account)
            if lock and lock.locked():
                lock.release()
            self._account_condition.notify_all()

    async def resume_video(self, account: str, conversation_id: str, timeout: int,
                           duration: int | None = None, on_poll=None) -> dict:
        """Resumes an accepted session without re-scheduling."""
        await self._reserve_specific_account(account)
        try:
            async with self.semaphore:
                def on_balance(balance, source=""):
                    self._set_credit_balance(account, balance, source)
                try:
                    result = await resume_video(account, conversation_id, timeout,
                                                duration=duration,
                                                on_poll=on_poll, on_balance=on_balance)
                    self._claim(account)
                    self._conn.execute(
                        "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                        (time.time(), account))
                    self._conn.commit()
                    return result
                except TimeoutError:
                    self._claim(account)
                    self._conn.commit()
                    raise
        finally:
            await self._release_account(account)

    async def generate_video(self, prompt: str, ratio: str = None, duration: int = None,
                             model: str = "seedance_v2.0", on_conversation_id=None,
                             on_poll=None, on_balance=None,
                             reference_image_paths: list[str] | None = None,
                             target_account: str | None = None) -> dict:
        """Picks an idle schedulable account; automatically rotates on quota/risk limits."""
        last_err = None
        while True:
            account = await self._reserve_schedulable_account(
                target_account, last_err)
            try:
                async with self.semaphore:
                        try:
                            def on_balance(balance, source=""):
                                self._set_credit_balance(account, balance, source)

                            result = await generate_video(
                                account, prompt, ratio, duration, model=model,
                                use_extension=duration == 30,
                                on_conversation_id=on_conversation_id, on_poll=on_poll,
                                on_balance=on_balance, reference_image_paths=reference_image_paths)
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            return result
                        except CreditInsufficientError as e:
                            print(f"[pool] {account} insufficient points before generation, skipping: {e}", flush=True)
                            self._mark_quota_blocked(account, str(e))
                            last_err = e
                            continue
                        except AccountLimitedError as e:
                            print(f"[pool] {account} reached daily limit, rotating: {e}", flush=True)
                            self._mark_daily_limit(account, str(e))
                            last_err = e
                            continue
                        except CreditError as e:
                            print(f"[pool] {account} out of quota, rotating: {e}", flush=True)
                            self._claim(account)
                            last_err = e
                            continue
                        except RiskControlError as e:
                            print(f"[pool] {account} risk control triggered (30m cooldown), rotating: {e}", flush=True)
                            self._conn.execute(
                                "UPDATE accounts_meta SET cooldown_until=? WHERE name=?",
                                (time.time() + COOLDOWN_SEC, account))
                            self._conn.commit()
                            last_err = e
                            continue
                        except SessionExpiredError as e:
                            print(f"[pool] {account} session expired, rotating: {e}", flush=True)
                            self.set_login_status(account, False)
                            last_err = e
                            continue
                        except TimeoutError as e:
                            # Once conversation_id is assigned, task continues on Dola side;
                            # do not re-submit to prevent duplicate credit consumption.
                            self._claim(account)
                            self._conn.execute(
                                "UPDATE accounts_meta SET last_used_at=? WHERE name=?",
                                (time.time(), account))
                            self._conn.commit()
                            raise
                        except FileNotFoundError as e:
                            print(f"[pool] {account} profile missing, skipping: {e}", flush=True)
                            last_err = e
                            continue
            finally:
                await self._release_account(account)
