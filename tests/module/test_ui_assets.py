"""Guards for static assets: rules whose regressions were real UI bugs."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "module" / "static" / "css" / "app.css").read_text(encoding="utf-8")
JS = (ROOT / "module" / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _rule(selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    return match.group(1) if match else ""


class TableScrollWheelTestCase(unittest.TestCase):
    def test_vertical_scroll_is_not_contained(self):
        # ``overflow-x: auto`` makes the computed overflow-y ``auto`` as well.
        # A plain ``overscroll-behavior: contain`` then swallowed the mouse
        # wheel while the pointer was over a long table: the page only scrolled
        # from outside the list.
        body = _rule(".table-scroll")
        self.assertIn("overflow-x: auto", body)
        self.assertIn("overscroll-behavior-x: contain", body)
        self.assertNotIn("overscroll-behavior: contain", body)


class ActionColumnTestCase(unittest.TestCase):
    def test_action_column_fits_three_labelled_buttons(self):
        # 上传 / 复制 / 删除 need ~186px; a narrower column cut the last button
        # in half on medium screens.
        body = _rule(".col-actions")
        match = re.search(r"width:\s*(\d+)px", body)
        self.assertIsNotNone(match, body)
        self.assertGreaterEqual(int(match.group(1)), 200)


class PaginationUrlTestCase(unittest.TestCase):
    def test_query_separator_depends_on_the_base_url(self):
        # v2.13.0 appended "&" to every list endpoint, so
        # /get_failed_list&page=1&page_size=25 became a 404 (the "&" was part
        # of the path). The separator must be picked from the base URL.
        self.assertIn('base.indexOf("?") === -1 ? "?" : "&"', JS)


if __name__ == "__main__":
    unittest.main()
