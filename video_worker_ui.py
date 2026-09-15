"""Video generation worker v2: UI automation with OpenCV slider puzzle solver."""
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from patchright.async_api import async_playwright

from gap import find_gap_x

import config
from browser import cookie_value, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url

# Daily limit pattern matching response text
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1\s*日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day|"
    # Zero credit remaining messages (all languages)
    r"0\s*動画クレジットが残っています|"
    r"0\s*video\s*credit|"
    r"还剩\s*0\s*(?:视频|影片)",
    re.IGNORECASE,
)


class AccountLimitedError(Exception):
    """Account reached daily video generation limit."""


class CreditInsufficientError(Exception):
    """Insufficient points prior to generation."""


class SessionExpiredError(Exception):
    """The persistent Dola profile no longer has an authenticated session."""


class SubmissionUnverifiedError(RuntimeError):
    """Submission outcome is unknown; never automatically submit it again."""


VIDEO_BTN = "text=動画を作成"          # Entry point button in ja-JP locale
CAPTCHA_FRAME_KEY = "bdcaptcha.html"   # Captcha verifycenter iframe
VIDEO_SKILL_PATTERN = re.compile(
    r"^(?:create\s*videos?|tạo\s*videos?|動画を作成|ビデオを作成)$", re.IGNORECASE
)

DOLA_INTERNAL_RETRY_PATTERN = re.compile(
    r"empty\s+task[_\s-]*id|task[_\s-]*id\s+(?:is\s+)?(?:empty|missing)|"
    r"retrying\s+(?:text[-\s]to[-\s]video|video generation)|"
    r"keyframe\s+image.*image[-\s]to[-\s]video",
    re.IGNORECASE,
)
SKILLS_MENU_PATTERN = re.compile(
    r"(?:skills?|kỹ\s*năng|スキル|技能)", re.IGNORECASE
)


def _conversation_id_from_url(url: str) -> str:
    """Returns only a server-issued numeric conversation id, never local_* ids."""
    try:
        tail = urlparse(url).path.rstrip("/").split("/")[-1]
    except (TypeError, ValueError):
        return ""
    return tail if tail.isdigit() else ""


def _debug_path(prefix: str, account: str) -> str:
    safe_account = re.sub(r"[^A-Za-z0-9_.-]+", "_", account or "unknown")
    return f"{prefix}_{safe_account}_{int(time.time())}.png"


async def _first_visible(locator):
    """Returns the first visible item from a locator collection."""
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


async def _find_active_editor(page):
    """Find the lowest visible composer editor while excluding navigation UI."""
    candidates = page.locator(
        '.tiptap.ProseMirror, .ProseMirror, div[contenteditable="true"], '
        'textarea, [role="textbox"]'
    )
    viewport_height = await page.evaluate("() => window.innerHeight")
    ranked = []
    for index in range(min(await candidates.count(), 30)):
        candidate = candidates.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            excluded = await candidate.evaluate(
                "el => !!el.closest('header, nav, aside, [class*=sidebar], [class*=conversation-item]')"
            )
            box = await candidate.bounding_box()
            if excluded or not box or box["width"] < 80 or box["height"] < 15:
                continue
            if box["y"] < viewport_height * 0.30:
                continue
            ranked.append((box["y"], candidate))
        except Exception:
            continue
    if not ranked:
        raise RuntimeError("Dola composer editor not found")
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


async def _editor_text(editor) -> str:
    try:
        tag_name = await editor.evaluate("el => el.tagName")
        if tag_name in ("TEXTAREA", "INPUT"):
            return await editor.input_value()
        return await editor.inner_text()
    except Exception:
        return ""


async def _find_send_button(page):
    exact = page.locator(
        '#flow-end-msg-send, button[class*="send-msg-btn"], [class*="send-msg-btn"], '
        '.send-btn-wrapper button, [data-testid*="send" i], '
        'button[aria-label*="send" i], button[aria-label*="gửi" i], '
        'button[id*="msg-send"]'
    )
    return await _first_visible(exact)


def _normalized_editor_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


async def _submission_acknowledged(page, editor, expected: str,
                                   initial_url: str, send_button=None) -> bool:
    """Detect the immediate UI transition caused by a successful submit."""
    if page.url != initial_url:
        return True
    if _normalized_editor_text(await _editor_text(editor)) != expected:
        return True
    if send_button is not None:
        try:
            if not await send_button.is_enabled() \
                    or await send_button.get_attribute("aria-disabled") == "true":
                return True
        except Exception:
            pass
    return False


