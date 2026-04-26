#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

USER_AGENT = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Mobile Safari/537.36"
DEFAULT_TIMEOUT = 20
YOUTUBE_BASE = "https://www.youtube.com"
ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


class YouTubeFetchError(RuntimeError):
    pass


def request_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,ko-KR;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.reason
        try:
            body = exc.read().decode("utf-8", "replace")
            if body:
                detail = f"{detail}: {body[:220]}"
        except Exception:
            pass
        raise YouTubeFetchError(f"HTTP {exc.code} for {url} ({detail})") from exc
    except urllib.error.URLError as exc:
        raise YouTubeFetchError(f"Network error for {url}: {exc.reason}") from exc


def excerpt(text: Optional[str], limit: int = 220) -> str:
    if not text:
        return ""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def _extract_yt_initial_data(html: str) -> Dict[str, Any]:
    decoder = json.JSONDecoder()
    pattern = re.compile(r"(?:var\s+)?ytInitialData\s*=\s*")
    for match in pattern.finditer(html):
        start = match.end()
        while start < len(html) and html[start].isspace():
            start += 1
        if start >= len(html):
            continue
        if html[start] == "{":
            try:
                payload, _end = decoder.raw_decode(html[start:])
                return payload
            except json.JSONDecodeError:
                pass
        if html[start] in {"'", '"'}:
            quote = html[start]
            i = start + 1
            escaped = False
            while i < len(html):
                ch = html[i]
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    raw = html[start + 1 : i]
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore", DeprecationWarning)
                            decoded = bytes(raw, "utf-8").decode("unicode_escape")
                        return json.loads(decoded)
                    except Exception:
                        break
                i += 1
    raise YouTubeFetchError("Could not locate parseable ytInitialData JSON in YouTube response")


