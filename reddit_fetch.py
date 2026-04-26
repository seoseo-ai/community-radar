#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

USER_AGENT = "gongyung-reddit-cli/0.1"
BASE = "https://www.reddit.com"
DEFAULT_TIMEOUT = 20
VALID_LIST_SORTS = {"hot", "new", "top", "rising", "controversial"}
VALID_SEARCH_SORTS = {"relevance", "hot", "top", "new", "comments"}
VALID_COMMENT_SORTS = {"confidence", "top", "new", "controversial", "old", "qa"}
VALID_TIME = {"hour", "day", "week", "month", "year", "all"}
ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


class RedditFetchError(RuntimeError):
    pass


def request(url: str):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
        },
    )
    try:
        return urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT)
    except urllib.error.HTTPError as exc:
        detail = exc.reason
        try:
            body = exc.read().decode("utf-8", "replace")
            if body:
                detail = f"{detail}: {body[:220]}"
        except Exception:
            pass
        raise RedditFetchError(f"HTTP {exc.code} for {url} ({detail})") from exc
    except urllib.error.URLError as exc:
        raise RedditFetchError(f"Network error for {url}: {exc.reason}") from exc


def fetch_json(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = dict(params or {})
    url = f"{BASE}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    with request(url) as resp:
        return json.load(resp)


def fetch_bytes(path: str, params: Optional[Dict[str, Any]] = None) -> bytes:
    query = dict(params or {})
    url = f"{BASE}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    with request(url) as resp:
        return resp.read()


def utc_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def excerpt(text: Optional[str], limit: int = 220) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def normalize_submission(post: Dict[str, Any]) -> Dict[str, Any]:
    permalink = post.get("permalink") or ""
    return {
        "id": post.get("id"),
        "fullname": post.get("name"),
        "title": post.get("title"),
        "subreddit": post.get("subreddit"),
        "author": post.get("author"),
        "score": post.get("score"),
        "num_comments": post.get("num_comments"),
        "created_utc": post.get("created_utc"),
        "created_iso": utc_iso(post.get("created_utc")),
        "permalink": permalink,
        "url": post.get("url"),
        "selftext": post.get("selftext") or "",
        "selftext_excerpt": excerpt(post.get("selftext")),
        "over_18": post.get("over_18"),
        "spoiler": post.get("spoiler"),
        "stickied": post.get("stickied"),
    }


def flatten_comments(children: Iterable[Dict[str, Any]], *, limit: Optional[int] = None, depth: int = 0) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for child in children:
        if child.get("kind") != "t1":
            continue
        data = child.get("data", {})
        permalink = data.get("permalink") or ""
        item = {
            "id": data.get("id"),
            "fullname": data.get("name"),
            "author": data.get("author"),
            "score": data.get("score"),
            "created_utc": data.get("created_utc"),
            "created_iso": utc_iso(data.get("created_utc")),
            "body": data.get("body") or "",
            "body_excerpt": excerpt(data.get("body")),
            "depth": depth,
            "permalink": permalink,
        }
        out.append(item)
        if limit is not None and len(out) >= limit:
            return out[:limit]
        replies = data.get("replies")
        if isinstance(replies, dict):
            nested = flatten_comments(replies.get("data", {}).get("children", []), limit=None if limit is None else max(limit - len(out), 0), depth=depth + 1)
            out.extend(nested)
            if limit is not None and len(out) >= limit:
                return out[:limit]
    return out


def extract_post_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9]{5,8}", value):
        return value
    match = re.search(r"/comments/([A-Za-z0-9]+)/", value)
    if match:
        return match.group(1)
    match = re.search(r"comments/([A-Za-z0-9]+)", value)
    if match:
        return match.group(1)
    raise RedditFetchError(f"Could not extract Reddit post id from: {value}")