async def _wait_for_submission_ack(page, editor, expected: str,
                                   initial_url: str, send_button=None,
                                   timeout: float = 4.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await _submission_acknowledged(
                page, editor, expected, initial_url, send_button):
            return True
        await page.wait_for_timeout(100)
    return False


async def _insert_and_submit_prompt(page, prompt: str,
                                    account: str = "unknown") -> None:
    """Insert with native input events and require proof that Dola accepted submit."""
    # The bundled media panel overlaps Dola's send button. It can remain active
    # while hidden; this prevents it from intercepting the trusted fallback click.
    await page.evaluate("""() => {
        const panel = document.getElementById('watermark-free-media-panel');
        if (panel) panel.style.setProperty('display', 'none', 'important');
    }""")

    editor = await _find_active_editor(page)
    await editor.click(force=True)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await page.keyboard.insert_text(prompt)

    expected = _normalized_editor_text(prompt)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        actual = _normalized_editor_text(await _editor_text(editor))
        if actual == expected:
            break
        await page.wait_for_timeout(100)
    else:
        raise RuntimeError("Prompt insertion was not acknowledged by the Dola editor")

    send_button = None
    send_deadline = time.monotonic() + 3
    while time.monotonic() < send_deadline:
        send_button = await _find_send_button(page)
        if send_button:
            try:
                if await send_button.is_enabled() and await send_button.get_attribute("aria-disabled") != "true":
                    break
            except Exception:
                pass
        await page.wait_for_timeout(100)
    else:
        send_button = None

    initial_url = page.url

    # Enter is the primary path: this mirrors a real keyboard submission and is
    # handled directly by Dola's ProseMirror/React composer.
    await editor.press("Enter")
    if await _wait_for_submission_ack(
            page, editor, expected, initial_url, send_button, timeout=3):
        return

    # If Enter merely inserted a line break, the normalized text is unchanged.
    # Use the actual Dola send button once, then require an observable UI change.
    send_button = await _find_send_button(page)
    if send_button is None:
        screenshot = _debug_path("submit_no_button", account)
        await page.screenshot(path=screenshot)
        raise RuntimeError(
            f"Dola did not acknowledge Enter and no send button was found; "
            f"prompt_chars={len(prompt)}; screenshot={screenshot}"
        )
    if not await send_button.is_enabled() \
            or await send_button.get_attribute("aria-disabled") == "true":
        screenshot = _debug_path("submit_button_disabled", account)
        await page.screenshot(path=screenshot)
        raise RuntimeError(
            f"Dola send button remained disabled after prompt insertion; "
            f"prompt_chars={len(prompt)}; screenshot={screenshot}"
        )

    await send_button.click()
    if await _wait_for_submission_ack(
            page, editor, expected, initial_url, send_button, timeout=5):
        return

    screenshot = _debug_path("submit_not_acknowledged", account)
    await page.screenshot(path=screenshot)
    raise RuntimeError(
        f"Dola did not acknowledge Enter or send-button click; "
        f"prompt_chars={len(prompt)}; screenshot={screenshot}"
    )


async def _find_model_selector(page):
    explicit = await _first_visible(page.locator(
        '[data-valid-btn="mode-select-action-btn"], [data-testid*="mode-select"]'
    ))
    if explicit is not None:
        return explicit
    buttons = page.locator('button, [role="button"], .semi-button')
    viewport_height = await page.evaluate("() => window.innerHeight")
    for index in range(min(await buttons.count(), 250)):
        candidate = buttons.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            if await candidate.evaluate("el => !!el.closest('aside, nav, [class*=sidebar]')"):
                continue
            text = (await candidate.inner_text()).strip()
            box = await candidate.bounding_box()
            if box and box['y'] > viewport_height * 0.30 and re.fullmatch(
                    r'(?:高速|プロ|Fast|Pro|Nhanh|2\.0|2\.5)', text, re.I):
                return candidate
        except Exception:
            continue
    return None


async def _select_model(page, model_key: str) -> None:
    desired = (re.compile(r"(?:プロ|\bpro\b|2\.5)", re.I)
               if model_key == "seedance_v2.5"
               else re.compile(r"(?:高速|\bfast\b|2\.0)", re.I))
    model_button = None
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        model_button = await _find_model_selector(page)
        if model_button is not None:
            break
        await page.wait_for_timeout(200)
    if not model_button:
        screenshot = _debug_path('model_selector_missing', 'ui')
        try:
            await page.screenshot(path=screenshot)
        except Exception:
            screenshot = 'unavailable'
        raise RuntimeError(f"Dola model selector not found within 15s; url={page.url}; screenshot={screenshot}")
    current = (await model_button.inner_text()).strip()
    if desired.search(current):
        # Even when Pro already appears selected, give React's tool context a
        # short settling window before the prompt is submitted.
        await page.wait_for_timeout(750)
        return

    menu_items = page.locator(
        '[role="menuitem"], [role="option"], [data-slot="dropdown-menu-item"], '
        '.semi-dropdown-item, [class*="dropdown-item"]'
    )
    target = None
    # React can render the menu after the old fixed 500ms scan, especially
    # when several profiles start together. Resolve fresh nodes on each pass.
    for attempt in range(3):
        model_button = await _find_model_selector(page)
        if model_button is None:
            await page.wait_for_timeout(200)
            continue
        if desired.search((await model_button.inner_text()).strip()):
            return
        await model_button.click(timeout=3000)
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            target = await _find_button_by_text(menu_items, desired, fullmatch=False)
            if target is not None:
                break
            await page.wait_for_timeout(150)
        if target is not None:
            break
        await page.keyboard.press('Escape')
        await page.wait_for_timeout(200)
    if not target:
        screenshot = _debug_path('model_option_missing', 'ui')
        try:
            await page.screenshot(path=screenshot)
        except Exception:
            screenshot = 'unavailable'
        await page.keyboard.press("Escape")
        raise RuntimeError(f"Requested Dola model option not found: {model_key}; url={page.url}; screenshot={screenshot}")
    await target.click(timeout=3000)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            model_button = await _find_model_selector(page)
            if desired.search((await model_button.inner_text()).strip()):
                await page.wait_for_timeout(750)
                return
        except Exception:
            pass
        await page.wait_for_timeout(150)
    raise RuntimeError(f"Dola model did not stabilize after selection: {model_key}")


async def _video_skill_active(page) -> bool:
    controls = page.locator(
        '[data-input-engine-actionbar-control-key="video-duration"], '
        '[data-input-engine-actionbar-control-key="video-ratio"], '
        '[data-input-engine-actionbar-control-key="video-model"]'
    )
    return bool(await controls.count()) and bool(await _first_visible(controls))


async def _ensure_normal_chat_mode(page) -> None:
    """Exit Create Videos because 15s generation is driven from Pro chat."""
    if not await _video_skill_active(page):
        return
    exit_button = await _first_visible(page.locator(
        '[class*="exit-skill"], .bg-g-exit-skill-btn-bg, '
        '[data-testid*="exit-skill"], [class*="exit-btn"]'
    ))
    if exit_button is None:
        raise RuntimeError("Create Videos is active but its exit control was not found")
    await exit_button.click(force=True)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if not await _video_skill_active(page):
            return
        await page.wait_for_timeout(150)
    raise RuntimeError("Could not return Dola to normal chat mode")


async def _visible_video_controls(page) -> list[str]:
    """Return a small diagnostic snapshot without dumping the whole page."""
    labels: list[str] = []
    controls = page.locator(
        'button[data-component-type="skill-item"], button, [role="button"], '
        '[role="menuitem"], [role="option"]'
    )
    for index in range(min(await controls.count(), 250)):
        candidate = controls.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            label = re.sub(r"\s+", " ", (await candidate.inner_text())).strip()
            if label and (VIDEO_SKILL_PATTERN.search(label) or SKILLS_MENU_PATTERN.search(label)):
                if label not in labels:
                    labels.append(label[:120])
        except Exception:
            continue
        if len(labels) >= 12:
            break
    return labels


async def _enter_video_skill(page, account: str = "unknown") -> None:
    """Enter Create Videos, tolerating delayed UI hydration under concurrency."""
    if await _video_skill_active(page):
        return

    deadline = time.monotonic() + 15
    last_click_at = 0.0
    skills_menu_opened = False
    while time.monotonic() < deadline:
        if await _video_skill_active(page):
            return

        # Dola hydrates the composer asynchronously. During parallel browser
        # startup the skill item can be absent for several seconds, so poll the
        # current DOM instead of treating the first empty scan as a hard error.
        now = time.monotonic()
        if now - last_click_at >= 2.5:
            candidates = page.locator(
                'button[data-component-type="skill-item"], button, [role="button"]'
            )
            target = await _find_button_by_text(candidates, VIDEO_SKILL_PATTERN)

            if target is None and not skills_menu_opened:
                skills_button = await _find_button_by_text(
                    candidates, SKILLS_MENU_PATTERN, fullmatch=False
                )
                if skills_button is not None:
                    await skills_button.click(force=True)
                    skills_menu_opened = True
                    await page.wait_for_timeout(300)

            if target is None and skills_menu_opened:
                menu_candidates = page.locator(
                    '[role="menuitem"], [role="option"], '
                    '[data-slot="dropdown-menu-item"], .semi-dropdown-item, button'
                )
                target = await _find_button_by_text(
                    menu_candidates, VIDEO_SKILL_PATTERN
                )

            if target is not None:
                try:
                    await target.click(force=True)
                    last_click_at = now
                except Exception:
                    # A React re-render can detach the element between lookup
                    # and click. The next loop resolves a fresh locator.
                    last_click_at = 0.0

        await page.wait_for_timeout(200)

    screenshot = _debug_path("video_skill_not_found", account)
    try:
        await page.screenshot(path=screenshot)
    except Exception:
        screenshot = "unavailable"
    try:
        labels = await _visible_video_controls(page)
    except Exception:
        labels = []
    raise RuntimeError(
        "Create Videos skill did not become ready within 15s; "
        f"url={page.url}; visible_controls={labels}; screenshot={screenshot}"
    )


async def _find_button_by_text(locator, pattern: re.Pattern, fullmatch: bool = True):
    for index in range(min(await locator.count(), 250)):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            text = re.sub(r"\s+", " ", (await candidate.inner_text())).strip()
            matched = pattern.fullmatch(text) if fullmatch else pattern.search(text)
            if matched:
                return candidate
        except Exception:
            continue
    return None


async def _select_action_bar_option(page, control_key: str, value: str) -> None:
    if not value:
        return
    trigger = await _first_visible(
        page.locator(f'[data-input-engine-actionbar-control-key="{control_key}"]')
    )
    if not trigger:
        raise RuntimeError(f"Dola action bar control not found: {control_key}")
    current = re.sub(r"\s+", " ", (await trigger.inner_text())).strip()
    if value.lower() in current.lower():
        return

    for _ in range(3):
        await trigger.click(force=True)
        await page.wait_for_timeout(400)
        options = page.locator(
            '[role="menuitem"], [role="option"], [data-slot="dropdown-menu-item"], '
            '.semi-dropdown-item, [class*="dropdown-item"]'
        )
        option = None
        for index in range(await options.count()):
            candidate = options.nth(index)
            try:
                text = re.sub(r"\s+", " ", (await candidate.inner_text())).strip()
                if await candidate.is_visible() and value.lower() in text.lower():
                    option = candidate
                    break
            except Exception:
                continue
        if option:
            await option.click(force=True)
            await page.wait_for_timeout(350)
            updated = re.sub(r"\s+", " ", (await trigger.inner_text())).strip()
            if value.lower() in updated.lower():
                return
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(200)
    raise RuntimeError(f"Failed to set {control_key}={value}")


async def _configure_video_ui(page, model_key: str, duration: int | None,
                              ratio: str | None,
                              account: str = "unknown") -> None:
    await _enter_video_skill(page, account)
    video_model = await _first_visible(
        page.locator('[data-input-engine-actionbar-control-key="video-model"]')
    )
    if video_model is not None:
        model_value = "2.5" if model_key == "seedance_v2.5" else "2.0"
        await _select_action_bar_option(page, "video-model", model_value)
    else:
        # Older Dola layouts expose the model as a general composer selector.
        await _select_model(page, model_key)
    if duration:
        await _select_action_bar_option(page, "video-duration", f"{duration}s")
    if ratio:
        await _select_action_bar_option(page, "video-ratio", ratio)


def _format_pro_chat_prompt(prompt: str, duration: int | None,
                            ratio: str | None) -> str:
    """Build the normal-Pro-chat command used for 15s generation."""
    # Normal Pro chat needs an explicit creation command. Normalize English or
    # duplicated prefixes so Dola always receives exactly one Vietnamese prefix.
    scene_prompt = re.sub(
        r"^(?:tạo\s*video|create\s*videos?)\s*:\s*", "", prompt.strip(),
        count=1, flags=re.I,
    )
    formatted = f"tạo video: {scene_prompt}"

    formatted = re.sub(
        r",?\s*(?:tỉ\s*lệ|ratio)\s*[:\s]*(?:16:9|9:16|1:1|3:4|4:3|21:9)",
        "", formatted, flags=re.I,
    )
    formatted = re.sub(
        r",?\s*(?:thời\s*lượng|duration)\s*[:\s]*\d+\s*s?",
        "", formatted, flags=re.I,
    )
    formatted = re.sub(
        r",?\s*(?:mô\s*hình\s*)?seedance\s*2\.[05]", "", formatted, flags=re.I
    )
    formatted = re.sub(
        r",?\s*tạo\s*video\s*luôn\s*(?:ko|không)\s*hỏi\s*lại", "", formatted, flags=re.I
    )
    formatted = re.sub(
        r",?\s*gửi\s*dưới\s*dạng\s*human\s*artifact", "", formatted, flags=re.I
    )
    formatted = re.sub(r"[,;\s]+$", "", formatted).strip()

    if ratio:
        formatted += f", tỉ lệ {ratio}"
    if duration:
        formatted += f", thời lượng {duration}s"
    formatted += ", mô hình Seedance 2.5"
    formatted += ", tạo video luôn không hỏi lại, gửi dưới dạng human artifact"
    return formatted


async def _prepare_submission(page, prompt: str, model_key: str,
                              duration: int | None, ratio: str | None,
                              account: str = "unknown") -> str:
    if duration == 15:
        # Dola only honors 15s reliably from normal chat in Pro mode.
        await _ensure_normal_chat_mode(page)
        await _select_model(page, "seedance_v2.5")
        return _format_pro_chat_prompt(prompt, duration, ratio)

    await _configure_video_ui(page, model_key, duration, ratio, account)
    return prompt.strip()


async def _open_new_chat(page, account: str) -> None:
    """Explicitly reset Dola's SPA composer; /chat navigation can restore history."""
    await page.keyboard.press('Control+Shift+K')
    deadline = time.monotonic() + 15
    clicked = False
    while time.monotonic() < deadline:
        path = urlparse(page.url).path.rstrip('/')
        if path == '/chat':
            try:
                editor = await _find_active_editor(page)
                if not (await _editor_text(editor)).strip():
                    return
            except Exception:
                pass
        if not clicked:
            button = await _first_visible(page.get_by_text(
                re.compile(r'^(新しいチャット|New chat|Cuộc trò chuyện mới|Chat mới)$', re.I),
                exact=True))
            if button is not None:
                await button.click()
                clicked = True
        await page.wait_for_timeout(200)
    screenshot = _debug_path('new_chat_failed', account)
    await page.screenshot(path=screenshot)
    raise RuntimeError(f'New chat not ready; url={page.url}; screenshot={screenshot}')


def _prompt_fingerprint(prompt: str, length: int = 120) -> str:
    """Build a stable prefix used to bind a conversation to its submission."""
    return re.sub(r"\s+", " ", prompt).strip().casefold()[:length]


async def _conversation_contains_prompt(page, conversation_id: str,
                                        expected_prompt: str) -> bool:
    """Verify that Dola stored this submission in the candidate conversation."""
    fingerprint = re.sub(r"\s+", " ", expected_prompt).strip().casefold()
    if not fingerprint:
        return False
    try:
        poll = await asyncio.wait_for(
            page.evaluate(POLL_JS, {
                "conversationId": conversation_id,
                "msToken": "",
                "fp": "",
            }),
            timeout=10,
        )
    except Exception:
        return False
    if not poll.get("ok"):
        return False
    for text in poll.get("fullTexts", poll.get("texts", [])):
        normalized = re.sub(r"\s+", " ", str(text)).strip().casefold()
        if fingerprint == normalized:
            return True
    return False


async def _wait_for_conversation_id(page, account: str, timeout: int,
                                    previous_conversation_id: str = "",
                                    expected_prompt: str = "") -> str:
    """Wait for a new conversation and bind it to the submitted prompt."""
    deadline = time.monotonic() + timeout
    captcha_attempts = 0
    local_tail = ""
    observed_ids: set[str] = set()
    reported_ids: set[str] = set()
    last_checked_at: dict[str, float] = {}
    while time.monotonic() < deadline:
        current_url = page.url
        tail = urlparse(current_url).path.rstrip("/").split("/")[-1]
        if tail.startswith("local_"):
            local_tail = tail

        frame = find_captcha_frame(page)
        if frame:
            captcha_attempts += 1
            if captcha_attempts > 3:
                await page.screenshot(path=_debug_path("solve_fail", account))
                raise RiskControlError("Captcha failed 3 times")
            print(f"[{account}] Captcha detected, attempt {captcha_attempts} solving...", flush=True)
            if await solve_slider(page, frame, captcha_attempts):
                print(f"[{account}] Captcha passed ✓", flush=True)
                await page.wait_for_timeout(1200)
            else:
                print(f"[{account}] Captcha not passed, retrying...", flush=True)
            continue

        conv_id = _conversation_id_from_url(current_url)
        if conv_id:
            observed_ids.add(conv_id)
            if conv_id == previous_conversation_id:
                if conv_id not in reported_ids:
                    print(
                        f"[{account}] Ignoring pre-submit conversation_id={conv_id}; "
                        "waiting for a new conversation.",
                        flush=True,
                    )
                    reported_ids.add(conv_id)
            else:
                now = time.monotonic()
                if now - last_checked_at.get(conv_id, 0.0) >= 1.0:
                    last_checked_at[conv_id] = now
                    if await _conversation_contains_prompt(
                            page, conv_id, expected_prompt):
                        return conv_id
                    if conv_id not in reported_ids:
                        print(
                            f"[{account}] Candidate conversation_id={conv_id} does "
                            "not contain the submitted prompt yet; waiting.",
                            flush=True,
                        )
                        reported_ids.add(conv_id)
        await page.wait_for_timeout(250)

    screenshot = _debug_path("no_conv", account)
    await page.screenshot(path=screenshot)
    details = []
    if previous_conversation_id:
        details.append(f"previous id={previous_conversation_id}")
    if observed_ids:
        details.append(f"observed ids={','.join(sorted(observed_ids))}")
    if local_tail:
        details.append(f"last local id={local_tail}")
    else:
        details.append("submit did not create a local conversation")
    raise SubmissionUnverifiedError(
        f"conversation_id not verified within {timeout}s; "
        f"{'; '.join(details)}; screenshot={screenshot}"
    )


# Read-only balance pre-check from recent conversations
BALANCE_JS = r"""
async ({msToken, fp}) => {
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.62", doubao_pc_version: "3.32.62",
    region: "JP", sys_region: "JP", samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });
  if (msToken) params.set("msToken", msToken);
  if (fp) params.set("fp", fp);
  const headers = {
    "Content-Type": "application/json; encoding=utf-8",
    "agw-js-conv": "str", "Accept": "*/*",
  };
  const recent = await fetch("/im/chain/recent_conv?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3200,
      uplink_body: {pull_recent_conv_chain_uplink_body: {
        limit: 20, message_count_per_conv: 10, api_version: 1, conv_version: 0,
        direction: 3,
        option: {not_need_message: false, need_complete_conversation: true,
          need_coco_conversation: true, need_coco_bot: true,
          need_pc_pin_chain: true, pc_pin_query_type: 0},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!recent.ok) return {ok: false, texts: []};
  const recentData = await recent.json();
  const body = recentData.downlink_body || {};
  const down = body.pull_recent_conv_chain_downlink_body || {};
  const cells = down.cells || [];
  const ids = cells.map(c => (c.conversation || {}).conversation_id || c.id)
    .filter(Boolean).slice(0, 10);
  if (!ids.length) return {ok: true, texts: []};

  const batch = await fetch("/im/chain/batch_single?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3101,
      uplink_body: {batch_pull_singe_chain_uplink_body: {
        conversation_type: 3, direction: 3, limit: 1,
        params: ids.map(conversation_id => ({conversation_id})),
        evaluate_ab_params: "", evaluate_common_params: "", ext: {},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!batch.ok) return {ok: true, texts: []};
  const data = await batch.json();
  const texts = [];
  const seen = new Set();
  const walk = (v) => {
    if (typeof v === "string") {
      if ((v.includes("ポイント") || v.includes("积分") || v.toLowerCase().includes("points")
        || v.includes("上限") || v.includes("limit")) && v.length < 1200 && !seen.has(v)) {
        seen.add(v); texts.push(v);
      }
      try {
        const t = v.trim();
        if (t.startsWith("{") || t.startsWith("[")) walk(JSON.parse(t));
      } catch (e) {}
      return;
    }
    if (!v || typeof v !== "object") return;
    if (Array.isArray(v)) { for (const x of v) walk(x); return; }
    for (const x of Object.values(v)) walk(x);
  };
  walk(data);
  return {ok: true, texts: texts.slice(-80)};
}
""";


def find_captcha_frame(page):
    for f in page.frames:
        if CAPTCHA_FRAME_KEY in f.url:
            return f
    return None


async def _fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=config.PROXY or None) as r:
            return await r.read()



