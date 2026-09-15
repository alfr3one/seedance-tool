"""Proxy validation and isolated browser connectivity probe (no account login)."""
import asyncio
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from diagnostics import redact


def validate_proxy(value):
    value = value.strip()
    if not value:
        return ''
    if any(c.isspace() or c in '\"\'\\#' for c in value):
        raise ValueError('Proxy chứa ký tự không hợp lệ')
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https', 'socks5') or not parsed.hostname
                or not parsed.port or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError()
    except ValueError:
        raise ValueError('Nhập http://host:port hoặc socks5://host:port; chưa hỗ trợ user/password') from None
    return value.rstrip('/')


async def check_proxy(value):
    async def probe():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, **({'proxy': {'server': value}} if value else {}))
            try:
                page = await browser.new_page()
                response = await page.goto('https://www.dola.com/chat', wait_until='domcontentloaded', timeout=15000)
                status = response.status if response else 0
                return {'ok': 200 <= status < 400, 'message': f'Dola HTTP {status}. Kiểm tra kết nối, không xác minh đăng nhập hay quota.'}
            finally:
                await browser.close()
    try:
        return await asyncio.wait_for(probe(), timeout=25)
    except Exception as exc:
        return {'ok': False, 'message': redact(f'{type(exc).__name__}: {exc}')[:600]}
