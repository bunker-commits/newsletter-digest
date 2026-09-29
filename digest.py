#!/usr/bin/env python3
"""Newsletter Digest.

Reads a list of RSS/Atom feeds from feeds.json and renders one static HTML
page (site/index.html), grouped by day. It is built to run on a schedule in
GitHub Actions and be published with GitHub Pages: no server, no database.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import feedparser

USER_AGENT = "newsletter-digest/1.0"
FETCH_TIMEOUT = 20
SUMMARY_LIMIT = 240
DEFAULTS = {"title": "Reading digest", "days": 14, "max_per_feed": 15, "timezone": "UTC"}

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_BLOCK_TAG_RE = re.compile(r"</?(?:p|br|div|li|ul|ol|h[1-6]|tr|td|table|blockquote|pre|hr)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Entry:
    source: str
    title: str
    link: str
    published: datetime  # timezone-aware, UTC
    summary: str


# --------------------------------------------------------------------------
# Cleaning helpers. Feed content is untrusted, so everything is reduced to
# plain text here and escaped again when the page is rendered.
# --------------------------------------------------------------------------


def clean_text(raw: str | None, limit: int = SUMMARY_LIMIT) -> str:
    """Strip markup from feed text, collapse whitespace and truncate."""
    text = _SCRIPT_STYLE_RE.sub(" ", raw or "")
    text = _BLOCK_TAG_RE.sub(" ", text)  # paragraph/line breaks separate words...
    text = html.unescape(_TAG_RE.sub("", text))  # ...inline tags like <b> must not
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def safe_url(url: str | None) -> str | None:
    """Only http(s) links are allowed; this drops javascript: and data: URLs."""
    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url.strip()
    return None


def source_hue(name: str) -> int:
    """Stable 0-359 hue per source, so each newsletter keeps its own color."""
    return zlib.crc32(name.encode("utf-8")) % 360


# --------------------------------------------------------------------------
# Fetching and parsing
# --------------------------------------------------------------------------


def fetch(url: str) -> bytes:
    """Download a feed. Plain file paths are also accepted (used by tests)."""
    if urlparse(url).scheme in ("http", "https"):
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        with urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return response.read()
    return Path(url).read_bytes()


def entry_datetime(entry, fallback: datetime) -> datetime:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return fallback


def entry_summary(entry) -> str:
    if entry.get("summary"):
        return entry["summary"]
    content = entry.get("content") or []
    return content[0].get("value", "") if content else ""


def load_feed(feed: dict, now: datetime, cutoff: datetime, max_per_feed: int) -> list[Entry]:
    parsed = feedparser.parse(fetch(feed["url"]))
    if not parsed.version:  # empty string means feedparser found no RSS/Atom format at all
        detail = f" ({parsed.bozo_exception})" if parsed.bozo else ""
        raise ValueError(f"not a readable RSS/Atom feed{detail}")
    if not parsed.entries:
        return []

    entries = []
    for item in parsed.entries:
        link = safe_url(item.get("link"))
        title = clean_text(item.get("title"), 200)
        if not link or not title:
            continue
        published = min(entry_datetime(item, now), now)  # clamp future-dated posts
        if published < cutoff:
            continue
        entries.append(Entry(feed["name"], title, link, published, clean_text(entry_summary(item))))

    entries.sort(key=lambda e: e.published, reverse=True)
    return entries[:max_per_feed]


def collect(config: dict, now: datetime):
    """Fetch every feed. Returns (entries, failures, feeds_that_worked).

    One broken feed never breaks the digest; it is reported on the page instead.
    """
    cutoff = now - timedelta(days=config["days"])

    def run(feed):
        try:
            return feed, load_feed(feed, now, cutoff, config["max_per_feed"]), None
        except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
            return feed, [], f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, config["feeds"]))

    entries: list[Entry] = []
    failures: list[tuple[str, str]] = []
    seen_links: set[str] = set()
    for feed, items, error in results:
        if error:
            failures.append((feed["name"], error))
            continue
        for entry in items:
            if entry.link in seen_links:  # same story from two feeds: keep first
                continue
            seen_links.add(entry.link)
            entries.append(entry)

    entries.sort(key=lambda e: e.published, reverse=True)
    return entries, failures, len(results) - len(failures)


def load_config(path: str) -> dict:
    config = {**DEFAULTS, **json.loads(Path(path).read_text(encoding="utf-8"))}
    feeds = config.get("feeds")
    if not isinstance(feeds, list) or not feeds:
        raise ValueError("feeds.json needs a non-empty 'feeds' list")

    normalized = []
    for feed in feeds:
        url = (feed.get("url") or "").strip() if isinstance(feed, dict) else ""
        if not url:
            raise ValueError(f"every feed needs a 'url': {feed!r}")
        name = (feed.get("name") or urlparse(url).netloc or url).strip()
        normalized.append({"name": name, "url": url})
    config["feeds"] = normalized
    ZoneInfo(config["timezone"])  # fail early on a mistyped timezone
    return config


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

PAGE = Template(
    """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>$title</title>