def _walk(node: Any):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _compact_runs(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(str(run.get("text", "")) for run in runs)
    return ""


def parse_search_results(html: str, limit: int) -> Dict[str, Any]:
    data = _extract_yt_initial_data(html)
    items: List[Dict[str, Any]] = []
    seen = set()
    for node in _walk(data):
        if not isinstance(node, dict):
            continue
        video = node.get("videoRenderer") or node.get("videoWithContextRenderer")
        if not isinstance(video, dict):
            continue
        video_id = video.get("videoId")
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        title = _compact_runs(video.get("title") or video.get("headline"))
        owner = _compact_runs(video.get("ownerText") or video.get("shortBylineText") or video.get("longBylineText"))
        published = _compact_runs(video.get("publishedTimeText"))
        views = _compact_runs(video.get("viewCountText") or video.get("shortViewCountText"))
        duration = _compact_runs(video.get("lengthText"))
        snippet = _compact_runs(video.get("detailedMetadataSnippets", [{}])[0].get("snippetText") if video.get("detailedMetadataSnippets") else video.get("descriptionSnippet"))
        channel_id = None
        byline = (video.get("ownerText") or video.get("shortBylineText") or {})
        nav = byline.get("runs", []) if isinstance(byline, dict) else []
        if nav:
            channel_id = (((nav[0] or {}).get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("browseId")
        items.append({
            "video_id": video_id,
            "title": title,
            "channel": owner,
            "channel_id": channel_id,
            "published": published,
            "views": views,
            "duration": duration,
            "url": f"{YOUTUBE_BASE}/watch?v={video_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "snippet": excerpt(snippet),
        })
        if len(items) >= limit:
            break
    return {
        "mode": "search",
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_search(query: str, limit: int) -> Dict[str, Any]:
    url = f"{YOUTUBE_BASE}/results?search_query={urllib.parse.quote(query)}"
    html = request_text(url)
    payload = parse_search_results(html, limit)
    payload["query"] = query
    return payload


def fetch_channel_feed(channel_id: str, limit: int) -> Dict[str, Any]:
    url = f"{YOUTUBE_BASE}/feeds/videos.xml?channel_id={urllib.parse.quote(channel_id)}"
    xml_text = request_text(url)
    root = ET.fromstring(xml_text)
    title = root.findtext("atom:title", default="", namespaces=ATOM_NS)
    author = root.find("atom:author", ATOM_NS)
    author_name = author.findtext("atom:name", default="", namespaces=ATOM_NS) if author is not None else ""
    items = []
    for entry in root.findall("atom:entry", ATOM_NS)[:limit]:
        video_id = entry.findtext("yt:videoId", default="", namespaces=ATOM_NS)
        item = {
            "video_id": video_id,
            "title": entry.findtext("atom:title", default="", namespaces=ATOM_NS),
            "published": entry.findtext("atom:published", default="", namespaces=ATOM_NS),
            "updated": entry.findtext("atom:updated", default="", namespaces=ATOM_NS),
            "author": entry.findtext("atom:author/atom:name", default=author_name, namespaces=ATOM_NS),
            "url": entry.findtext("atom:link", default=f"{YOUTUBE_BASE}/watch?v={video_id}", namespaces=ATOM_NS),
        }
        media_desc = entry.findtext("media:group/media:description", default="", namespaces=ATOM_NS)
        item["snippet"] = excerpt(media_desc)
        items.append(item)
    return {
        "mode": "channel",
        "channel_id": channel_id,
        "channel_title": title,
        "author": author_name,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_video(video_id: str) -> Dict[str, Any]:
    html = request_text(f"{YOUTUBE_BASE}/watch?v={urllib.parse.quote(video_id)}")
    title_m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    desc_m = re.search(r'<meta property="og:description" content="([^"]*)"', html)
    channel_m = re.search(r'"ownerChannelName":"([^"]*)"', html)
    channel_id_m = re.search(r'"channelId":"([^"]*)"', html)
    return {
        "mode": "video",
        "video": {
            "video_id": video_id,
            "title": title_m.group(1) if title_m else "",
            "channel": channel_m.group(1) if channel_m else "",
            "channel_id": channel_id_m.group(1) if channel_id_m else "",
            "url": f"{YOUTUBE_BASE}/watch?v={video_id}",
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            "snippet": excerpt(desc_m.group(1) if desc_m else ""),
        },
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def render_video(item: Dict[str, Any]) -> str:
    lines = [
        f"- {item.get('title')}",
        f"  channel: {item.get('channel') or item.get('author')} | views: {item.get('views') or '-'} | published: {item.get('published') or item.get('updated')}",
        f"  url: {item.get('url')}",
    ]
    if item.get("snippet"):
        lines.append(f"  text: {item.get('snippet')}")
    return "\n".join(lines)


def emit_text(payload: Dict[str, Any]) -> str:
    if payload.get("mode") == "video":
        return render_video(payload.get("video", {}))
    if payload.get("mode") in {"search", "channel"}:
        header = f"youtube {payload.get('mode')} query={payload.get('query') or '-'} channel={payload.get('channel_id') or '-'} count={payload.get('count')}"
        body = "\n\n".join(render_video(item) for item in payload.get("items", [])) or "(no videos)"
        return f"{header}\n\n{body}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "text"), default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch YouTube search results, channel feeds, and basic video metadata.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    sub = parser.add_subparsers(dest="command", required=True)

    search = sub.add_parser("search")
    add_format_arg(search)
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=10)

    channel = sub.add_parser("channel")
    add_format_arg(channel)
    channel.add_argument("channel_id")
    channel.add_argument("--limit", type=int, default=10)

    video = sub.add_parser("video")
    add_format_arg(video)
    video.add_argument("video_id")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "search":
            payload = fetch_search(args.query, args.limit)
        elif args.command == "channel":
            payload = fetch_channel_feed(args.channel_id, args.limit)
        elif args.command == "video":
            payload = fetch_video(args.video_id)
        else:
            raise YouTubeFetchError(f"Unknown command: {args.command}")
    except (YouTubeFetchError, ET.ParseError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    output_format = getattr(args, "format", None) or "json"
    if output_format == "text":
        print(emit_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
