import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request


class DesktopBackendTests(unittest.TestCase):
    def test_start_stop_restart_and_occupied_port(self):
        entry = Path(__file__).resolve().parents[1] / 'desktop_backend.py'
        with tempfile.TemporaryDirectory() as data:
            port = None
            for _ in range(2):
                proc = subprocess.Popen([sys.executable, str(entry), '--data-dir', data],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, env={**os.environ, 'PYTHONIOENCODING':'utf-8'})
                try:
                    # subprocess watchdog prevents a stalled startup hanging the suite.
                    import threading
                    watchdog = threading.Timer(30, proc.kill)
                    watchdog.start()
                    line = proc.stdout.readline()
                    self.assertTrue(line.startswith('DOLA_DESKTOP_READY '), line)
                    current = json.loads(line.split(' ', 1)[1])['port']
                    if port is not None:
                        self.assertEqual(port, current)
                    port = current
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/') as response:
                        self.assertEqual(response.status, 200)
                    proc.communicate('shutdown\n', timeout=30)
                    self.assertEqual(proc.returncode, 0)
                finally:
                    watchdog.cancel()
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', port))
                listener.listen()
                result = subprocess.run([sys.executable, str(entry), '--data-dir', data],
                    input='', capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('occupied', result.stderr)


if __name__ == '__main__':
    unittest.main()
