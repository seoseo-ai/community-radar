#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

WORKSPACE = Path(__file__).resolve().parent.parent
SCRIPTS = WORKSPACE / "scripts"
STATE_FILE = Path(__file__).resolve().parent / "state.json"
WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"
DEFAULT_SOURCES = ["reddit", "dc", "github", "hn", "youtube", "searxng"]
ALL_SOURCES = DEFAULT_SOURCES + ["searxng", "discord"]
DESCRIPTION = "Aggregate community sentiment/signals across non-Discord sources by default, with optional Discord support."
DEFAULT_TIMEOUT = 45
GENERIC_HOT_QUERIES = {
    "hot", "trending", "trend", "news", "realtime", "real-time", "issues",
    "핫", "핫이슈", "실시간", "트렌드", "이슈", "전체", "종합",
}
SOURCE_BASE_WEIGHTS = {
    "reddit": 1200,
    "dc": 1150,
    "github": 950,
    "hn": 1250,
    "youtube": 900,
    "searxng": 800,
    "discord": 700,
}

SEARXNG_BASE_URL = "https://vps4.tail1546e7.ts.net:18443"
DEFAULT_HOT_SUBREDDITS = ["technology", "worldnews", "programming", "singularity"]
DEFAULT_BUCKET_PREVIEW = 3
PRESET_CONFIGS = {
    "general-news": {
        "query": "hot",
        "sources": ["reddit", "dc", "hn", "youtube"],
        "bucket": None,
    },
    "agent-news": {
        "query": "OpenClaw",
        "sources": ["reddit", "dc", "github", "hn", "youtube"],
        "bucket": None,
    },
}


class AggregateError(RuntimeError):
    pass