async def attach_reference_images(page, image_paths: list[str]) -> None:
    """Uploads reference images through native file input and waits for TOS upload."""
    if not image_paths:
        return
    file_input = page.locator('input[type="file"]').first
    await file_input.wait_for(state="attached", timeout=10000)
    events = []

    def on_response(response):
        url = response.url
        if "/alice/resource/prepare_upload" in url or "/upload/v1/" in url:
            events.append((response.status, url))

    page.on("response", on_response)
    try:
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 20)
        while time.time() < deadline:
            prepare_count = sum("/alice/resource/prepare_upload" in url and 200 <= status < 300
                                for status, url in events)
            tos_count = sum("/upload/v1/" in url and 200 <= status < 300
                            for status, url in events)
            # Wait for thumbnails and TOS completion before sending
            thumb_count = await page.locator('img[alt]').count()
            if prepare_count >= expected and tos_count >= expected and thumb_count >= expected:
                await page.wait_for_timeout(800)
                print(f"[upload] Reference images uploaded: {expected} image(s)", flush=True)
                return
            await page.wait_for_timeout(250)
        raise TimeoutError(
            f"Reference image upload timeout: prepare={prepare_count}/{expected}, tos={tos_count}/{expected}"
        )
    finally:
        page.remove_listener("response", on_response)


