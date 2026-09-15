import logging
import os
from pathlib import Path
import tempfile
import unittest
from diagnostics import login_diagnostics, log_stage, redact


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def test_redacts_credentials_and_url_queries(self):
        message = redact('pw-secret otp=123456 user@example.com https://u:pw@host.test/a?token=abc', ('pw-secret',))
        for value in ('pw-secret','123456','user@example.com','token=abc','u:pw'):
            self.assertNotIn(value,message)
        self.assertIn('https://host.test',message)

    async def test_launch_failure_is_persistent_and_sanitized(self):
        @login_diagnostics
        async def failing(account,email,password,secret):
            log_stage('chromium_launch','proxy=http://127.0.0.1:7890')
            raise RuntimeError(f'net::ERR_PROXY_CONNECTION_FAILED {password} {email} {secret}')
        with tempfile.TemporaryDirectory() as directory:
            old=Path.cwd()
            try:
                os.chdir(directory)
                with self.assertRaises(RuntimeError):
                    await failing('acc-test','test@example.com','password-secret','totp-secret')
                file=Path('logs/account-login.log').resolve()
                content=file.read_text(encoding='utf-8')
                self.assertIn('failed_stage=chromium_launch',content)
                self.assertIn('ERR_PROXY_CONNECTION_FAILED',content)
                self.assertIn('account=acc-test',content)
                for value in ('password-secret','totp-secret','test@example.com'):
                    self.assertNotIn(value,content)
                logger=logging.getLogger(str(file))
                for handler in logger.handlers[:]:
                    handler.close();logger.removeHandler(handler)
            finally:
                os.chdir(old)

if __name__=='__main__':
    unittest.main()
