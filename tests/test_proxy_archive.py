from pathlib import Path
import tempfile
import unittest
import zipfile
from proxy_settings import validate_proxy
from video_archive import build_video_archive
from store import TaskStore


class ProxyArchiveTests(unittest.TestCase):
    def test_proxy_validation(self):
        for value in ('', 'http://127.0.0.1:7890', 'socks5://localhost:1080'):
            self.assertEqual(validate_proxy(value),value)
        for value in ('null','http://a:80\nDOLA_ADMIN_KEY=x','http://u:p@a:80','http://a:0','file:///x','http://a:80?x=y'):
            with self.assertRaises(ValueError):validate_proxy(value)

    def test_latest_completed_selection_and_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            store=TaskStore(str(Path(directory)/'tasks.db'))
            try:
                for index,status in enumerate(('completed','completed','failed','processing')):
                    store.create(str(index),'seedance-2.5','prompt','9:16',15,account='acc1')
                    store.update(str(index),status=status,finished_at=index,video_url=f'http://localhost/videos/{index}.mp4')
                rows=store.latest_completed_videos()
                self.assertEqual([row['id'] for row in rows],['1'])
                (Path(directory)/'1.mp4').write_bytes(b'test-video')
                archive,count,skipped=build_video_archive(rows+[{'video_url':'http://localhost/videos/missing.mp4'}],directory)
                try:
                    self.assertEqual((count,skipped),(1,1))
                    with zipfile.ZipFile(archive) as z:
                        self.assertEqual(z.namelist(),['1.mp4'])
                        self.assertEqual(z.read('1.mp4'),b'test-video')
                finally:archive.unlink()
                with self.assertRaises(ValueError):build_video_archive([],directory)
            finally:store._conn.close()