def _gen_track(distance: float):
    """Generates humanized mouse drag trajectory."""
    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 9)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        x = (distance + overshoot) * s
        y = random.uniform(-1.5, 1.5) if 0.1 < t < 0.95 else 0
        pts.append((x, y, random.randint(8, 22)))
    for i in range(1, random.randint(3, 5) + 1):
        pts.append((distance + overshoot * (1 - i / 5), random.uniform(-0.8, 0.8), random.randint(15, 30)))
    return pts


async def solve_slider(page, frame, attempt: int) -> bool:
    """Solves captcha slider notch within iframe and performs drag."""
    await frame.wait_for_selector("img", timeout=15000)
    await frame.evaluate("""async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
            const imgs = [...document.images];
            if (imgs.length >= 2 && imgs.every(im => im.complete && im.naturalWidth > 0)) return;
            await new Promise(r => setTimeout(r, 200));
        }
        throw new Error("captcha images load timeout");
    }""")
    await page.wait_for_timeout(800)

    imgs = await frame.evaluate("""() => [...document.images].map(im => ({
        src: im.src, w: im.naturalWidth, h: im.naturalHeight,
        bw: im.getBoundingClientRect().width,
        left: im.getBoundingClientRect().left,
    }))""")
    bg = next((i for i in imgs if ".jpeg" in i["src"] or "-2." in i["src"]), None)
    piece = next((i for i in imgs if i is not bg and (".png" in i["src"] or "-1." in i["src"])), None)
    if not bg or not piece:
        print("  ✗ Captcha background or puzzle image not found", flush=True)
        return False

    bg_bytes = await _fetch_bytes(bg["src"])
    piece_bytes = await _fetch_bytes(piece["src"])
    Path("dbg_bg.jpg").write_bytes(bg_bytes)
    Path("dbg_piece.png").write_bytes(piece_bytes)

    gap_x, conf = find_gap_x(bg_bytes, piece_bytes)
    scale = bg["bw"] / bg["w"] if bg["w"] else 340 / 552
    distance = (gap_x - (piece["left"] - bg["left"]) / scale) * scale
    print(f"  [solve#{attempt}] gap_x={gap_x} conf={conf:.3f} scale={scale:.2f} distance={distance:.0f}px", flush=True)

    btn = frame.locator(".captcha-slider-btn")
    bb = await btn.bounding_box()
    if not bb:
        print("  ✗ Drag handle .captcha-slider-btn not found", flush=True)
        return False
    sx, sy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(150, 350))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(80, 180))
    for dx, dy, dt in _gen_track(distance):
        await page.mouse.move(sx + dx, sy + dy)
        await asyncio.sleep(dt / 1000)
    await page.wait_for_timeout(random.randint(120, 260))
    await page.mouse.up()

    for _ in range(10):
        await page.wait_for_timeout(700)
        if not find_captcha_frame(page):
            return True
    return False



