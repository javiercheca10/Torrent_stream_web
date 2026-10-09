import unittest

import bencodepy

from bot import main as bot


class BotHelpersTests(unittest.TestCase):
    def test_parse_torrent_name(self):
        data = bencodepy.encode({b"info": {b"name": b"demo.mp4", b"length": 1, b"piece length": 16384, b"pieces": b"a" * 20}})
        self.assertEqual(bot.parse_torrent_name(data), "demo.mp4")

    def test_parse_torrent_name_invalid(self):
        self.assertEqual(bot.parse_torrent_name(b"bad"), "torrent")

    def test_messages_escape_torrent_metadata(self):
        message = bot._msg_ready({"title": "<b>bad</b>"}, "ignored", "<video>.mkv", 1, "http://x/?a=1&b=2")
        self.assertIn("&lt;b&gt;bad&lt;/b&gt;", message)
        self.assertIn("&lt;video&gt;", message)
        self.assertIn("&amp;", message)

    def test_allowed_requires_id(self):
        self.assertFalse(bot.allowed(None))
        self.assertFalse(bot.allowed(0))

    def test_fmt_size_bytes(self):
        self.assertEqual(bot.fmt_size(512), "512 B")

    def test_fmt_size_mib(self):
        self.assertEqual(bot.fmt_size(1048576), "1.0 MiB")

    def test_fmt_size_gib(self):
        self.assertEqual(bot.fmt_size(1073741824), "1.00 GiB")


class BotKeyboardTests(unittest.TestCase):
    def test_session_keyboard_has_pause_and_delete(self):
        kb = bot.session_keyboard("abc")
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "pause:abc")
        self.assertEqual(kb["inline_keyboard"][0][1]["callback_data"], "del:abc")

    def test_session_keyboard_button_texts(self):
        kb = bot.session_keyboard("abc")
        self.assertIn("Pausar", kb["inline_keyboard"][0][0]["text"])
        self.assertIn("Terminar", kb["inline_keyboard"][0][1]["text"])

    def test_paused_session_keyboard_offers_resume(self):
        kb = bot.session_keyboard("abc", paused=True)
        self.assertIn("Reanudar", kb["inline_keyboard"][0][0]["text"])
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "resume:abc")

    def test_resumed_session_keyboard_returns_to_pause(self):
        kb = bot.session_keyboard("abc", paused=False)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "pause:abc")

    def test_buffer_keyboard_has_two_buttons(self):
        kb = bot.buffer_keyboard("abc")
        self.assertEqual(len(kb["inline_keyboard"][0]), 2)
        self.assertEqual(kb["inline_keyboard"][0][0]["callback_data"], "buf:abc")
        self.assertEqual(kb["inline_keyboard"][0][1]["callback_data"], "del:abc")


if __name__ == "__main__":
    unittest.main()
