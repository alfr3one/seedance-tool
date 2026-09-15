"""Automated Google OAuth login for account profile setup.

Usage: python add_account.py <account_name> "email----password----totp_secret"
"""
import asyncio
import base64
import hashlib
import hmac
import re
import shutil
import struct
import sys
import time
from pathlib import Path

# Force UTF-8 encoding for standard output to avoid 'charmap' codec errors on Windows
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS
import config
from diagnostics import login_diagnostics, log_stage, safe_url


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """Standard TOTP (RFC 6238), Google Authenticator compatible."""
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


async def google_login(g, email: str, password: str, secret: str):
    """Executes Google OAuth login state machine until callback."""
    # Give user up to 5 minutes (120 steps * 2.5s) to solve any manual captchas
    for step in range(120):
        await g.wait_for_timeout(2500)
        if "accounts.google.com" not in g.url:
            print("[google] Redirected out of Google domain (OAuth callback)", flush=True)
            return
        # 1) Account chooser page
        if "accountchooser" in g.url:
            acc = g.locator(f"text={email}").first
            if await acc.count() and await acc.is_visible():
                await acc.click(timeout=5000)
                print("[google] Account chooser page -> clicked account", flush=True)
                continue
        # 2) Email page
        identifier = g.locator("#identifierId").first
        if await identifier.count() and await identifier.is_visible():
            await identifier.fill(email)
            # Use DOM click to safely trigger submit event
            await g.locator("#identifierNext").evaluate("e => e.click()")
            log_stage('google_email', 'Email submitted (value omitted)')
            await g.wait_for_timeout(1500)
            continue
        # 3) Password page
        password_input = g.locator("input[type='password']").first
        if await password_input.count() and await password_input.is_visible():
            await password_input.fill(password)
            await g.locator("#passwordNext").evaluate("e => e.click()")
            log_stage('google_password', 'Password submitted (value omitted)')
            await g.wait_for_timeout(2000)
            continue
        # 3.5) reCAPTCHA challenge (Bot detection)
        if await g.locator("iframe[src*='recaptcha']").count() > 0 or "challenge/recaptcha" in g.url:
            print("[google] reCAPTCHA detected! Please solve it MANUALLY in the opened browser window...", flush=True)
            # Wait up to 3 minutes for user to solve it manually
            for _ in range(60):
                await g.wait_for_timeout(3000)
                if "accounts.google.com" not in g.url or await g.locator("iframe[src*='recaptcha']").count() == 0:
                    print("[google] reCAPTCHA resolved manually, continuing...", flush=True)
                    break
            continue
        # 4) 2FA TOTP / Auth challenge page
        clicked = False
        for sel in ["#submit_button",
                    "[role='button']:has-text('続行')", "button:has-text('続行')",
                    "[role='button']:has-text('继续')", "button:has-text('继续')",
                    "[role='button']:has-text('Continue')", "button:has-text('Continue')",
                    "[role='button']:has-text('Tiếp tục')", "button:has-text('Tiếp tục')"]:
            try:
                loc = g.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    print(f"[google] Consent page -> clicked button", flush=True)
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            continue
        # 5) 2FA page
        if await g.locator('input[type="tel"]').count():
            if not secret:
                raise RuntimeError("Google requested 2FA, but no TOTP secret was provided")
            code = totp(secret)
            log_stage('google_2fa', 'OTP submitted (value omitted)')
            await g.fill('input[type="tel"]', code)
            try:
                await g.click("#totpNext", timeout=3000, force=True)
            except Exception:
                pass
            continue
        # 6) Dola 18+ age confirmation popup
        if await g.locator("text=18").count():
            ok = await g.evaluate("""() => {
                const els = [...document.querySelectorAll('button, [role="button"], div, span')];
                const t = els.find(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0);
                if (t) { t.click(); return true; }
                return false;
            }""")
            print(f"[dola] Age confirmation -> JS click OK = {ok}", flush=True)
            await g.wait_for_timeout(1500)
            continue
        txt = await g.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        log_stage('google_unrecognized', f'iteration={step} origin={safe_url(g.url)}')
    if "accounts.google.com" in g.url:
        await g.screenshot(path="dbg_google2.png")
        raise RuntimeError("Google login did not complete within 5 minutes (saved dbg_google2.png)")