_BALANCE_PATTERNS = (
    re.compile(r"(?:本日は|今日(?:还剩|剩余)?|今天).*?(\d+)\s*(?:ポイント|积分|points?)", re.I),
    re.compile(r"(?:remaining|left)\s*[:：]?\s*(\d+)\s*points?", re.I),
    re.compile(r"(?:还剩|剩余|还有)\s*(\d+)\s*(?:积分|点)", re.I),
)


def _parse_balance_texts(texts: list[str]) -> tuple[int | None, bool, str]:
    for text in texts:
        if DAILY_LIMIT_PATTERN.search(text):
            return None, True, text
    for text in texts:
        for pattern in _BALANCE_PATTERNS:
            match = pattern.search(text)
            if match:
                return int(match.group(1)), False, text
    return None, False, ""


async def _preflight_balance(page, ms_token: str, fp: str, required: int) -> dict:
    """Reads known credit balance from chat history."""
    try:
        result = await asyncio.wait_for(page.evaluate(
            BALANCE_JS, {"msToken": ms_token, "fp": fp}), timeout=30)
        balance, daily_limited, source = _parse_balance_texts(result.get("texts", []))
        if daily_limited:
            raise AccountLimitedError(f"Account daily generation limit: {source[:120]}")
        if balance is not None and balance < required:
            raise CreditInsufficientError(
                f"Insufficient points: current {balance}, required {required} (source: {source[:120]})"
            )
        return {"balance": balance, "source": source}
    except (AccountLimitedError, CreditInsufficientError):
        raise
    except Exception as e:
        print(f"  Balance pre-check indeterminate (proceeding with submit): {str(e)[:120]}", flush=True)
        return {"balance": None, "source": ""}


