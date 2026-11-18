"""Tests for the bot's help card."""

import asyncio
import unittest
from unittest import mock

from pyrogram.parser.parser import Parser

from module import bot, language
from module.language import Language


class HelpKeyboardTestCase(unittest.TestCase):
    def test_help_card_points_at_this_project_and_botfather(self):
        # The upstream project and its old support group must not be advertised
        # by this fork: the buttons have to open this repo and BotFather.
        markup = bot.help_keyboard()
        buttons = markup.inline_keyboard[0]

        self.assertEqual([button.text for button in buttons], ["Github", "BotFather"])
        self.assertEqual(buttons[0].url, "https://github.com/QCEnjoyLL/tg-dl")
        self.assertEqual(buttons[1].url, "https://t.me/BotFather")

    def test_help_card_links_are_https(self):
        for button in bot.help_keyboard().inline_keyboard[0]:
            self.assertTrue(button.url.startswith("https://"), button.url)


class HelpTextTestCase(unittest.TestCase):
    COMMANDS = [
        "/help",
        "/get_info",
        "/download",
        "/forward",
        "/listen_forward",
        "/forward_to_comments",
        "/set_language",
        "/stop",
    ]

    def setUp(self):
        self._saved_language = language._language  # pylint: disable = W0212
        language.set_language(Language.EN)

    def tearDown(self):
        language.set_language(self._saved_language)

    @staticmethod
    def examples_block(header: str = "Command examples:") -> str:
        return bot.build_help_text().split(header)[1]

    def example_lines(self) -> list[str]:
        return [line for line in self.examples_block().splitlines() if line.strip()]

    def test_every_command_has_an_example(self):
        examples = self.examples_block()

        for command in self.COMMANDS:
            self.assertIn("\n" + command, examples, command)

    def test_every_example_carries_an_annotation(self):
        lines = [line for line in self.example_lines() if line != "```"]

        # 每条示例两行：命令一行，下一行以「# 」开头的说明
        self.assertEqual(len(lines), 18)
        for command, annotation in zip(lines[::2], lines[1::2]):
            self.assertTrue(command.startswith("/"), command)
            self.assertTrue(annotation.startswith("# "), annotation)

    def test_examples_block_is_fenced_code(self):
        examples = self.examples_block().strip()

        # Markdown fences keep the examples copyable and aligned on Telegram.
        self.assertTrue(examples.startswith("```"), examples[:20])
        self.assertTrue(examples.endswith("```"), examples[-20:])

    def test_markdown_renders_as_pre_block(self):
        parsed = asyncio.run(Parser(None).parse(bot.build_help_text()))
        entities = parsed["entities"] or []
        kinds = [type(entity).__name__ for entity in entities]

        self.assertIn("MessageEntityPre", kinds, kinds)
        # The command text itself survives the markdown pass untouched, with the
        # explanation on the next line.
        self.assertIn(
            "/download https://t.me/example_channel 1 0\n"
            "# Download all messages from channel example_channel "
            "(1 = oldest, 0 = newest)",
            parsed["message"],
        )

    def test_chinese_card_translates_the_example_header(self):
        language.set_language(Language.ZH)

        text = bot.build_help_text()

        self.assertIn("命令示例", text)
        self.assertNotIn("Command examples:", text)

    def test_chinese_card_annotates_examples_in_chinese(self):
        language.set_language(Language.ZH)
        examples = self.examples_block("命令示例：")

        self.assertIn("从群组 1111111111 转发第 1~400 条消息到群组 2222222222", examples)
        self.assertIn("从频道 example_channel 下载全部消息", examples)
        self.assertIn("从群组 1234567890 只下载符合 [过滤器] 的消息", examples)
        self.assertIn("把群组 1111111111 的第 100~200 条消息转发到群组 2222222222", examples)
        self.assertIn("停止当前下载或转发任务", examples)


class HelpUpdateHintTestCase(unittest.TestCase):
    """A newer release is advertised under the help card (and only then)."""

    NEWER = {
        "current": "2.13.2",
        "latest": "2.14.0",
        "newer": True,
        "url": "https://github.com/QCEnjoyLL/tg-dl/releases",
        "checked_at": 1,
    }
    UP_TO_DATE = {
        "current": "2.13.2",
        "latest": "2.13.2",
        "newer": False,
        "url": "https://github.com/QCEnjoyLL/tg-dl/releases",
        "checked_at": 1,
    }

    def setUp(self):
        self._saved_language = language._language  # pylint: disable = W0212
        language.set_language(Language.EN)

    def tearDown(self):
        language.set_language(self._saved_language)

    def test_no_hint_when_already_up_to_date(self):
        with mock.patch.object(bot, "update_info", return_value=self.UP_TO_DATE):
            text = bot.build_help_text()

        self.assertNotIn("is available", text)
        self.assertNotIn("releases", text)

    def test_hint_names_the_new_version(self):
        with mock.patch.object(bot, "update_info", return_value=self.NEWER):
            text = bot.build_help_text()

        self.assertIn(
            "New version v2.14.0 is available (current v2.13.2)", text
        )
        self.assertIn(self.NEWER["url"], text)

    def test_hint_is_translated(self):
        language.set_language(Language.ZH)
        with mock.patch.object(bot, "update_info", return_value=self.NEWER):
            text = bot.build_help_text()

        self.assertIn("新版本 v2.14.0 可用（当前 v2.13.2）", text)


if __name__ == "__main__":
    unittest.main()
