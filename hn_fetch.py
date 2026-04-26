#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

HN_BASE = "https://hacker-news.firebaseio.com/v0"
ALGOLIA_BASE = "https://hn.algolia.com/api/v1"
USER_AGENT = "gongyung-hn-cli/0.1"
DEFAULT_TIMEOUT = 20
LIST_TYPES = {"top", "new", "best", "ask", "show", "job"}


class HNFetchError(RuntimeError):
    pass


def request_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.reason
        try:
            body = exc.read().decode("utf-8", "replace")
            if body:
                detail = f"{detail}: {body[:220]}"
        except Exception:
            pass
        raise HNFetchError(f"HTTP {exc.code} for {url} ({detail})") from exc
    except urllib.error.URLError as exc:
        raise HNFetchError(f"Network error for {url}: {exc.reason}") from exc


def utc_iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def excerpt(text: Optional[str], limit: int = 220) -> str:
    if not text:
        return ""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def normalize_item(item: Dict[str, Any]) -> Dict[str, Any]:
    item_id = item.get("id") or item.get("objectID")
    item_type = item.get("type")
    if not item_type:
        tags = item.get("_tags") or []
        item_type = tags[0] if tags else None
    url = item.get("url") or (f"https://news.ycombinator.com/item?id={item.get('story_id')}" if item.get("story_id") else None)
    return {
        "id": item_id,
        "type": item_type,
        "title": item.get("title") or item.get("story_title"),
        "author": item.get("by") or item.get("author"),
        "score": item.get("score") or item.get("points"),
        "url": url,
        "text_excerpt": excerpt(item.get("text") or item.get("comment_text") or item.get("story_text")),
        "created_at": utc_iso(item.get("time")) or item.get("created_at"),
        "descendants": item.get("descendants") if item.get("descendants") is not None else item.get("num_comments"),
        "parent": item.get("parent") or item.get("story_id"),
        "hn_url": f"https://news.ycombinator.com/item?id={item_id}",
    }


def fetch_item(item_id: int) -> Dict[str, Any]:
    payload = request_json(f"{HN_BASE}/item/{item_id}.json")
    return {
        "mode": "item",
        "item": normalize_item(payload),
        "raw": payload,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_list(list_type: str, limit: int) -> Dict[str, Any]:
    if list_type not in LIST_TYPES:
        raise HNFetchError(f"Invalid list type: {list_type}")
    endpoint = {
        "top": "topstories",
        "new": "newstories",
        "best": "beststories",
        "ask": "askstories",
        "show": "showstories",
        "job": "jobstories",
    }[list_type]
    ids = request_json(f"{HN_BASE}/{endpoint}.json")[:limit]
    items = []
    for item_id in ids:
        raw = request_json(f"{HN_BASE}/item/{item_id}.json")
        items.append(normalize_item(raw))
    return {
        "mode": "list",
        "list": list_type,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def search(query: str, tag: Optional[str], limit: int, sort: str) -> Dict[str, Any]:
    path = "/search" if sort == "relevance" else "/search_by_date"
    params = {"query": query, "hitsPerPage": limit}
    if tag:
        params["tags"] = tag
    url = f"{ALGOLIA_BASE}{path}?{urllib.parse.urlencode(params)}"
    payload = request_json(url)
    items = [normalize_item(hit) for hit in payload.get("hits", [])]
    return {
        "mode": "search",
        "query": query,
        "tag": tag,
        "sort": sort,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def render_item(item: Dict[str, Any]) -> str:
    lines = [
        f"- [{item.get('type')}] {item.get('title') or '(no title)'}",
        f"  id: {item.get('id')} | author: {item.get('author')} | score: {item.get('score')} | comments: {item.get('descendants')}",
        f"  created: {item.get('created_at')}",
        f"  url: {item.get('url') or '-'}",
        f"  hn: {item.get('hn_url')}",
    ]
    if item.get("text_excerpt"):
        lines.append(f"  text: {item.get('text_excerpt')}")
    return "\n".join(lines)


def emit_text(payload: Dict[str, Any]) -> str:
    if payload.get("mode") == "item":
        return render_item(payload.get("item", {}))
    if payload.get("mode") == "list":
        header = f"hn {payload.get('list')} count={payload.get('count')}"
        body = "\n\n".join(render_item(item) for item in payload.get("items", [])) or "(no items)"
        return f"{header}\n\n{body}"
    if payload.get("mode") == "search":
        header = f"hn search query={payload.get('query')!r} tag={payload.get('tag') or '-'} sort={payload.get('sort')} count={payload.get('count')}"
        body = "\n\n".join(render_item(item) for item in payload.get("items", [])) or "(no hits)"
        return f"{header}\n\n{body}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "text"), default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch Hacker News stories and search results.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    add_format_arg(list_parser)
    list_parser.add_argument("kind", choices=sorted(LIST_TYPES))
    list_parser.add_argument("--limit", type=int, default=10)

    item_parser = sub.add_parser("item")
    add_format_arg(item_parser)
    item_parser.add_argument("id", type=int)

    search_parser = sub.add_parser("search")
    add_format_arg(search_parser)
    search_parser.add_argument("query")
    search_parser.add_argument("--tag", choices=("story", "comment", "poll", "pollopt", "job", "ask_hn", "show_hn", "front_page"))
    search_parser.add_argument("--sort", choices=("relevance", "new"), default="new")
    search_parser.add_argument("--limit", type=int, default=10)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "list":
            payload = fetch_list(args.kind, args.limit)
        elif args.command == "item":
            payload = fetch_item(args.id)
        elif args.command == "search":
            payload = search(args.query, args.tag, args.limit, args.sort)
        else:
            raise HNFetchError(f"Unknown command: {args.command}")
    except HNFetchError as exc:
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