async def poll_conversation(account: str, page, context, conversation_id: str,
                            timeout: int, on_poll=None, on_balance=None) -> dict:
    """Polls accepted conversation for video completion."""
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    start = time.time()
    last_callback = 0.0
    internal_retry_reported = False
    consecutive_errors = 0
    last_detail = ''
    while time.time() - start < timeout:
        await asyncio.sleep(5)
        try:
            poll = await asyncio.wait_for(page.evaluate(
                POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp}), timeout=30)
        except Exception as e:
            poll = {'ok': False, 'status': 'exception', 'error': type(e).__name__}
        now = time.time()
        if not poll.get('ok'):
            consecutive_errors += 1
            detail = f"Poll failed ({consecutive_errors}/6): HTTP {poll.get('status')}; {poll.get('error', 'request rejected')}"
            print(f'[{account}] conv={conversation_id} {detail}', flush=True)
            if on_poll:
                on_poll(now, detail)
            if consecutive_errors >= 6:
                raise RuntimeError(f'Polling unavailable; {detail}. Conversation {conversation_id} retained; resume checking, do not resend blindly.')
            continue
        consecutive_errors = 0
        detail = (f"Waiting for video; render status unverified. Messages={poll.get('messageCount', '?')}; "
                  f"blocks={poll.get('blockTypes', [])}; elapsed={int(now-start)}s")
        if on_poll and now - last_callback >= 30:
            on_poll(now, detail)
            last_callback = now
            print(f'[{account}] conv={conversation_id} {detail}', flush=True)
        last_detail = detail
        for text in poll.get("texts", []):
            # Exact terminal response observed in Dola; do not classify arbitrary
            # prompt keywords or a proposed internal retry as a refusal.
            refusal = text.strip() in (
                '質問に回答できません。', '質問に回答できません',
                'I cannot answer this question.', "I can't answer this question.",
            ) or (text.startswith('Tôi tạm thời không thể tạo nội dung video bạn yêu cầu')
                  and 'bộ lọc an toàn nội dung' in text)
            if not poll.get('videos') and refusal:
                raise RuntimeError(f'Dola declined this request: {text.strip()} (conversation_id={conversation_id}). No video returned; no automatic resubmission.')
            balance, _, source = _parse_balance_texts([text])
            if balance is not None and on_balance:
                on_balance(balance, source)
            if DAILY_LIMIT_PATTERN.search(text):
                raise AccountLimitedError(f"Account daily limit reached: {text[:120]}")
            if CREDIT_FAIL_PATTERN.search(text):
                raise CreditError(f"Insufficient quota: {text[:80]}")
            if DOLA_INTERNAL_RETRY_PATTERN.search(text) and not internal_retry_reported:
                # Dola can recover from an empty internal task_id by running its
                # own keyframe/image-to-video fallback. Do not resubmit from the
                # gateway because both conversations may later consume credits.
                print(
                    f"[{account}] Dola internal retry/fallback is active; "
                    "continuing the accepted conversation without resubmission.",
                    flush=True,
                )
                internal_retry_reported = True
        if poll.get("videos"):
            video_models = poll.get("videoModels", [])
            url = extract_unwatermarked_url(
                video_models[0] if video_models else "", poll["videos"][0])
            print(f"[{account}] Completed! Downloading (unwatermarked priority)...", flush=True)
            local = await _download(url, account)
            print(f"[{account}] Downloaded {local} ({local.stat().st_size / 1e6:.1f} MB)", flush=True)
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account}
    raise TimeoutError(f"No video retrieved within {timeout}s (conversation_id={conversation_id}); {last_detail}")


