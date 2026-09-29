import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import digest  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
NOW = "2026-09-28T12:00:00+00:00"


def write_config(directory: Path, feeds: list[dict]) -> Path:
    path = directory / "feeds.json"
    path.write_text(
        json.dumps({"title": "Test digest", "days": 14, "timezone": "Asia/Kolkata", "feeds": feeds}),
        encoding="utf-8",
    )
    return path


class CleaningTests(unittest.TestCase):
    def test_strips_tags_scripts_and_entities(self):
        raw = "<p>Hello <b>world</b> &amp; friends</p><script>steal()</script><style>p{}</style>"
        self.assertEqual(digest.clean_text(raw), "Hello world & friends")

    def test_truncates_on_a_word_boundary(self):
        text = digest.clean_text("word " * 200, limit=50)
        self.assertLessEqual(len(text), 50)
        self.assertTrue(text.endswith("…"))

    def test_safe_url_only_allows_http(self):
        self.assertEqual(digest.safe_url("https://example.com/a"), "https://example.com/a")
        self.assertIsNone(digest.safe_url("javascript:alert(1)"))
        self.assertIsNone(digest.safe_url("data:text/html,hi"))
        self.assertIsNone(digest.safe_url(""))
        self.assertIsNone(digest.safe_url(None))

    def test_source_hue_is_stable_and_in_range(self):
        self.assertEqual(digest.source_hue("Blog"), digest.source_hue("Blog"))
        self.assertTrue(0 <= digest.source_hue("Blog") < 360)


class BuildTests(unittest.TestCase):
    def build(self, feeds):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        config = write_config(tmp_path, feeds)
        out = tmp_path / "site"
        code = digest.main(["--config", str(config), "--out", str(out), "--now", NOW])
        page = (out / "index.html").read_text(encoding="utf-8") if (out / "index.html").exists() else None
        return code, page

    def two_feeds(self):
        return [
            {"name": "Sample Blog", "url": str(FIXTURES / "sample_rss.xml")},
            {"name": "Sample Atom", "url": str(FIXTURES / "sample_atom.xml")},
        ]

    def test_builds_page_from_rss_and_atom(self):
        code, page = self.build(self.two_feeds())
        self.assertEqual(code, 0)
        self.assertIn("Fresh post one", page)
        self.assertIn("Atom entry two", page)
        self.assertIn("Hello world, this is the summary.", page)

    def test_old_posts_are_left_out(self):
        _, page = self.build(self.two_feeds())
        self.assertNotIn("Ancient post", page)

    def test_same_story_from_two_feeds_appears_once(self):
        _, page = self.build(self.two_feeds())
        self.assertEqual(page.count("Shared story"), 1)

    def test_unsafe_links_and_markup_never_reach_the_page(self):
        _, page = self.build(self.two_feeds())
        self.assertNotIn("Bad link post", page)
        self.assertNotIn("javascript:", page)
        self.assertNotIn("alert(1)", page)
        self.assertIn("Injected title", page)

    def test_days_are_grouped_in_the_configured_timezone(self):
        # 20:00 UTC on 27 Sep is 01:30 on 28 Sep in Asia/Kolkata, so it belongs to "Today".
        _, page = self.build(self.two_feeds())
        self.assertIn("<h2>Today</h2>", page)
        self.assertIn("<h2>Yesterday</h2>", page)
        self.assertIn("Sat, 26 Sep 2026", page)

    def test_one_broken_feed_is_reported_but_does_not_stop_the_build(self):
        feeds = self.two_feeds() + [{"name": "Broken", "url": str(FIXTURES / "does_not_exist.xml")}]
        code, page = self.build(feeds)
        self.assertEqual(code, 0)
        self.assertIn("Couldn't load Broken", page)
        self.assertIn("Fresh post one", page)

    def test_all_feeds_failing_returns_an_error_and_writes_nothing(self):
        code, page = self.build([{"name": "Broken", "url": str(FIXTURES / "does_not_exist.xml")}])
        self.assertEqual(code, 1)
        self.assertIsNone(page)

    def test_non_feed_content_counts_as_a_failure(self):
        not_a_feed = FIXTURES / "not_a_feed.txt"
        not_a_feed.write_text("<html><body>Just a web page</body></html>", encoding="utf-8")
        self.addCleanup(not_a_feed.unlink)
        code, page = self.build([{"name": "Web page", "url": str(not_a_feed)}])
        self.assertEqual(code, 1)
        self.assertIsNone(page)


class MarkdownArchiveTests(unittest.TestCase):
    def build_with_archive(self, feeds):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tmp_path = Path(tmp.name)
        config = write_config(tmp_path, feeds)
        out = tmp_path / "site"
        archive = tmp_path / "digests"
        code = digest.main(
            ["--config", str(config), "--out", str(out), "--archive-dir", str(archive), "--now", NOW]
        )
        files = list(archive.glob("*.md"))
        return code, files

    def test_writes_one_dated_markdown_file(self):
        feeds = [
            {"name": "Sample Blog", "url": str(FIXTURES / "sample_rss.xml")},
            {"name": "Sample Atom", "url": str(FIXTURES / "sample_atom.xml")},
        ]
        code, files = self.build_with_archive(feeds)
        self.assertEqual(code, 0)
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("Fresh post one", text)
        self.assertIn("[Fresh post one]", text)  # rendered as a Markdown link


class ConfigTests(unittest.TestCase):
    def test_rejects_missing_feeds_and_bad_timezone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.json"
            path.write_text(json.dumps({"feeds": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                digest.load_config(str(path))
            path.write_text(json.dumps({"timezone": "Mars/Base", "feeds": [{"url": "https://a.example/rss"}]}))
            with self.assertRaises(KeyError):
                digest.load_config(str(path))

    def test_name_defaults_to_the_hostname(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feeds.json"
            path.write_text(json.dumps({"feeds": [{"url": "https://blog.example.com/rss"}]}))
            self.assertEqual(digest.load_config(str(path))["feeds"][0]["name"], "blog.example.com")


if __name__ == "__main__":
    unittest.main()
