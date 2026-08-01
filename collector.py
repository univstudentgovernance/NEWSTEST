#!/usr/bin/env python3
"""RSS-only college student-council news collector.

The collector fetches syndication feeds, not article pages. It therefore does
not bypass robots.txt, paywalls, logins, or anti-bot controls.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "news.json"
USER_AGENT = "StudentCouncilNewsMonitor/1.0 (+GitHub Actions; RSS metadata only)"
KST = timezone(timedelta(hours=9))


def clean(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value: str | None) -> datetime:
    if value:
        try:
            dt = email.utils.parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(timezone.utc)


def text_of(node: ET.Element, *names: str) -> str:
    for name in names:
        child = node.find(name)
        if child is not None and child.text:
            return child.text
    return ""


def unwrap_google_url(url: str) -> str:
    # Google News RSS links remain valid redirects. We intentionally do not
    # resolve them because that would request the publisher's article page.
    return url.strip()


def fetch_xml(url: str) -> ET.Element:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml;q=0.9"})
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = response.read(5_000_000)
    return ET.fromstring(payload)


def parse_rss(root: ET.Element, source_name: str, matched_query: str = "") -> list[dict]:
    rows: list[dict] = []
    items = root.findall("./channel/item")
    if not items and root.tag.endswith("rss"):
        items = root.findall(".//item")
    for item in items:
        source_node = item.find("source")
        link = text_of(item, "link", "guid").strip()
        rows.append({
            "title": clean(text_of(item, "title")),
            "url": link,
            "summary": clean(text_of(item, "description", "{http://purl.org/rss/1.0/modules/content/}encoded")),
            "source": clean(source_node.text if source_node is not None else source_name),
            "published_at": parse_date(text_of(item, "pubDate", "{http://purl.org/dc/elements/1.1/}date")).isoformat(),
            "matched_query": matched_query,
            "feed": source_name,
        })
    return rows


def fetch_google_feed(query: str) -> list[dict]:
    params = urllib.parse.urlencode({"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
    url = f"https://news.google.com/rss/search?{params}"
    return parse_rss(fetch_xml(url), "Google News RSS", query)


def fetch_publisher_feed(source: dict) -> list[dict]:
    # Official publisher RSS items normally contain the publisher's own article URL.
    return parse_rss(fetch_xml(source["url"]), source["name"])


def accepted(item: dict, config: dict) -> bool:
    haystack = f"{item['title']} {item['summary']}".lower()
    if any(word.lower() in haystack for word in config["exclude_any"]):
        return False
    has_core = any(word.lower() in haystack for word in config["required_any"])
    has_context = any(word.lower() in haystack for word in config["university_context"])
    return has_core and has_context


def category(item: dict, config: dict) -> str:
    haystack = f"{item['title']} {item['summary']}".lower()
    scores = {
        name: sum(1 for word in words if word.lower() in haystack)
        for name, words in config["category_rules"].items()
    }
    winner, score = max(scores.items(), key=lambda pair: pair[1])
    return winner if score else "기타"


def identity(item: dict) -> str:
    # Title normalization catches the same syndicated story returned by several queries.
    normalized = re.sub(r"[^0-9a-z가-힣]+", "", item["title"].lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def load_existing() -> list[dict]:
    try:
        return json.loads(DATA_PATH.read_text(encoding="utf-8")).get("items", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    merged = {item["id"]: item for item in load_existing() if item.get("id")}
    failures = []
    for index, query in enumerate(config["queries"]):
        try:
            for item in fetch_google_feed(query):
                if accepted(item, config):
                    item["id"] = identity(item)
                    item["category"] = category(item, config)
                    item["collected_at"] = datetime.now(timezone.utc).isoformat()
                    merged.setdefault(item["id"], item)
        except Exception as exc:  # keep other feeds usable when one request fails
            failures.append({"query": query, "error": f"{type(exc).__name__}: {exc}"})
        if index + 1 < len(config["queries"]):
            time.sleep(1.2)

    for source in config.get("rss_sources", []):
        try:
            for item in fetch_publisher_feed(source):
                if item["title"] and item["url"] and accepted(item, config):
                    item["id"] = identity(item)
                    item["category"] = category(item, config)
                    item["collected_at"] = datetime.now(timezone.utc).isoformat()
                    # Prefer an official publisher RSS record over a Google record
                    # with the same normalized title, because its link is direct.
                    merged[item["id"]] = item
        except Exception as exc:
            failures.append({"source": source["name"], "error": f"{type(exc).__name__}: {exc}"})
        time.sleep(1.2)

    cutoff = datetime.now(timezone.utc) - timedelta(days=int(config["retention_days"]))
    items = [item for item in merged.values() if datetime.fromisoformat(item["published_at"]) >= cutoff]
    items.sort(key=lambda item: item["published_at"], reverse=True)
    items = items[: int(config["max_items"])]
    output = {
        "updated_at": datetime.now(KST).isoformat(),
        "item_count": len(items),
        "collection_policy": "RSS metadata only; article pages are not crawled",
        "failures": failures,
        "items": items,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"saved {len(items)} items ({len(failures)} query failures)")
    total_feeds = len(config["queries"]) + len(config.get("rss_sources", []))
    if failures and len(failures) == total_feeds:
        raise SystemExit("all feed requests failed")


if __name__ == "__main__":
    main()