<style>
:root {
  --paper: #f4f6f9; --ink: #141b2d; --quiet: #5b6478; --rule: #d9dee8; --link: #2434c4;
  --s: 52%; --l: 42%;
}
@media (prefers-color-scheme: dark) {
  :root { --paper: #0e1220; --ink: #e6e9f2; --quiet: #98a1b8; --rule: #262d44; --link: #9aa8ff; --s: 62%; --l: 68%; }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
body {
  margin: 0; background: var(--paper); color: var(--ink);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
main { max-width: 60rem; margin: 0 auto; padding: 3rem 1.25rem 4rem; }
h1 {
  margin: 0 0 .35rem; font: 700 clamp(2rem, 6vw, 3.25rem)/1.05 Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
  letter-spacing: -.02em;
}
.lede { margin: 0 0 2rem; color: var(--quiet); }
.tools { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 2.5rem; align-items: center; }
#q {
  flex: 1 1 14rem; min-width: 0; padding: .55rem .75rem; font: inherit; color: inherit;
  background: transparent; border: 1px solid var(--rule); border-radius: 4px;
}
.chip {
  display: inline-flex; align-items: center; gap: .4rem; padding: .4rem .65rem; font: inherit; font-size: .875rem;
  color: var(--ink); background: transparent; border: 1px solid var(--rule); border-radius: 4px; cursor: pointer;
}
.chip[aria-pressed="true"] { border-color: var(--ink); font-weight: 600; }
.dot { width: .6rem; height: .6rem; border-radius: 50%; background: hsl(var(--h) var(--s) var(--l)); }
:is(#q, .chip, a):focus-visible { outline: 3px solid var(--link); outline-offset: 2px; }
.day { display: grid; grid-template-columns: 9rem 1fr; gap: 0 2rem; padding: 1.5rem 0; border-top: 1px solid var(--rule); }
.day h2 { position: sticky; top: 1rem; align-self: start; margin: 0; font-size: 1rem; font-weight: 600; }
.item { position: relative; padding: 0 0 1.5rem 1rem; }
.item:last-child { padding-bottom: 0; }
.item::before {
  content: ""; position: absolute; left: 0; top: .3rem; bottom: 1.6rem; width: 3px; border-radius: 2px;
  background: hsl(var(--h) var(--s) var(--l));
}
.item:last-child::before { bottom: 0; }
.title {
  color: var(--ink); text-decoration-color: var(--rule); text-underline-offset: .2em; text-decoration-thickness: 2px;
  font: 600 1.2rem/1.3 Charter, "Bitstream Charter", "Sitka Text", Cambria, Georgia, serif;
}
.title:hover { color: var(--link); text-decoration-color: currentColor; }
.meta { display: flex; gap: .75rem; margin: .2rem 0 .35rem; font-size: .85rem; color: var(--quiet); }
.meta b { font-weight: 600; color: var(--ink); }
.summary { margin: 0; color: var(--quiet); max-width: 62ch; }
.note { margin-top: 3rem; padding-top: 1.25rem; border-top: 1px solid var(--rule); font-size: .85rem; color: var(--quiet); }
.note p { margin: 0 0 .4rem; }
@media (max-width: 42rem) {
  .day { grid-template-columns: 1fr; gap: .75rem; }
  .day h2 { position: static; }
}
</style>
</head>
<body>
<main>
  <h1>$title</h1>
  <p class="lede">$lede</p>
  <div class="tools">
    <input id="q" type="search" placeholder="Search titles and summaries" aria-label="Search posts">
    $chips
  </div>
  $body
  <div class="note">$footer</div>
</main>
<script>
(function () {
  var items = Array.prototype.slice.call(document.querySelectorAll(".item"));
  var days = Array.prototype.slice.call(document.querySelectorAll(".day"));
  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip"));
  var search = document.getElementById("q");
  var source = "";
  function apply() {
    var q = search.value.trim().toLowerCase();
    items.forEach(function (el) {
      var okSource = !source || el.getAttribute("data-source") === source;
      var okText = !q || el.textContent.toLowerCase().indexOf(q) !== -1;
      el.hidden = !(okSource && okText);
    });
    days.forEach(function (d) { d.hidden = !d.querySelector(".item:not([hidden])"); });
  }
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      source = chip.getAttribute("data-source") || "";
      chips.forEach(function (c) { c.setAttribute("aria-pressed", String(c === chip)); });
      apply();
    });
  });
  search.addEventListener("input", apply);
})();
</script>
</body>
</html>
"""
)


def day_label(day, today) -> str:
    if day == today:
        return "Today"
    if day == today - timedelta(days=1):
        return "Yesterday"
    return f"{day:%a, %d %b %Y}"


def render(config: dict, entries: list[Entry], failures, now: datetime) -> str:
    esc = html.escape
    tz = ZoneInfo(config["timezone"])
    local_now = now.astimezone(tz)
    today = local_now.date()

    groups: dict = {}
    for entry in entries:
        groups.setdefault(entry.published.astimezone(tz).date(), []).append(entry)

    sections = []
    for day in sorted(groups, reverse=True):
        rows = []
        for e in groups[day]:
            summary = f'<p class="summary">{esc(e.summary)}</p>' if e.summary else ""
            rows.append(
                f'<article class="item" data-source="{esc(e.source)}" style="--h:{source_hue(e.source)}">'
                f'<a class="title" href="{esc(e.link)}" target="_blank" rel="noopener noreferrer">{esc(e.title)}</a>'
                f'<div class="meta"><b>{esc(e.source)}</b><span>{e.published.astimezone(tz):%H:%M}</span></div>'
                f"{summary}</article>"
            )
        sections.append(
            f'<section class="day"><h2>{esc(day_label(day, today))}</h2><div>{"".join(rows)}</div></section>'
        )

    if entries:
        body = "".join(sections)
    else:
        body = (
            f'<p class="lede">Nothing new in the last {config["days"]} days. '
            "Add more feeds in feeds.json or raise <code>days</code> to look further back.</p>"
        )

    counts: dict[str, int] = {}
    for e in entries:
        counts[e.source] = counts.get(e.source, 0) + 1
    chips = ['<button class="chip" type="button" aria-pressed="true" data-source="">All</button>']
    for feed in config["feeds"]:
        if feed["name"] in counts:
            chips.append(
                f'<button class="chip" type="button" aria-pressed="false" data-source="{esc(feed["name"])}" '
                f'style="--h:{source_hue(feed["name"])}"><span class="dot"></span>'
                f'{esc(feed["name"])} ({counts[feed["name"]]})</button>'
            )

    footer = [f"<p>Updated {local_now:%d %b %Y, %H:%M} ({esc(config['timezone'])}).</p>"]
    for name, error in failures:
        footer.append(f"<p>Couldn't load {esc(name)}: {esc(error)}</p>")

    lede = (
        f"{len(entries)} new post{'s' if len(entries) != 1 else ''} from "
        f"{len(counts)} of {len(config['feeds'])} sources in the last {config['days']} days."
    )
    return PAGE.substitute(
        title=esc(config["title"]), lede=esc(lede), chips="".join(chips), body=body, footer="".join(footer)
    )


def render_markdown(config: dict, entries: list[Entry], failures, now: datetime) -> str:
    """A dated snapshot of the same digest, for a durable, browsable git history.

    Unlike the HTML page (which always shows a rolling window and is rebuilt
    in place), this file is written once per run and committed, so every run
    leaves a permanent record of what was new at that point in time.
    """
    tz = ZoneInfo(config["timezone"])
    local_now = now.astimezone(tz)

    lines = [f"# {config['title']} — {local_now:%d %b %Y}", ""]
    lines.append(
        f"{len(entries)} new post{'s' if len(entries) != 1 else ''} from "
        f"{len({e.source for e in entries})} of {len(config['feeds'])} sources "
        f"in the last {config['days']} days. Generated {local_now:%d %b %Y, %H:%M} ({config['timezone']})."
    )
    lines.append("")

    if entries:
        by_day: dict = {}
        for e in entries:
            by_day.setdefault(e.published.astimezone(tz).date(), []).append(e)
        for day in sorted(by_day, reverse=True):
            lines.append(f"## {day:%A, %d %b %Y}")
            lines.append("")
            for e in by_day[day]:
                lines.append(f"- **[{e.title}]({e.link})** — {e.source}, {e.published.astimezone(tz):%H:%M}")
                if e.summary:
                    lines.append(f"  {e.summary}")
            lines.append("")
    else:
        lines.append(f"Nothing new in the last {config['days']} days.")
        lines.append("")

    if failures:
        lines.append("---")
        lines.append("")
        for name, error in failures:
            lines.append(f"- Couldn't load {name}: {error}")
        lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build a static digest page from RSS/Atom feeds.")
    parser.add_argument("--config", default="feeds.json", help="path to the feed list (default: feeds.json)")
    parser.add_argument("--out", default="site", help="output directory for the HTML page (default: site)")
    parser.add_argument(
        "--archive-dir",
        default=None,
        help="if set, also write a dated Markdown snapshot into this directory (e.g. digests)",
    )
    parser.add_argument("--now", help="ISO timestamp to treat as 'now' (for tests and reproducible builds)")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, KeyError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    now = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    entries, failures, succeeded = collect(config, now)
    for name, error in failures:
        print(f"warning: {name}: {error}", file=sys.stderr)

    if succeeded == 0:
        print("error: every feed failed, so no page was written", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render(config, entries, failures, now), encoding="utf-8")
    print(f"wrote {out / 'index.html'}: {len(entries)} posts from {succeeded}/{len(config['feeds'])} feeds")

    if args.archive_dir:
        tz = ZoneInfo(config["timezone"])
        archive = Path(args.archive_dir)
        archive.mkdir(parents=True, exist_ok=True)
        snapshot_path = archive / f"{now.astimezone(tz):%Y-%m-%d}.md"
        snapshot_path.write_text(render_markdown(config, entries, failures, now), encoding="utf-8")
        print(f"wrote {snapshot_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
