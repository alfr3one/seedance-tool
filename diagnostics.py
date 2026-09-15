"""Local, rotated login diagnostics. Never records cookies or page content."""
import asyncio
from contextvars import ContextVar
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import traceback
from urllib.parse import urlsplit

_account = ContextVar('diagnostic_account', default='unknown')
_stage = ContextVar('diagnostic_stage', default='start')
_secrets = ContextVar('diagnostic_secrets', default=())


def safe_url(value):
    try:
        url = urlsplit(value)
        return f'{url.scheme}://{url.hostname or ""}' + (f':{url.port}' if url.port else '')
    except ValueError:
        return '[invalid URL]'


def redact(text, secrets=()):
    text = str(text)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'https?://[^\s<>"\']+', lambda m: safe_url(m.group()), text)
    text = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    text = re.sub(r'\b\d{6}\b', '[CODE]', text)
    text = re.sub(r'(?i)\b(password|cookie|authorization|sessionid|msToken|totp|otp)\s*[:=]\s*[^\s,;]+', r'\1=[REDACTED]', text)
    return text


def log_stage(stage, detail=''):
    _stage.set(stage)
    path = Path('logs/account-login.log').resolve()
    logger = logging.getLogger(str(path))
    if not logger.handlers:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    message = redact(f'account={_account.get()} stage={stage} {detail}', _secrets.get())
    logger.info(message)
    print(message, flush=True)


def login_diagnostics(function):
    @wraps(function)
    async def wrapped(account, email, password, secret):
        tokens = (_account.set(account), _stage.set('start'), _secrets.set((email,password,secret)))
        try:
            log_stage('start', 'Login started')
            result = await function(account,email,password,secret)
            log_stage('success', 'Login completed')
            return result
        except BaseException as exc:
            failed_stage = _stage.get()
            kind = 'cancelled' if isinstance(exc,asyncio.CancelledError) else 'failed'
            log_stage(kind, f'failed_stage={failed_stage}; {type(exc).__name__}: {exc}\n'+''.join(traceback.format_tb(exc.__traceback__)))
            raise
        finally:
            _account.reset(tokens[0]); _stage.reset(tokens[1]); _secrets.reset(tokens[2])
    return wrapped
