import asyncio
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from video_worker_ui import (
    _conversation_id_from_url,
    _conversation_contains_prompt,
    _enter_video_skill,
    _format_pro_chat_prompt,
    _prompt_fingerprint,
    _prepare_submission,
    _wait_for_conversation_id,
    _select_model,
    _open_new_chat,
)


class ConversationIdParsingTests(unittest.TestCase):
    def test_accepts_server_issued_numeric_id(self):
        self.assertEqual(
            _conversation_id_from_url("https://www.dola.com/chat/38417873770849297"),
            "38417873770849297",
        )

    def test_prompt_fingerprint_normalizes_case_and_whitespace(self):
        self.assertEqual(
            _prompt_fingerprint("  Tạo VIDEO:\n  A cat   runs  "),
            "tạo video: a cat runs",
        )

    def test_rejects_local_and_non_chat_urls(self):
        self.assertEqual(
            _conversation_id_from_url("https://www.dola.com/chat/local_4420026521928617"),
            "",
        )
        self.assertEqual(_conversation_id_from_url("https://www.dola.com/chat"), "")
        self.assertEqual(_conversation_id_from_url("not a url"), "")

    def test_ignores_query_and_fragment(self):
        self.assertEqual(
            _conversation_id_from_url(
                "https://www.dola.com/chat/38417873770849297?source=test#result"
            ),
            "38417873770849297",
        )


class ProChatPromptTests(unittest.TestCase):
    def test_formats_fifteen_second_video_for_pro_chat(self):
        result = _format_pro_chat_prompt("A cat in a garden", 15, "9:16")

        self.assertTrue(result.startswith("tạo video: A cat in a garden"))
        self.assertIn("tỉ lệ 9:16", result)
        self.assertIn("thời lượng 15s", result)
        self.assertIn("mô hình Seedance 2.5", result)
        self.assertEqual(result.lower().count("không hỏi lại"), 1)
        self.assertEqual(result.lower().count("human artifact"), 1)

    def test_replaces_existing_generation_parameters(self):
        result = _format_pro_chat_prompt(
            "tạo video: city, ratio 16:9, duration 10s, mô hình Seedance 2.0",
            15,
            "1:1",
        )

        self.assertNotIn("16:9", result)
        self.assertNotIn("10s", result)
        self.assertNotIn("Seedance 2.0", result)
        self.assertIn("tỉ lệ 1:1", result)
        self.assertIn("thời lượng 15s", result)
        self.assertIn("Seedance 2.5", result)

    def test_normalizes_existing_english_prefix_without_duplication(self):
        result = _format_pro_chat_prompt(
            "Create video: sunrise over the ocean", 15, "16:9"
        )

        self.assertTrue(result.startswith("tạo video: sunrise over the ocean"))
        self.assertEqual(result.lower().count("tạo video:"), 1)


class SubmissionModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifteen_seconds_stays_in_normal_pro_chat(self):
        with (
            patch("video_worker_ui._ensure_normal_chat_mode", new=AsyncMock()) as normal,
            patch("video_worker_ui._select_model", new=AsyncMock()) as select_model,
            patch("video_worker_ui._configure_video_ui", new=AsyncMock()) as video_ui,
        ):
            prompt = await _prepare_submission(
                object(), "A cat in a garden", "seedance_v2.0", 15, "9:16"
            )

        normal.assert_awaited_once()
        select_model.assert_awaited_once_with(ANY, "seedance_v2.5")
        video_ui.assert_not_awaited()
        self.assertIn("thời lượng 15s", prompt)

    async def test_thirty_seconds_uses_create_videos_action_bar(self):
        page = object()
        with (
            patch("video_worker_ui._ensure_normal_chat_mode", new=AsyncMock()) as normal,
            patch("video_worker_ui._select_model", new=AsyncMock()) as select_model,
            patch("video_worker_ui._configure_video_ui", new=AsyncMock()) as video_ui,
        ):
            prompt = await _prepare_submission(
                page, "Pure scene description", "seedance_v2.5", 30, "16:9"
            )

        video_ui.assert_awaited_once_with(
            page, "seedance_v2.5", 30, "16:9", "unknown"
        )
        normal.assert_not_awaited()
        select_model.assert_not_awaited()
        self.assertEqual(prompt, "Pure scene description")

    async def test_ten_seconds_uses_create_videos_action_bar(self):
        page = object()
        with (
            patch("video_worker_ui._ensure_normal_chat_mode", new=AsyncMock()) as normal,
            patch("video_worker_ui._select_model", new=AsyncMock()) as select_model,
            patch("video_worker_ui._configure_video_ui", new=AsyncMock()) as video_ui,
        ):
            prompt = await _prepare_submission(
                page, "Pure scene description", "seedance_v2.0", 10, "9:16"
            )

        video_ui.assert_awaited_once_with(
            page, "seedance_v2.0", 10, "9:16", "unknown"
        )
        normal.assert_not_awaited()
        select_model.assert_not_awaited()
        self.assertEqual(prompt, "Pure scene description")


class VideoSkillEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_menu_waits_for_option_and_verifies_pro(self):
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        button = MagicMock()
        button.click = AsyncMock()
        button.inner_text = AsyncMock(side_effect=['高速', '高速', 'プロ'])
        option = MagicMock()
        option.click = AsyncMock()
        with (
            patch('video_worker_ui._find_model_selector', new=AsyncMock(return_value=button)),
            patch('video_worker_ui._find_button_by_text',
                  new=AsyncMock(side_effect=[None, None, option])) as find,
        ):
            await _select_model(page, 'seedance_v2.5')
        self.assertEqual(find.await_count, 3)
        option.click.assert_awaited_once()
        self.assertEqual(button.inner_text.await_count, 3)

    async def test_model_selector_can_appear_after_hydration(self):
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        button = MagicMock()
        button.inner_text = AsyncMock(return_value='Pro')
        with patch('video_worker_ui._find_model_selector',
                   new=AsyncMock(side_effect=[None, None, button])) as find:
            await _select_model(page, 'seedance_v2.5')
        self.assertEqual(find.await_count, 3)

    async def test_waits_for_delayed_skill_button_and_action_bar(self):
        page = MagicMock()
        page.wait_for_timeout = AsyncMock()
        page.locator.return_value = object()
        delayed_button = AsyncMock()

        with (
            patch(
                "video_worker_ui._video_skill_active",
                new=AsyncMock(side_effect=[False, False, False, True]),
            ),
            patch(
                "video_worker_ui._find_button_by_text",
                new=AsyncMock(side_effect=[None, None, delayed_button]),
            ) as find_button,
        ):
            await _enter_video_skill(page, "acc6")

        self.assertGreaterEqual(find_button.await_count, 3)
        delayed_button.click.assert_awaited_once_with(force=True)


class ConversationAcquisitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompts_with_same_prefix_are_not_interchangeable(self):
        prefix = 'A long identical cinematic scene description ' * 5
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={
            'ok': True, 'fullTexts': [prefix + 'orange boat'],
            'texts': [(prefix + 'orange boat')[:600]],
        })
        self.assertFalse(await _conversation_contains_prompt(
            page, '12345', prefix + 'blue boat'))

    async def test_new_chat_uses_shortcut_and_requires_empty_composer(self):
        page = MagicMock()
        page.url = 'https://www.dola.com/chat/12345'
        async def press(_key):
            page.url = 'https://www.dola.com/chat'
        page.keyboard.press = AsyncMock(side_effect=press)
        editor = MagicMock()
        with (
            patch('video_worker_ui._find_active_editor', new=AsyncMock(return_value=editor)),
            patch('video_worker_ui._editor_text', new=AsyncMock(return_value='')),
        ):
            await _open_new_chat(page, 'acc6')
        page.keyboard.press.assert_awaited_once_with('Control+Shift+K')

    async def test_rejects_old_id_and_waits_for_verified_new_conversation(self):
        old_id = "38417525216489489"
        new_id = "38417525449389585"

        class FakePage:
            def __init__(self):
                self.urls = [
                    f"https://www.dola.com/chat/{old_id}",
                    f"https://www.dola.com/chat/{new_id}",
                ]

            @property
            def url(self):
                return self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]

            async def wait_for_timeout(self, _milliseconds):
                return None

        page = FakePage()
        with (
            patch("video_worker_ui.find_captcha_frame", return_value=None),
            patch(
                "video_worker_ui._conversation_contains_prompt",
                new=AsyncMock(return_value=True),
            ) as contains_prompt,
        ):
            result = await _wait_for_conversation_id(
                page, "acc6", 1,
                previous_conversation_id=old_id,
                expected_prompt="tạo video: a new scene",
            )

        self.assertEqual(result, new_id)
        contains_prompt.assert_awaited_once_with(
            page, new_id, "tạo video: a new scene"
        )

    async def test_rejects_candidate_when_conversation_has_another_prompt(self):
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={
            "ok": True,
            "texts": ["Chỉ trả lời đúng một từ: OK", "OK"],
        })

        matched = await _conversation_contains_prompt(
            page,
            "38417525216489489",
            "tạo video: Create a 15-second military scene",
        )

        self.assertFalse(matched)

    async def test_accepts_candidate_containing_the_submitted_prompt(self):
        prompt = "tạo video: Create a 15-second military scene"
        page = MagicMock()
        page.evaluate = AsyncMock(return_value={
            "ok": True,
            "texts": [prompt, "Generating video"],
        })

        matched = await _conversation_contains_prompt(
            page, "38417525449389585", prompt
        )

        self.assertTrue(matched)

    async def test_parallel_accounts_do_not_share_stale_conversation_ids(self):
        class FakePage:
            def __init__(self, old_id, new_id):
                self.urls = [
                    f"https://www.dola.com/chat/{old_id}",
                    f"https://www.dola.com/chat/{new_id}",
                ]

            @property
            def url(self):
                return self.urls.pop(0) if len(self.urls) > 1 else self.urls[0]

            async def wait_for_timeout(self, _milliseconds):
                return None

        pages = []
        expected_ids = []
        calls = []
        for index in range(10):
            old_id = f"3841752521648{index:04d}"
            new_id = f"3841752544938{index:04d}"
            pages.append((FakePage(old_id, new_id), old_id, f"prompt {index}"))
            expected_ids.append(new_id)

        async def verify(page, conversation_id, prompt):
            calls.append((page, conversation_id, prompt))
            return True

        with (
            patch("video_worker_ui.find_captcha_frame", return_value=None),
            patch("video_worker_ui._conversation_contains_prompt", new=verify),
        ):
            results = await asyncio.gather(*(
                _wait_for_conversation_id(
                    page, f"acc{index + 1}", 1,
                    previous_conversation_id=old_id,
                    expected_prompt=prompt,
                )
                for index, (page, old_id, prompt) in enumerate(pages)
            ))

        self.assertEqual(results, expected_ids)
        self.assertEqual([item[1] for item in calls], expected_ids)


if __name__ == "__main__":
    unittest.main()
