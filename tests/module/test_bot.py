"""Tests for the bot's help card."""

import asyncio
import unittest

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

    def examples_block(self) -> str:
        return bot.build_help_text().split("Command examples:")[1]

    def test_every_command_has_an_example(self):
        examples = self.examples_block()

        for command in self.COMMANDS:
            self.assertIn("\n" + command, examples, command)

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
        # The command text itself survives the markdown pass untouched.
        self.assertIn("/download https://t.me/example_channel 1 0", parsed["message"])

    def test_chinese_card_translates_the_example_header(self):
        language.set_language(Language.ZH)

        text = bot.build_help_text()

        self.assertIn("命令示例", text)
        self.assertNotIn("Command examples:", text)


if __name__ == "__main__":
    unittest.main()