def run_json(args: List[str], timeout: int = DEFAULT_TIMEOUT) -> Any:
    proc = subprocess.run(args, cwd=WORKSPACE, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise AggregateError((proc.stderr or proc.stdout or f"command failed: {' '.join(args)}").strip())
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AggregateError(f"Invalid JSON from {' '.join(args)}: {exc}") from exc


def excerpt(text: Optional[str], limit: int = 220) -> str:
    if not text:
        return ""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None


def load_state() -> Dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def update_state_for_source(state: Dict[str, Any], source: str, items: List[Dict[str, Any]]) -> None:
    if not items:
        return
    latest_published = ""
    latest_id = ""
    for item in items:
        pub = item.get("published") or ""
        if pub > latest_published:
            latest_published = pub
        item_url = item.get("url") or ""
        if item_url > latest_id:
            latest_id = item_url
    state[source] = {
        "last_seen_id": latest_id,
        "last_seen_ts": latest_published,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def filter_incremental(items: List[Dict[str, Any]], source: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    source_state = state.get(source) or {}
    last_ts = source_state.get("last_seen_ts") or ""
    last_id = source_state.get("last_seen_id") or ""
    if not last_ts and not last_id:
        return items
    filtered = []
    for item in items:
        pub = item.get("published") or ""
        url = item.get("url") or ""
        if last_ts and pub and pub > last_ts:
            filtered.append(item)
        elif last_id and url and url != last_id and pub and pub >= last_ts:
            filtered.append(item)
        elif not last_ts and url and url != last_id:
            filtered.append(item)
    return filtered


def load_watchlist() -> Dict[str, Any]:
    if WATCHLIST_FILE.exists():
        try:
            return json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"keywords": [], "galleries": [], "subreddits": []}


def matches_watchlist(item: Dict[str, Any], watchlist: Dict[str, Any]) -> List[str]:
    matches = []
    keywords = watchlist.get("keywords") or []
    galleries = watchlist.get("galleries") or []
    subreddits = watchlist.get("subreddits") or []
    text = " ".join(filter(None, [
        item.get("title") or "",
        item.get("summary") or "",
        (item.get("extra") or {}).get("gallery") or "",
        (item.get("extra") or {}).get("gallery_name") or "",
        (item.get("extra") or {}).get("subreddit") or "",
    ])).lower()
    for kw in keywords:
        if kw.lower() in text:
            matches.append(f"keyword:{kw}")
    extra = item.get("extra") or {}
    item_gallery = (extra.get("gallery") or extra.get("source_gallery_id") or "").lower()
    for g in galleries:
        if g.lower() == item_gallery:
            matches.append(f"gallery:{g}")
    item_sub = (extra.get("subreddit") or "").lower()
    for s in subreddits:
        if s.lower() == item_sub:
            matches.append(f"subreddit:{s}")
    return matches


def to_item(source: str, kind: str, title: str, url: str, *, author: Optional[str] = None, published: Optional[str] = None,
            summary: Optional[str] = None, score: Optional[int] = None, comments: Optional[int] = None,
            extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "source": source,
        "kind": kind,
        "title": title,
        "url": url,
        "author": author,
        "published": published,
        "summary": excerpt(summary),
        "score": score,
        "comments": comments,
        "extra": extra or {},
    }


def _dc_rank(row: Dict[str, Any]) -> int:
    score = as_int(row.get("recommend")) or 0
    hits = as_int(row.get("hits")) or 0
    comments = as_int(row.get("comments")) or 0
    return score * 12000 + comments * 350 + hits


def _normalize_dc_published(date_str: str) -> str:
    """Convert DC short date/time to ISO format for proper sorting."""
    if not date_str:
        return ""
    date_str = date_str.strip()
    # Already ISO?
    if len(date_str) >= 10 and date_str[4] == "-":
        return date_str
    # Time-only like "12:40" → today in KST
    if re.match(r"^\d{1,2}:\d{2}$", date_str):
        from datetime import datetime, timezone, timedelta
        KST = timezone(timedelta(hours=9))
        now_kst = datetime.now(KST)
        h, m = date_str.split(":")
        try:
            dt = now_kst.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            return dt.isoformat()
        except ValueError:
            return date_str
    return date_str


def _log_score(value: int, factor: int = 100) -> int:
    if value <= 0:
        return 0
    return int(math.log10(value + 1) * factor)


def _universal_rank(item: Dict[str, Any], query: Optional[str] = None) -> int:
    source = item.get("source") or ""
    kind = item.get("kind") or ""
    extra = item.get("extra") or {}
    source_rank = as_int(extra.get("source_rank")) or 0
    score = item.get("score") or 0
    comments = item.get("comments") or 0
    base = SOURCE_BASE_WEIGHTS.get(source, 1000)
    if source == "dc":
        rank = base + _log_score(source_rank, 220)
    elif source == "reddit":
        rank = base + _log_score(score, 180) + _log_score(comments, 120)
    elif source == "hn":
        rank = base + _log_score(score, 180) + _log_score(comments, 130)
    elif source == "github":
        rank = base + _log_score(score, 140) + _log_score(comments, 110)
    elif source == "youtube":
        rank = base + _log_score(score, 120) + _log_score(comments, 80)
    elif source == "discord":
        rank = base + _log_score(comments, 90)
    else:
        rank = base + _log_score(score, 120) + _log_score(comments, 90)

    q = (query or "").strip().lower()
    if q in GENERIC_HOT_QUERIES:
        if source == "github" and kind == "repo":
            rank -= 180
        if source == "hn" and kind == "comment":
            rank -= 160
        if source == "youtube":
            rank -= 60
    return rank


def _sort_rank(item: Dict[str, Any], query: Optional[str] = None) -> tuple:
    published = item.get("published") or ""
    score = item.get("score") or 0
    comments = item.get("comments") or 0
    return (_universal_rank(item, query=query), published, score, comments)


def _is_generic_hot_query(query: str) -> bool:
    return query.strip().lower() in GENERIC_HOT_QUERIES


def _recent_date_query(days: int = 7) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d")


def collect_reddit(query: str, limit: int, subreddit: Optional[str]) -> List[Dict[str, Any]]:
    items = []
    if _is_generic_hot_query(query):
        subreddits = [subreddit] if subreddit else DEFAULT_HOT_SUBREDDITS
        per_sub_limit = max(1, limit)
        seen_urls = set()
        for name in subreddits:
            payload = run_json([
                "python3", str(SCRIPTS / "reddit_fetch.py"), "subreddit", name,
                "--sort", "hot", "--limit", str(per_sub_limit),
            ])
            for row in payload.get("items", []):
                url = row.get("url") or f"https://www.reddit.com{row.get('permalink') or ''}"
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                items.append(to_item(
                    "reddit",
                    "post",
                    row.get("title") or "(no title)",
                    url,
                    author=row.get("author"),
                    published=row.get("created_iso") or row.get("search_updated"),
                    summary=row.get("selftext_excerpt") or row.get("search_summary"),
                    score=as_int(row.get("score")),
                    comments=as_int(row.get("num_comments")),
                    extra={"subreddit": row.get("subreddit")},
                ))
            
    else:
        args = ["python3", str(SCRIPTS / "reddit_fetch.py"), "search", query, "--limit", str(limit)]
        if subreddit:
            args.extend(["--subreddit", subreddit])
        payload = run_json(args)
        for row in payload.get("items", []):
            items.append(to_item(
                "reddit",
                "post",
                row.get("title") or "(no title)",
                row.get("url") or f"https://www.reddit.com{row.get('permalink') or ''}",
                author=row.get("author"),
                published=row.get("created_iso") or row.get("search_updated"),
                summary=row.get("selftext_excerpt") or row.get("search_summary"),
                score=as_int(row.get("score")),
                comments=as_int(row.get("num_comments")),
                extra={"subreddit": row.get("subreddit")},
            ))
    return items


def _should_use_dc_best(query: str, mode: str, gallery: Optional[str]) -> bool:
    if gallery:
        return False
    if mode == "best":
        return True
    if mode == "search":
        return False
    return query.strip().lower() in GENERIC_HOT_QUERIES


def collect_dc(query: str, limit: int, gallery: Optional[str], mode: str = "auto") -> List[Dict[str, Any]]:
    use_best = _should_use_dc_best(query, mode, gallery)
    if use_best:
        payload = run_json(["python3", str(SCRIPTS / "dc_fetch.py"), "best", "--limit", str(limit)])
    elif mode == "popular" and gallery:
        payload = run_json(["python3", str(SCRIPTS / "dc_fetch.py"), "gallery", gallery, "--sort", "popular", "--limit", str(limit)])
    else:
        args = ["python3", str(SCRIPTS / "dc_fetch.py"), "search", query, "--limit", str(limit)]
        if gallery:
            args.extend(["--gallery", gallery])
        payload = run_json(args)
    items = []
    for row in payload.get("posts", []):
        items.append(to_item(
            "dc",
            "post",
            row.get("title") or "(no title)",
            row.get("source_post_url") or row.get("url") or row.get("desktop_url") or "",
            author=row.get("writer"),
            published=_normalize_dc_published(row.get("date")),
            summary=row.get("subject") or row.get("title"),
            comments=as_int(row.get("comments")),
            score=as_int(row.get("recommend")),
            extra={
                "gallery": gallery or row.get("gallery_id"),
                "gallery_name": row.get("gallery_name"),
                "hits": row.get("hits"),
                "source_gallery_name": row.get("source_gallery_name"),
                "source_gallery_hint": row.get("source_gallery_hint"),
                "source_gallery_id": row.get("source_gallery_id"),
                "source_post_url": row.get("source_post_url"),
                "source_rank": _dc_rank(row),
            },
        ))
    items.sort(key=lambda item: ((item.get("extra") or {}).get("source_rank") or 0, item.get("published") or ""), reverse=True)
    return items


def collect_github(query: str, limit: int, repo: Optional[str]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if repo:
        payload = run_json(["python3", str(SCRIPTS / "github_fetch.py"), "issues", repo, "--limit", str(limit)])
        for row in payload.get("items", []):
            text = row.get("body_excerpt") or ""
            title = row.get("title") or "(no title)"
            if query.lower() in (title + " " + text).lower():
                items.append(to_item(
                    "github",
                    "pr" if row.get("pull_request") else "issue",
                    title,
                    row.get("url") or "",
                    author=row.get("author"),
                    published=row.get("updated_at") or row.get("created_at"),
                    summary=text,
                    comments=as_int(row.get("comments")),
                    extra={"labels": row.get("labels") or [], "repo": repo},
                ))
    else:
        if _is_generic_hot_query(query):
            issue_query = f"updated:>{_recent_date_query(3)} comments:>20"
            issue_payload = run_json(["python3", str(SCRIPTS / "github_fetch.py"), "search-issues", issue_query, "--limit", str(limit)])
            for row in issue_payload.get("items", []):
                items.append(to_item(
                    "github",
                    "pr" if row.get("pull_request") else "issue",
                    row.get("title") or "(no title)",
                    row.get("url") or "",
                    author=row.get("author"),
                    published=row.get("updated_at") or row.get("created_at"),
                    summary=row.get("body_excerpt"),
                    comments=as_int(row.get("comments")),
                    extra={"labels": row.get("labels") or [], "repo": row.get("repo")},
                ))
            hot_query = f"topic:opensource pushed:>{_recent_date_query(7)} stars:>1000"
            repo_payload = run_json(["python3", str(SCRIPTS / "github_fetch.py"), "search-repos", hot_query, "--sort", "updated", "--limit", str(max(1, limit // 2))])
            for row in repo_payload.get("items", []):
                items.append(to_item(
                    "github",
                    "repo",
                    row.get("full_name") or row.get("name") or "(no name)",
                    row.get("url") or "",
                    published=row.get("updated_at") or row.get("pushed_at"),
                    summary=row.get("description"),
                    score=as_int(row.get("stars")),
                    comments=as_int(row.get("open_issues")),
                    extra={"language": row.get("language"), "topics": row.get("topics") or []},
                ))
        else:
            payload = run_json(["python3", str(SCRIPTS / "github_fetch.py"), "search-issues", query, "--limit", str(limit)])
            for row in payload.get("items", []):
                items.append(to_item(
                    "github",
                    "pr" if row.get("pull_request") else "issue",
                    row.get("title") or "(no title)",
                    row.get("url") or "",
                    author=row.get("author"),
                    published=row.get("updated_at") or row.get("created_at"),
                    summary=row.get("body_excerpt"),
                    comments=as_int(row.get("comments")),
                    extra={"labels": row.get("labels") or []},
                ))
            repo_payload = run_json(["python3", str(SCRIPTS / "github_fetch.py"), "search-repos", query, "--limit", str(limit)])
            for row in repo_payload.get("items", []):
                items.append(to_item(
                    "github",
                    "repo",
                    row.get("full_name") or row.get("name") or "(no name)",
                    row.get("url") or "",
                    published=row.get("updated_at") or row.get("pushed_at"),
                    summary=row.get("description"),
                    score=as_int(row.get("stars")),
                    comments=as_int(row.get("open_issues")),
                    extra={"language": row.get("language"), "topics": row.get("topics") or []},
                ))
    return items[:limit * 2]


def collect_hn(query: str, limit: int) -> List[Dict[str, Any]]:
    if _is_generic_hot_query(query):
        payload = run_json(["python3", str(SCRIPTS / "hn_fetch.py"), "list", "top", "--limit", str(limit)])
    else:
        payload = run_json(["python3", str(SCRIPTS / "hn_fetch.py"), "search", query, "--limit", str(limit), "--sort", "new"])
    items = []
    for row in payload.get("items", []):
        items.append(to_item(
            "hn",
            row.get("type") or "story",
            row.get("title") or "(no title)",
            row.get("url") or row.get("hn_url") or "",
            author=row.get("author"),
            published=row.get("created_at"),
            summary=row.get("text_excerpt"),
            score=as_int(row.get("score")),
            comments=as_int(row.get("descendants")),
            extra={"hn_url": row.get("hn_url")},
        ))
    return items


def collect_youtube(query: str, limit: int) -> List[Dict[str, Any]]:
    yt_query = "tech news" if _is_generic_hot_query(query) else query
    payload = run_json(["python3", str(SCRIPTS / "youtube_fetch.py"), "search", yt_query, "--limit", str(limit)])
    items = []
    for row in payload.get("items", []):
        items.append(to_item(
            "youtube",
            "video",
            row.get("title") or "(no title)",
            row.get("url") or "",
            author=row.get("channel"),
            published=row.get("published"),
            summary=row.get("snippet"),
            score=as_int(row.get("views")),
            extra={"channel_id": row.get("channel_id"), "duration": row.get("duration")},
        ))
    return items


def collect_discord(limit: int, channels: List[str]) -> tuple[List[Dict[str, Any]], List[str]]:
    items: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for channel_id in channels:
        payload = run_json([
            "python3", str(SCRIPTS / "discord_fetch.py"), "--allow-missing-token", "messages", channel_id, "--limit", str(limit),
        ])
        if payload.get("warning"):
            warnings.append(f"channel {channel_id}: {payload['warning']}")
        for row in payload.get("items", []):
            items.append(to_item(
                "discord",
                "message",
                excerpt(row.get("content") or row.get("content_excerpt") or "(empty)", 80),
                f"discord://channel/{channel_id}/message/{row.get('id')}",
                author=row.get("author"),
                published=row.get("timestamp"),
                summary=row.get("content_excerpt") or row.get("content"),
                comments=None,
                extra={"channel_id": channel_id, "author_id": row.get("author_id")},
            ))
    return items, warnings


def collect_searxng(query: str, limit: int, categories: Optional[str] = None) -> List[Dict[str, Any]]:
    """Collect results from SearXNG meta-search engine."""
    import urllib.request
    import urllib.parse
    import ssl

    if not query or query.strip().lower() in GENERIC_HOT_QUERIES:
        return []  # SearXNG needs a concrete query, skip generic hot queries

    params = {"q": query, "format": "json", "pageno": 1}
    if categories:
        params["categories"] = categories

    url = f"{SEARXNG_BASE_URL}/search?{urllib.parse.urlencode(params)}"
    ctx = ssl.create_default_context()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "community-aggregate/1.0"})
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        raise AggregateError(f"SearXNG fetch failed: {exc}")

    items: List[Dict[str, Any]] = []
    for row in data.get("results", [])[:limit]:
        engines = ", ".join(row.get("engines", []))
        items.append(to_item(
            "searxng",
            row.get("category", "general"),
            excerpt(row.get("title", "(no title)"), 100),
            row.get("url", ""),
            author=None,
            published=None,
            summary=excerpt(row.get("content", ""), 300),
            score=row.get("score"),
            comments=None,
            extra={"engines": engines, "searxng_score": row.get("score")},
        ))
    return items


def _bucket_for_item(item: Dict[str, Any]) -> str:
    source = item.get("source")
    kind = item.get("kind")
    if source in {"reddit", "dc"}:
        return "community"
    if source == "searxng":
        kind = item.get("kind", "")
        if kind in ("it", "science", "tech"):
            return "dev"
        if kind == "news":
            return "news"
        return "other"
    if source == "youtube":
        return "news"
    if source == "hn":
        return "dev"
    if source == "github":
        return "dev" if kind in {"issue", "pr", "repo"} else "news"
    return "other"


def _bucket_summary(bucket: str, items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""
    top = items[:2]
    titles = [item.get("title") or "(no title)" for item in top]
    joined = " / ".join(titles)
    labels = {
        "news": "영상/외부 뉴스 흐름",
        "community": "커뮤니티 반응",
        "dev": "개발자/도구 신호",
        "other": "기타 신호",
    }
    prefix = labels.get(bucket, bucket)
    return f"{prefix}: {joined}"


def aggregate(args: argparse.Namespace) -> Dict[str, Any]:
    sources = args.sources or DEFAULT_SOURCES
    out: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    incremental = getattr(args, "incremental", False)
    watch = getattr(args, "watch", False)
    watch_alert = getattr(args, "watch_alert", False)
    state = load_state() if incremental else {}

    for source in sources:
        try:
            source_items: List[Dict[str, Any]] = []
            if source == "reddit":
                source_items = collect_reddit(args.query, args.limit, args.reddit_subreddit)
            elif source == "dc":
                source_items = collect_dc(args.query, args.limit, args.dc_gallery, args.dc_mode)
            elif source == "github":
                source_items = collect_github(args.query, args.limit, args.github_repo)
            elif source == "hn":
                source_items = collect_hn(args.query, args.limit)
            elif source == "youtube":
                source_items = collect_youtube(args.query, args.limit)
            elif source == "searxng":
                source_items = collect_searxng(args.query, args.limit, args.searxng_categories)
            elif source == "discord":
                if not args.discord_channel:
                    raise AggregateError("discord source requires --discord-channel")
                discord_items, discord_warnings = collect_discord(args.limit, args.discord_channel)
                source_items = discord_items
                for warning in discord_warnings:
                    errors.append({"source": source, "error": warning})
            if incremental:
                source_items = filter_incremental(source_items, source, state)
                update_state_for_source(state, source, source_items)
            out.extend(source_items)
        except Exception as exc:
            errors.append({"source": source, "error": str(exc)})

    if incremental:
        save_state(state)

    for item in out:
        item.setdefault("extra", {})["bucket"] = _bucket_for_item(item)

    watch_matches: List[Dict[str, Any]] = []
    if watch or watch_alert:
        watchlist = load_watchlist()
        for item in out:
            matched = matches_watchlist(item, watchlist)
            if matched:
                item.setdefault("extra", {})["watch_matches"] = matched
                watch_matches.append(item)
        if watch:
            out = watch_matches

    if args.bucket:
        out = [item for item in out if (item.get("extra") or {}).get("bucket") == args.bucket]

    out.sort(key=lambda item: _sort_rank(item, query=args.query), reverse=True)
    if args.max_items:
        out = out[: args.max_items]

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for item in out:
        bucket = (item.get("extra") or {}).get("bucket") or "other"
        buckets.setdefault(bucket, []).append(item)

    bucket_summaries = {
        bucket: {
            "count": len(items),
            "summary": _bucket_summary(bucket, items),
            "top_items": items[:DEFAULT_BUCKET_PREVIEW],
        }
        for bucket, items in buckets.items()
    }

    result = {
        "query": args.query,
        "sources": sources,
        "count": len(out),
        "items": out,
        "buckets": buckets,
        "bucket_summaries": bucket_summaries,
        "errors": errors,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if watch_alert and watch_matches:
        result["watch_alerts"] = watch_matches
    return result


def _append_item_text(lines: List[str], item: Dict[str, Any], *, include_summary: bool = True) -> None:
    meta = []
    if item.get("author"):
        meta.append(f"author: {item['author']}")
    if item.get("published"):
        meta.append(f"published: {item['published']}")
    if item.get("score") is not None:
        meta.append(f"score: {item['score']}")
    if item.get("comments") is not None:
        meta.append(f"comments: {item['comments']}")
    if item.get('source') == 'dc':
        dc_gallery = (item.get('extra') or {}).get('source_gallery_name') or (item.get('extra') or {}).get('gallery_name') or (item.get('extra') or {}).get('gallery')
        if dc_gallery:
            meta.append(f"gallery: {dc_gallery}")
    elif item.get('source') == 'reddit':
        subreddit = (item.get('extra') or {}).get('subreddit')
        if subreddit:
            meta.append(f"subreddit: {subreddit}")
    lines.append(f"- [{item.get('source')}/{item.get('kind')}] {item.get('title')}")
    if meta:
        lines.append(f"  {' | '.join(meta)}")
    lines.append(f"  url: {item.get('url')}")
    if include_summary and item.get("summary"):
        lines.append(f"  text: {item.get('summary')}")
    lines.append("")


def emit_text(payload: Dict[str, Any], output_mode: str = "default") -> str:
    lines = [f"community aggregate query={payload.get('query')!r} count={payload.get('count')} sources={','.join(payload.get('sources') or [])}"]
    if payload.get("errors"):
        lines.append("")
        lines.append("[warnings]")
        for err in payload["errors"]:
            lines.append(f"- {err.get('source')}: {err.get('error')}")
    buckets = payload.get("buckets") or {}
    bucket_summaries = payload.get("bucket_summaries") or {}
    ordered_buckets = [bucket for bucket in ["news", "community", "dev", "other"] if buckets.get(bucket)]
    if ordered_buckets:
        lines.append("")
        for bucket in ordered_buckets:
            info = bucket_summaries.get(bucket) or {}
            bucket_items = buckets.get(bucket, [])
            preview_count = 1 if output_mode == "brief" else (len(bucket_items) if output_mode == "full" else DEFAULT_BUCKET_PREVIEW)
            lines.append(f"[{bucket}] {info.get('count', len(bucket_items))}개")
            if info.get("summary"):
                lines.append(f"요약: {info['summary']}")
            lines.append("")
            for item in bucket_items[:preview_count]:
                _append_item_text(lines, item, include_summary=(output_mode != "brief"))
    else:
        if payload.get("items"):
            lines.append("")
        items = payload.get("items", [])
        preview_count = min(len(items), 5 if output_mode == "brief" else len(items) if output_mode == "full" else DEFAULT_BUCKET_PREVIEW)
        for item in items[:preview_count]:
            _append_item_text(lines, item, include_summary=(output_mode != "brief"))
    if not payload.get("items"):
        lines.append("(no items)")
    return "\n".join(lines).rstrip()


SOURCE_EMOJI = {
    "reddit": "\U0001f4e2",
    "dc": "\U0001f1f0\U0001f1f7",
    "github": "\U0001f4bb",
    "hn": "\U0001f4d0",
    "youtube": "\U0001f3ac",
    "searxng": "\U0001f50d",
    "discord": "\U0001f4ac",
}


def emit_watch_alert(payload: Dict[str, Any]) -> str:
    alerts = payload.get("watch_alerts") or []
    if not alerts:
        return ""
    lines = ["\u26a0\ufe0f Watchlist Alert"]
    for item in alerts:
        source = item.get("source") or ""
        emoji = SOURCE_EMOJI.get(source, "\U0001f4cc")
        title = item.get("title") or "(no title)"
        url = item.get("url") or ""
        wm = (item.get("extra") or {}).get("watch_matches") or []
        lines.append(f"{emoji} [{', '.join(wm)}] {title}")
        if url:
            lines.append(f"   {url}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("query", nargs="?", default="hot", help="Query to search across collectors (defaults to hot/general news)")
    parser.add_argument("--preset", choices=tuple(PRESET_CONFIGS.keys()), help="Apply a preset query/source profile")
    parser.add_argument("--sources", nargs="+", choices=ALL_SOURCES, default=None)
    parser.add_argument("--limit", type=int, default=5, help="Per-source fetch limit")
    parser.add_argument("--max-items", type=int, default=20, help="Final merged item cap")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--output-mode", choices=("brief", "default", "full"), default="default", help="Text output verbosity")
    parser.add_argument("--brief", action="store_true", help="Alias for --output-mode brief")
    parser.add_argument("--full", action="store_true", help="Alias for --output-mode full")
    parser.add_argument("--reddit-subreddit", help="Optional subreddit filter for Reddit search")
    parser.add_argument("--dc-gallery", help="Optional DC gallery filter")
    parser.add_argument("--dc-mode", choices=("auto", "search", "best", "popular"), default="auto", help="DC collection mode")
    parser.add_argument("--github-repo", help="Optional owner/repo to search repo issues/PRs instead of global GitHub search")
    parser.add_argument("--bucket", choices=("news", "community", "dev", "other"), help="Filter final output to one bucket")
    parser.add_argument("--searxng-categories", default=None, help="SearXNG categories (comma-separated, e.g. general,news,it)")
    parser.add_argument("--discord-channel", action="append", help="Discord channel id to fetch. Repeatable.")
    parser.add_argument("--incremental", action="store_true", help="Only collect items newer than last run (uses state.json)")
    parser.add_argument("--watch", action="store_true", help="Filter results to watchlist matches only (uses watchlist.json)")
    parser.add_argument("--watch-alert", action="store_true", help="Output watchlist matches in alert format for Telegram delivery")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.brief and args.full:
        print("ERROR: --brief and --full cannot be used together", file=sys.stderr)
        return 2
    if args.brief:
        args.output_mode = "brief"
    elif args.full:
        args.output_mode = "full"

    if args.preset:
        preset = PRESET_CONFIGS[args.preset]
        if not args.query or args.query == parser.get_default("query"):
            args.query = preset["query"]
        if args.sources is None:
            args.sources = list(preset["sources"])
        if args.bucket is None and preset.get("bucket") is not None:
            args.bucket = preset["bucket"]

    if args.sources is None:
        args.sources = list(DEFAULT_SOURCES)

    try:
        payload = aggregate(args)
    except AggregateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.watch_alert and payload.get("watch_alerts"):
        alert_text = emit_watch_alert(payload)
        if alert_text:
            print(alert_text)
            print()

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(emit_text(payload, output_mode=args.output_mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
