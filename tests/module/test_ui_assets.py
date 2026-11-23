"""Guards for static assets: rules whose regressions were real UI bugs."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "module" / "static" / "css" / "app.css").read_text(encoding="utf-8")
JS = (ROOT / "module" / "static" / "js" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "module" / "templates" / "index.html").read_text(encoding="utf-8")


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


class EmptyAndStatusAssetsTestCase(unittest.TestCase):
    def test_server_filtered_empty_state_does_not_require_client_rows(self):
        # Server-side pagination sets rawRows === visibleRows, so checking
        # rawRows.length > 0 hid the "清空筛选" button on every empty search.
        self.assertNotIn("rawRows.length > 0 && isFiltered", JS)
        self.assertIn("if (isFiltered)", JS)

    def test_disk_banner_is_wired_across_html_css_and_js(self):
        self.assertIn('id="disk-banner"', HTML)
        self.assertIn(".disk-banner", CSS)
        self.assertIn('$("#disk-banner")', JS)

    def test_uploading_chip_style_exists_for_completed_tab(self):
        self.assertIn("is-uploading", JS)
        self.assertIn(".state-chip.is-uploading", CSS)

    def test_tab_loading_guards_against_polling_races(self):
        self.assertIn("tabRequestSeq", JS)
        self.assertIn("AbortController", JS)
        self.assertIn("AbortError", JS)
        self.assertIn("refreshActive({ background: true })", JS)

    def test_preview_form_values_cover_every_config_field(self):
        import importlib.util
        from module import config_form

        spec = importlib.util.spec_from_file_location(
            "preview_build", str(ROOT / "preview" / "build.py")
        )
        preview_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(preview_mod)

        expected = {field["name"] for field in config_form.FIELDS}
        actual = set(preview_mod.FORM_VALUES.keys())
        self.assertEqual(expected - actual, set())


if __name__ == "__main__":
    unittest.main()
