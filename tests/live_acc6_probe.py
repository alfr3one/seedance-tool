"""Explicit, manually run Dola integration probe (consumes one video request)."""
import asyncio
import json
import time

from patchright.async_api import async_playwright
from browser import launch_account_context
from video_worker_ui import (
    _open_new_chat, _prepare_submission, _insert_and_submit_prompt,
    _wait_for_conversation_id, poll_conversation,
)


async def main():
    started = time.time()
    async with async_playwright() as p:
        context = await launch_account_context(p, 'acc6', use_extension=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://www.dola.com/chat/38417525216489489',
                            wait_until='domcontentloaded')
            await page.wait_for_timeout(3000)
            await _open_new_chat(page, 'acc6')
            print(json.dumps({'phase': 'new_chat', 'url': page.url}), flush=True)
            prompt = await _prepare_submission(page,
                'A small orange paper boat floats gently across a quiet blue pond, '
                'soft morning sunlight, static camera, no text.',
                'seedance_v2.5', 15, '9:16', 'acc6')
            await _insert_and_submit_prompt(page, prompt, 'acc6')
            conv = await _wait_for_conversation_id(page, 'acc6', 45,
                previous_conversation_id='38417525216489489', expected_prompt=prompt)
            print(json.dumps({'phase': 'verified', 'id': conv,
                              'seconds': time.time() - started}), flush=True)
            result = await poll_conversation('acc6', page, context, conv, 900)
            print(json.dumps({'phase': 'completed', 'path': result['local_path'],
                              'seconds': time.time() - started}), flush=True)
        finally:
            await context.close()


if __name__ == '__main__':
    asyncio.run(main())