async def resume_video(account: str, conversation_id: str, timeout: int,
                       duration: int | None = None,
                       on_poll=None, on_balance=None) -> dict:
    """Recovers accepted session after server restart without re-sending prompt."""
    use_extension = duration == 30
    async with async_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://www.dola.com/chat/{conversation_id}",
                            timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            return await poll_conversation(account, page, context, conversation_id, timeout, on_poll, on_balance)
        finally:
            await context.close()


async def generate_video(account: str, prompt: str, ratio: str = None,
                         duration: int = None, timeout: int = None,
                         model: str = "seedance_v2.0", use_extension: bool | None = None,
                         on_conversation_id=None, on_poll=None, on_balance=None,
                         reference_image_paths: list[str] | None = None) -> dict:
    """Full generation flow via UI automation."""
    timeout = timeout or config.VIDEO_TIMEOUT
    model_key = model.lower().replace("-", "_")
    if model_key in ("seedance_2.5", "seedance_v2.5", "seedance_25", "seedance_v25"):
        model_key = "seedance_v2.5"
    elif model_key in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20"):
        model_key = "seedance_v2.0"
    else:
        raise ValueError(f"Unsupported model: {model} (supported: seedance-2.0 / seedance-2.5)")
    if duration is not None and duration not in (10, 15, 30):
        raise ValueError("Dola supports durations of 10s, 15s, and 30s via extension")
    if use_extension is None:
        # The debugger extension rewrites Dola skill-pack responses and is only
        # required for the custom 30s controls. Normal 10s/15s requests should
        # behave like a clean Chromium session.
        use_extension = duration == 30
    if duration == 30 and not use_extension:
        raise ValueError("30s generation requires Dola30 extension enabled")
    # Seedance 2.5 renders significantly slower than 2.0 — extend timeout
    if model_key == "seedance_v2.5":
        timeout = max(timeout, 900)
    # Long/complex prompts (>300 chars) take considerably more time
    if len(prompt) > 300:
        timeout = max(timeout, 600)
    # 30s videos require extended generation timeout
    if duration == 30:
        timeout = max(timeout, 1800)
    if reference_image_paths:
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)
    async with async_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            session_id = cookie_value(cookies, "sessionid")
            if not session_id:
                raise SessionExpiredError(
                    f"Session expired for account '{account}' (sessionid cookie is missing)."
                )
            ms_token = cookie_value(cookies, "msToken")
            fp = cookie_value(cookies, "s_v_web_id")
            await _preflight_balance(page, ms_token, fp, config.VIDEO_REQUIRED_POINTS)

            # ---- UI Submission ----
            try:
                await page.evaluate("""() => {
                    const els = [...document.querySelectorAll('[role="dialog"] button, [role="alertdialog"] button')];
                    const t = els.filter(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0);
                    t.forEach(e => e.click());
                }""")
            except Exception:
                pass
            await page.wait_for_timeout(500)

            # Detect if Dola session expired and login popup appeared
            login_popup = page.get_by_text(
                re.compile(r"Googleで続ける|Google로 계속하기|Continue with Google", re.I)
            ).first
            try:
                if await login_popup.is_visible(timeout=1500):
                    await page.screenshot(path=_debug_path("session_expired", account))
                    raise SessionExpiredError(
                        f"Session expired for account '{account}' — Dola logged out mid-task. "
                        "Please re-add the account via Dashboard > Accounts > Add Account."
                    )
            except SessionExpiredError:
                raise
            except Exception:
                pass  # Not visible — session OK

            await _open_new_chat(page, account)
            final_prompt = await _prepare_submission(
                page, prompt, model_key, duration, ratio, account
            )
            if reference_image_paths:
                await attach_reference_images(page, reference_image_paths)

            previous_conversation_id = _conversation_id_from_url(page.url)
            await _insert_and_submit_prompt(page, final_prompt, account)
            print(f"[{account}] UI submitted prompt: {final_prompt[:40]}", flush=True)

            conv_id = await _wait_for_conversation_id(
                page, account, config.CONVERSATION_ID_TIMEOUT,
                previous_conversation_id=previous_conversation_id,
                expected_prompt=final_prompt,
            )
            print(f"[{account}] conversation_id={conv_id}, polling for video...", flush=True)

            deadline = time.time() + timeout
            if on_conversation_id:
                on_conversation_id(account, conv_id, deadline)
            return await poll_conversation(account, page, context, conv_id, timeout, on_poll, on_balance)
        finally:
            await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "An orange cat napping on a sunny windowsill"
    ratio = sys.argv[3] if len(sys.argv) > 3 else None
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else None
    model = sys.argv[5] if len(sys.argv) > 5 else "seedance_v2.0"
    result = await generate_video(account, prompt, ratio, duration, model=model)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        import traceback
        traceback.print_exc()