def parse_feed_entries(xml_bytes: bytes) -> List[Dict[str, str]]:
    root = ET.fromstring(xml_bytes)
    out: List[Dict[str, str]] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = (entry.findtext("atom:title", default="", namespaces=ATOM_NS) or "").strip()
        updated = (entry.findtext("atom:updated", default="", namespaces=ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:content", default="", namespaces=ATOM_NS) or "").strip()
        link = ""
        for node in entry.findall("atom:link", ATOM_NS):
            href = node.attrib.get("href")
            rel = node.attrib.get("rel", "alternate")
            if href and rel == "alternate":
                link = href
                break
            if href and not link:
                link = href
        out.append({
            "title": title,
            "updated": updated,
            "summary": excerpt(summary, 320),
            "link": link,
        })
    return out


def fetch_listing(subreddit: str, sort: str, limit: int, time_filter: str) -> Dict[str, Any]:
    if sort not in VALID_LIST_SORTS:
        raise RedditFetchError(f"Invalid sort: {sort}")
    if time_filter not in VALID_TIME:
        raise RedditFetchError(f"Invalid time filter: {time_filter}")
    params: Dict[str, Any] = {"limit": limit}
    if sort in {"top", "controversial"}:
        params["t"] = time_filter
    data = fetch_json(f"/r/{urllib.parse.quote(subreddit)}/{sort}.json", params)
    posts = [normalize_submission(child.get("data", {})) for child in data.get("data", {}).get("children", []) if child.get("kind") == "t3"]
    return {
        "mode": "subreddit",
        "subreddit": subreddit,
        "sort": sort,
        "time": time_filter,
        "count": len(posts),
        "items": posts,
        "fetched_at": utc_iso(time.time()),
    }


def fetch_search(query: str, subreddit: Optional[str], sort: str, limit: int, time_filter: str) -> Dict[str, Any]:
    if sort not in VALID_SEARCH_SORTS:
        raise RedditFetchError(f"Invalid search sort: {sort}")
    if time_filter not in VALID_TIME:
        raise RedditFetchError(f"Invalid time filter: {time_filter}")
    params: Dict[str, Any] = {
        "q": query,
        "sort": sort,
        "t": time_filter,
    }
    if subreddit:
        path = f"/r/{urllib.parse.quote(subreddit)}/search.rss"
        params["restrict_sr"] = "on"
    else:
        path = "/search.rss"

    entries = parse_feed_entries(fetch_bytes(path, params))[:limit]
    items: List[Dict[str, Any]] = []
    for entry in entries:
        link = entry.get("link") or ""
        try:
            post = fetch_post(link)["post"]
            post["search_summary"] = entry.get("summary") or ""
            post["search_updated"] = entry.get("updated") or ""
            items.append(post)
        except RedditFetchError:
            items.append({
                "id": extract_post_id(link) if "/comments/" in link else None,
                "title": entry.get("title"),
                "subreddit": subreddit,
                "author": None,
                "score": None,
                "num_comments": None,
                "created_utc": None,
                "created_iso": None,
                "permalink": link.replace("https://www.reddit.com", "") if link.startswith("https://www.reddit.com") else link,
                "url": link,
                "selftext": "",
                "selftext_excerpt": entry.get("summary") or "",
                "search_summary": entry.get("summary") or "",
                "search_updated": entry.get("updated") or "",
            })
    return {
        "mode": "search",
        "query": query,
        "subreddit": subreddit,
        "sort": sort,
        "time": time_filter,
        "count": len(items),
        "items": items,
        "fetched_at": utc_iso(time.time()),
    }


def fetch_post(post_ref: str) -> Dict[str, Any]:
    post_id = extract_post_id(post_ref)
    data = fetch_json(f"/comments/{post_id}/.json", {"limit": 1})
    try:
        post = normalize_submission(data[0]["data"]["children"][0]["data"])
    except Exception as exc:
        raise RedditFetchError(f"Unexpected post payload for {post_ref}") from exc
    return {
        "mode": "post",
        "post": post,
        "fetched_at": utc_iso(time.time()),
    }


def fetch_comments(post_ref: str, sort: str, limit: int) -> Dict[str, Any]:
    if sort not in VALID_COMMENT_SORTS:
        raise RedditFetchError(f"Invalid comment sort: {sort}")
    post_id = extract_post_id(post_ref)
    data = fetch_json(f"/comments/{post_id}/.json", {"sort": sort, "limit": limit})
    try:
        post = normalize_submission(data[0]["data"]["children"][0]["data"])
        comments_listing = data[1]["data"]["children"]
    except Exception as exc:
        raise RedditFetchError(f"Unexpected comment payload for {post_ref}") from exc
    comments = flatten_comments(comments_listing, limit=limit)
    return {
        "mode": "comments",
        "sort": sort,
        "count": len(comments),
        "post": post,
        "items": comments,
        "fetched_at": utc_iso(time.time()),
    }


def render_submission_text(post: Dict[str, Any]) -> str:
    lines = [
        f"- r/{post.get('subreddit')} | {post.get('title')}",
        f"  id: {post.get('id')} | author: u/{post.get('author')} | score: {post.get('score')} | comments: {post.get('num_comments')}",
        f"  created: {post.get('created_iso')}",
        f"  url: {post.get('url')}",
        f"  permalink: https://www.reddit.com{post.get('permalink') or ''}",
    ]
    if post.get("selftext_excerpt"):
        lines.append(f"  text: {post['selftext_excerpt']}")
    return "\n".join(lines)


def render_comment_text(comment: Dict[str, Any]) -> str:
    indent = "  " * int(comment.get("depth") or 0)
    return (
        f"- depth={comment.get('depth')} | u/{comment.get('author')} | score={comment.get('score')} | {comment.get('created_iso')}\n"
        f"  {indent}{comment.get('body_excerpt')}\n"
        f"  link: https://www.reddit.com{comment.get('permalink') or ''}"
    )


def emit_text(payload: Dict[str, Any]) -> str:
    mode = payload.get("mode")
    if mode == "subreddit":
        header = f"subreddit=r/{payload.get('subreddit')} sort={payload.get('sort')} time={payload.get('time')} count={payload.get('count')}"
        body = "\n\n".join(render_submission_text(item) for item in payload.get("items", [])) or "(no posts)"
        return f"{header}\n\n{body}"
    if mode == "search":
        scope = f"r/{payload['subreddit']}" if payload.get("subreddit") else "all"
        header = f"search query={payload.get('query')!r} scope={scope} sort={payload.get('sort')} time={payload.get('time')} count={payload.get('count')}"
        body = "\n\n".join(render_submission_text(item) for item in payload.get("items", [])) or "(no posts)"
        return f"{header}\n\n{body}"
    if mode == "post":
        return render_submission_text(payload.get("post", {}))
    if mode == "comments":
        header = render_submission_text(payload.get("post", {}))
        body = "\n\n".join(render_comment_text(item) for item in payload.get("items", [])) or "(no comments)"
        return f"{header}\n\ncomments sort={payload.get('sort')} count={payload.get('count')}\n\n{body}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "text"), default=None, help="Output format")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch public Reddit data from the CLI for agent use.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output format")
    sub = parser.add_subparsers(dest="command", required=True)

    subreddit = sub.add_parser("subreddit", help="Fetch a subreddit listing")
    add_format_arg(subreddit)
    subreddit.add_argument("name", help="Subreddit name, without r/")
    subreddit.add_argument("--sort", default="hot", choices=sorted(VALID_LIST_SORTS))
    subreddit.add_argument("--time", default="day", choices=sorted(VALID_TIME))
    subreddit.add_argument("--limit", type=int, default=10)

    search = sub.add_parser("search", help="Search Reddit posts")
    add_format_arg(search)
    search.add_argument("query", help="Search query")
    search.add_argument("--subreddit", help="Restrict search to a subreddit")
    search.add_argument("--sort", default="relevance", choices=sorted(VALID_SEARCH_SORTS))
    search.add_argument("--time", default="week", choices=sorted(VALID_TIME))
    search.add_argument("--limit", type=int, default=10)

    post = sub.add_parser("post", help="Fetch one Reddit post by URL or id")
    add_format_arg(post)
    post.add_argument("ref", help="Reddit post URL or base36 id")

    comments = sub.add_parser("comments", help="Fetch comments for a Reddit post by URL or id")
    add_format_arg(comments)
    comments.add_argument("ref", help="Reddit post URL or base36 id")
    comments.add_argument("--sort", default="top", choices=sorted(VALID_COMMENT_SORTS))
    comments.add_argument("--limit", type=int, default=20)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    output_format = getattr(args, "format", None) or "json"

    try:
        if args.command == "subreddit":
            payload = fetch_listing(args.name, args.sort, args.limit, args.time)
        elif args.command == "search":
            payload = fetch_search(args.query, args.subreddit, args.sort, args.limit, args.time)
        elif args.command == "post":
            payload = fetch_post(args.ref)
        elif args.command == "comments":
            payload = fetch_comments(args.ref, args.sort, args.limit)
        else:
            raise RedditFetchError(f"Unknown command: {args.command}")
    except RedditFetchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if output_format == "text":
        print(emit_text(payload))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
