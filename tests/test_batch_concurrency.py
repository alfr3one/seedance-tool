import asyncio
import tempfile
import sqlite3
from datetime import date
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from browser_pool import BrowserPool
from store import TaskStore
from video_worker_ui import SessionExpiredError, SubmissionUnverifiedError


class BatchConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_readded_name_gets_new_counter_and_preserves_old_history(self):
        with tempfile.TemporaryDirectory() as root:
            accounts = Path(root) / 'accounts'
            (accounts / 'acc3').mkdir(parents=True)
            db = str(Path(root) / 'pool.db')
            pool = BrowserPool(str(accounts), db)
            pool._claim('acc3')
            old_id = pool._meta('acc3')['instance_id']
            pool.delete_account('acc3')
            (accounts / 'acc3').mkdir()
            self.assertIn('acc3', pool.accounts)
            self.assertNotEqual(old_id, pool._meta('acc3')['instance_id'])
            self.assertEqual(pool.used_today('acc3'), 0)
            self.assertEqual(pool._conn.execute(
                'SELECT used FROM usage_by_instance WHERE instance_id=?', (old_id,)
            ).fetchone()[0], 1)
            pool._claim('acc3')
            pool._conn.close()
            pool = BrowserPool(str(accounts), db)
            self.assertEqual(pool.used_today('acc3'), 1)
            pool._conn.close()

    async def test_legacy_migration_preserves_existing_not_orphan_counters(self):
        with tempfile.TemporaryDirectory() as root:
            accounts = Path(root) / 'accounts'
            (accounts / 'existing').mkdir(parents=True)
            (accounts / 'reused').mkdir()
            db = str(Path(root) / 'pool.db')
            conn = sqlite3.connect(db)
            conn.execute('CREATE TABLE usage(account TEXT, day TEXT, used INTEGER, PRIMARY KEY(account,day))')
            conn.execute('CREATE TABLE accounts_meta(name TEXT PRIMARY KEY, created_at REAL)')
            conn.execute('INSERT INTO accounts_meta VALUES (?,?)', ('existing', time.time()))
            conn.executemany('INSERT INTO usage VALUES (?,?,?)', [
                ('existing', date.today().isoformat(), 4),
                ('reused', date.today().isoformat(), 10)])
            conn.commit(); conn.close()
            pool = BrowserPool(str(accounts), db)
            self.assertEqual(pool.used_today('existing'), 4)
            self.assertEqual(pool.used_today('reused'), 0)
            pool._conn.commit(); pool._conn.close()

    async def test_unverified_submission_does_not_claim_usage_or_retry(self):
        with tempfile.TemporaryDirectory() as root:
            accounts_dir = Path(root) / 'accounts'
            (accounts_dir / 'acc6').mkdir(parents=True)
            pool = BrowserPool(str(accounts_dir), str(Path(root) / 'pool.db'), 2)
            try:
                with patch('browser_pool.generate_video',
                           side_effect=SubmissionUnverifiedError('unverified')) as worker:
                    with self.assertRaises(SubmissionUnverifiedError):
                        await pool.generate_video('scene', duration=15, target_account='acc6')
                    self.assertEqual(worker.call_count, 1)
                    self.assertEqual(pool.used_today('acc6'), 0)
                    self.assertFalse(pool._locks['acc6'].locked())
            finally:
                pool._conn.close()

    async def test_ten_target_profiles_run_concurrently(self):
        with tempfile.TemporaryDirectory() as root:
            accounts_dir = Path(root) / "accounts"
            accounts_dir.mkdir()
            account_names = [f"acc{i}" for i in range(1, 11)]
            for name in account_names:
                (accounts_dir / name).mkdir()

            pool = BrowserPool(
                accounts_dir=str(accounts_dir),
                db_path=str(Path(root) / "pool.db"),
                max_concurrency=10,
            )
            active = 0
            peak_active = 0
            active_lock = asyncio.Lock()

            async def fake_generate(account, *args, **kwargs):
                nonlocal active, peak_active
                async with active_lock:
                    active += 1
                    peak_active = max(peak_active, active)
                await asyncio.sleep(0.05)
                async with active_lock:
                    active -= 1
                return {"account": account, "local_path": f"{account}.mp4"}

            try:
                with patch("browser_pool.generate_video", side_effect=fake_generate):
                    results = await asyncio.gather(*[
                        pool.generate_video(
                            prompt=f"video {name}",
                            ratio="9:16",
                            duration=10,
                            model="seedance-2.5",
                            target_account=name,
                        )
                        for name in account_names
                    ])
            finally:
                pool._conn.close()

            self.assertEqual(peak_active, 10)
            self.assertEqual({result["account"] for result in results}, set(account_names))

    async def test_target_profile_is_persisted_for_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(str(Path(root) / "tasks.db"))
            try:
                store.create(
                    "task-1",
                    "seedance-2.5",
                    "prompt",
                    "9:16",
                    15,
                    target_account="acc10",
                )
                recovered = store.recoverable_queued_tasks()
            finally:
                store._conn.close()

            self.assertEqual(recovered[0]["target_account"], "acc10")

    async def test_expired_session_rotates_to_next_account(self):
        with tempfile.TemporaryDirectory() as root:
            accounts_dir = Path(root) / "accounts"
            accounts_dir.mkdir()
            for name in ("acc1", "acc2"):
                (accounts_dir / name).mkdir()

            pool = BrowserPool(
                accounts_dir=str(accounts_dir),
                db_path=str(Path(root) / "pool.db"),
                max_concurrency=2,
            )
            attempted = []

            async def fake_generate(account, *args, **kwargs):
                attempted.append(account)
                if account == "acc1":
                    raise SessionExpiredError("expired")
                return {"account": account, "local_path": f"{account}.mp4"}

            try:
                with patch("browser_pool.generate_video", side_effect=fake_generate):
                    result = await pool.generate_video(prompt="video", duration=10)
                account_states = {item["name"]: item for item in pool.list_accounts()}
            finally:
                pool._conn.close()

            self.assertEqual(attempted, ["acc1", "acc2"])
            self.assertEqual(result["account"], "acc2")
            self.assertFalse(account_states["acc1"]["login_ok"])

    async def test_extension_is_only_enabled_for_thirty_seconds(self):
        with tempfile.TemporaryDirectory() as root:
            accounts_dir = Path(root) / "accounts"
            accounts_dir.mkdir()
            (accounts_dir / "acc1").mkdir()
            pool = BrowserPool(
                accounts_dir=str(accounts_dir),
                db_path=str(Path(root) / "pool.db"),
                max_concurrency=1,
            )
            extension_modes = []

            async def fake_generate(account, *args, **kwargs):
                extension_modes.append(kwargs.get("use_extension"))
                return {"account": account, "local_path": f"{account}.mp4"}

            try:
                with patch("browser_pool.generate_video", side_effect=fake_generate):
                    await pool.generate_video(prompt="15s", duration=15)
                    await pool.generate_video(prompt="30s", duration=30)
            finally:
                pool._conn.close()

            self.assertEqual(extension_modes, [False, True])

    async def test_same_profile_queue_wakes_without_five_second_poll(self):
        with tempfile.TemporaryDirectory() as root:
            accounts_dir = Path(root) / "accounts"
            accounts_dir.mkdir()
            (accounts_dir / "acc1").mkdir()
            pool = BrowserPool(
                accounts_dir=str(accounts_dir),
                db_path=str(Path(root) / "pool.db"),
                max_concurrency=2,
            )
            active = 0
            peak_active = 0

            async def fake_generate(account, *args, **kwargs):
                nonlocal active, peak_active
                active += 1
                peak_active = max(peak_active, active)
                await asyncio.sleep(0.05)
                active -= 1
                return {"account": account, "local_path": f"{account}.mp4"}

            started = time.monotonic()
            try:
                with patch("browser_pool.generate_video", side_effect=fake_generate):
                    await asyncio.gather(
                        pool.generate_video(
                            prompt="first", duration=15, target_account="acc1"),
                        pool.generate_video(
                            prompt="second", duration=15, target_account="acc1"),
                    )
            finally:
                pool._conn.close()

            self.assertEqual(peak_active, 1)
            self.assertLess(time.monotonic() - started, 0.5)

    async def test_untargeted_tasks_balance_across_idle_profiles(self):
        with tempfile.TemporaryDirectory() as root:
            accounts_dir = Path(root) / "accounts"
            accounts_dir.mkdir()
            for name in ("acc1", "acc2"):
                (accounts_dir / name).mkdir()
            pool = BrowserPool(
                accounts_dir=str(accounts_dir),
                db_path=str(Path(root) / "pool.db"),
                max_concurrency=2,
            )
            pool._claim("acc1")
            selected = []

            async def fake_generate(account, *args, **kwargs):
                selected.append(account)
                return {"account": account, "local_path": f"{account}.mp4"}

            try:
                with patch("browser_pool.generate_video", side_effect=fake_generate):
                    await pool.generate_video(prompt="video", duration=15)
            finally:
                pool._conn.close()

            self.assertEqual(selected, ["acc2"])


if __name__ == "__main__":
    unittest.main()
