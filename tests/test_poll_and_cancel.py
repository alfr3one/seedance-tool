import asyncio
import importlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
from video_worker_ui import poll_conversation


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_dola_refusal_stops_immediately(self):
        page=MagicMock(evaluate=AsyncMock(return_value={'ok':True,'texts':['質問に回答できません。'],'videos':[]}))
        with patch('video_worker_ui.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,'Dola declined'):
                await poll_conversation('acc-test',page,MagicMock(cookies=AsyncMock(return_value=[])),'123',900)
        self.assertEqual(page.evaluate.await_count,1)

    async def test_repeated_http_errors_stop_without_resubmission(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={'ok': False, 'status': 403})
        context = MagicMock(cookies=AsyncMock(return_value=[]))
        callback = MagicMock()
        with patch('video_worker_ui.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'Polling unavailable'):
                await poll_conversation('acc-test', page, context, '123', 900, callback)
        self.assertEqual(page.evaluate.await_count, 6)
        self.assertIn('403', callback.call_args.args[1])

    async def test_transport_exception_is_not_generating(self):
        page = MagicMock(evaluate=AsyncMock(side_effect=ValueError('network')))
        with patch('video_worker_ui.asyncio.sleep', new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'ValueError'):
                await poll_conversation('acc-test', page, MagicMock(cookies=AsyncMock(return_value=[])), '123', 900)

    async def test_vietnamese_content_refusal_stops(self):
        text='Tôi tạm thời không thể tạo nội dung video bạn yêu cầu do bộ lọc an toàn nội dung.'
        page=MagicMock(evaluate=AsyncMock(return_value={'ok':True,'texts':[text],'videos':[]}))
        with patch('video_worker_ui.asyncio.sleep',new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError,'Dola declined'):
                await poll_conversation('acc-test',page,MagicMock(cookies=AsyncMock(return_value=[])),'123',900)

    async def test_success_after_transient_error(self):
        page = MagicMock(evaluate=AsyncMock(side_effect=[{'ok':False,'status':503},
            {'ok':True,'videos':['https://example.com/video.mp4'],'texts':[]}]))
        file = MagicMock()
        file.stat.return_value.st_size = 1024
        with patch('video_worker_ui.asyncio.sleep',new=AsyncMock()), patch('video_worker_ui._download',new=AsyncMock(return_value=file)):
            result = await poll_conversation('acc-test',page,MagicMock(cookies=AsyncMock(return_value=[])),'123',900)
        self.assertEqual(result['conversation_id'],'123')


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cwd = Path.cwd()
        try:
            os.chdir(cls.temp.name)
            with patch.object(config,'DB_PATH',str(Path(cls.temp.name)/'tasks.db')), patch.object(config,'DOWNLOAD_DIR',str(Path(cls.temp.name)/'downloads')):
                cls.server = importlib.import_module('server')
        finally:
            os.chdir(cwd)

    @classmethod
    def tearDownClass(cls):
        cls.server.pool._conn.close()
        cls.server.store._conn.close()
        cls.temp.cleanup()

    async def test_cancel_stops_worker_and_releases_finally(self):
        server = self.server
        entered, released = asyncio.Event(), asyncio.Event()
        async def work():
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                released.set()
        task = server.schedule_task('test-cancel',work())
        await entered.wait()
        store = MagicMock()
        store.get.return_value = {'status':'processing'}
        with patch.object(server,'store',store),patch.object(server,'_admin_auth'):
            await server.admin_task_cancel('test-cancel')
        self.assertTrue(task.cancelled())
        self.assertTrue(released.is_set())
        self.assertEqual(store.update.call_args.kwargs['failure_code'],'cancelled')

    async def test_duplicate_runner_is_rejected(self):
        server = self.server
        async def work():
            await asyncio.Event().wait()
        task = server.schedule_task('duplicate',work())
        try:
            with self.assertRaises(server.HTTPException):
                server.schedule_task('duplicate',work())
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task


if __name__ == '__main__':
    unittest.main()