@login_diagnostics
async def add_account_flow(account: str, email: str, password: str, secret: str) -> bool:
    """Full account addition flow; returns True on success."""
    profile_dir = Path("accounts") / account
    log_stage('profile_create')
    profile_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        kwargs = {"headless": False, "args": LAUNCH_ARGS,
                  "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        log_stage('chromium_launch', f'executable_exists={Path(p.chromium.executable_path).exists()} proxy={safe_url(config.PROXY) if config.PROXY else "none"}')
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        log_stage('chromium_opened')
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.on('crash', lambda _: log_stage('page_crashed', 'Chromium renderer crashed'))
            log_stage('dola_navigation', 'Opening Dola; navigation timeout 60s')
            await page.goto("https://www.dola.com/chat", timeout=60000)
            await page.wait_for_timeout(4000)

            cookies = await context.cookies("https://www.dola.com")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                print(f"[{account}] Active session exists, login not required", flush=True)
                return True

            # Click login entry -> Google option
            await page.wait_for_timeout(5000)
            # Dismiss any overlays (cookie consent, age check)
            await page.evaluate("""() => {
                const els = [...document.querySelectorAll('button, [role="button"], div, span')];
                els.filter(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0)
                   .forEach(e => e.click());
            }""")
            await page.wait_for_timeout(2000)
            # Click login button via JS to bypass any remaining overlay
            try:
                login_btn = None
                for text in ["ログイン", "Login", "Đăng nhập", "登录"]:
                    loc = page.get_by_text(text, exact=True).first
                    if await loc.count() and await loc.is_visible():
                        login_btn = loc
                        break
                if not login_btn:
                    login_btn = page.locator("button, a").filter(has_text=re.compile(r"ログイン|Login|Đăng nhập|登录")).first
                
                await login_btn.evaluate("e => { if(e) e.click(); }")
            except Exception:
                log_stage('login_button_missing', 'Initial login button click failed; trying Google option')
            await page.wait_for_timeout(5000)
            await page.screenshot(path="dbg_after_login_click.png")
            try:
                await page.wait_for_selector(".semi-modal-wrap", timeout=5000)
            except Exception:
                pass
            # Google OAuth opens in a popup window — listen for new page event before clicking
            google_page_future = asyncio.get_event_loop().create_future()

            def _on_page(new_page):
                if not google_page_future.done():
                    google_page_future.set_result(new_page)

            context.on("page", _on_page)
            await page.wait_for_timeout(500)

            # Click the Google button via JS to bypass overlay interception
            try:
                # Target specific text precisely to avoid clicking giant wrapper divs
                google_btn = None
                for text in ["Googleで続ける", "Continue with Google", "Tiếp tục với Google"]:
                    loc = page.get_by_text(text, exact=True).first
                    if await loc.count() and await loc.is_visible():
                        google_btn = loc
                        break
                
                # Fallback to regex if exact text fails
                if not google_btn:
                    google_btn = page.locator("button, div[role='button']").filter(
                        has_text=re.compile(r"Google")).first

                await google_btn.evaluate("e => { if(e) e.click(); }")
                log_stage('google_popup_wait', 'Google button clicked')
            except Exception:
                log_stage('google_button_missing', 'Google button click failed')

            # Wait up to 8s for Google popup to open
            g = None
            try:
                g = await asyncio.wait_for(google_page_future, timeout=8)
                await g.wait_for_load_state("domcontentloaded", timeout=10000)
                log_stage('google_popup_opened', safe_url(g.url))
            except asyncio.TimeoutError:
                pass
            finally:
                context.remove_listener("page", _on_page)

            # Fallback: scan existing pages / current tab
            if g is None or "google" not in g.url:
                await page.wait_for_timeout(2000)
                g = next((pg for pg in context.pages if "accounts.google.com" in pg.url), None)
                if g is None and "accounts.google.com" in page.url:
                    g = page

            if g is None:
                await page.screenshot(path="dbg_add_account.png")
                raise RuntimeError("Failed to redirect to Google login page (saved dbg_add_account.png)")


            await google_login(g, email, password, secret)
            log_stage('session_wait', 'Waiting for Dola session, timeout 180s')

            # Wait for Dola sessionid after OAuth callback
            for _ in range(60):
                await page.wait_for_timeout(3000)
                cookies = await context.cookies("https://www.dola.com")
                if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                    print(f"[{account}] [SUCCESS] Login successful, sessionid saved to {profile_dir}", flush=True)
                    await page.wait_for_timeout(3000)
                    return True
            await page.screenshot(path="dbg_add_account.png")
            raise RuntimeError("sessionid not acquired within 3 minutes (saved dbg_add_account.png)")
        finally:
            await context.close()


async def main():
    account = sys.argv[1]
    email, password, secret = sys.argv[2].split("----")
    await add_account_flow(account, email, password, secret)
    print(f"[{account}] Account added successfully!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
