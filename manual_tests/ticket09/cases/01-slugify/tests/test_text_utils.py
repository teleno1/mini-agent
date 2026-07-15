import unittest

from text_utils import slugify


class SlugifyTests(unittest.TestCase):
    def test_collapses_whitespace(self) -> None:
        self.assertEqual(slugify("Mini   Agent\tMVP"), "mini-agent-mvp")

    def test_removes_punctuation(self) -> None:
        self.assertEqual(slugify("Hello, coding agent!"), "hello-coding-agent")

    def test_strips_separators(self) -> None:
        self.assertEqual(slugify(" --Already Slugged-- "), "already-slugged")


if __name__ == "__main__":
    unittest.main()
