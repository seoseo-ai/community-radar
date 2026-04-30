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

REPO_DIR = Path(__file__).resolve().parent
# Standalone repo: scripts are in same dir. OpenClaw workspace: scripts/ sibling
_SCRIPTS_CANDIDATES = [REPO_DIR, REPO_DIR.parent / "scripts"]
SCRIPTS = next((p for p in _SCRIPTS_CANDIDATES if (p / "dc_fetch.py").exists()), REPO_DIR)
STATE_FILE = Path(__file__).resolve().parent / "state.json"
WATCHLIST_FILE = Path(__file__).resolve().parent / "watchlist.json"
SNAPSHOTS_DIR = Path(__file__).resolve().parent / "snapshots"
DEFAULT_SOURCES = ["reddit", "dc", "github", "hn", "youtube", "searxng"]
ALL_SOURCES = DEFAULT_SOURCES + ["searxng", "discord"]
DESCRIPTION = "Aggregate community sentiment/signals across non-Discord sources by default, with optional Discord support."
DEFAULT_TIMEOUT = 45
TIME_DECAY_HALF_LIFE_HOURS = 6
TIME_DECAY_FLOOR = 0.1
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

# Signal-quality heuristics. These are intentionally simple and transparent:
# the radar should favor items that look actionable/structural, and demote
# pure meme/gossip traffic that can dominate raw community hot lists.
HIGH_SIGNAL_TERMS = {
    "ai", "agent", "llm", "model", "openai", "anthropic", "google", "mistral", "claude",
    "gemini", "gpt", "gpu", "nvidia", "amd", "semiconductor", "chip", "benchmark",
    "release", "launched", "launch", "open source", "opensource", "api", "sdk", "pricing",
    "outage", "incident", "downtime", "breach", "leak", "vulnerability", "cve", "exploit",
    "security", "supply chain", "lawsuit", "regulation", "policy", "ban", "tariff", "oil",
    "iran", "ukraine", "russia", "china", "taiwan", "korea", "rate", "inflation",
    "earnings", "acquisition", "investment", "funding", "ipo", "merger",
    "인공지능", "에이아이", "모델", "오픈AI", "앤트로픽", "구글", "반도체", "엔비디아", "AMD",
    "보안", "취약점", "해킹", "유출", "장애", "사고", "출시", "공개", "규제", "정책",
    "금리", "물가", "환율", "유가", "투자", "인수", "합병", "소송", "중국", "대만", "이란", "우크라이나",
}

LOW_SIGNAL_TERMS = {
    "싱글벙글", "와들와들", "ㅋㅋ", "ㅎㅎ", "개웃", "웃긴", "짤", "밈", "진상", "설거지론",
    "념글", "개념글", "후방", "인증", "떡밥", "어그로", "gossip", "meme", "funny",
}

HIGH_SIGNAL_SUBREDDITS = {"singularity", "technology", "programming", "worldnews", "machinelearning", "localllama"}
LOW_SIGNAL_DC_HINTS = {"싱갤", "주갤", "해갤"}
QUALITY_MODE_DEFAULT_MIN = {"raw": None, "balanced": None, "signal": 35}

SEARXNG_BASE_URL = "https://vps4.tail1546e7.ts.net:18443"
DEFAULT_HOT_SUBREDDITS = ["technology", "worldnews", "programming", "singularity"]
DEFAULT_BUCKET_PREVIEW = 3
PRESET_CONFIGS = {
    "general-news": {
        "query": "hot",
        "sources": ["reddit", "dc", "hn", "youtube"],
        "bucket": None,
    },
    "signal-news": {
        "query": "hot",
        "sources": ["reddit", "dc", "github", "hn", "youtube"],
        "bucket": None,
        "quality_mode": "signal",
        "max_items": 12,
    },
    "agent-news": {
        "query": "OpenClaw",
        "sources": ["reddit", "dc", "github", "hn", "youtube"],
        "bucket": None,
        "quality_mode": "signal",
    },
}


class AggregateError(RuntimeError):
    pass


def run_json(args: List[str], timeout: int = DEFAULT_TIMEOUT) -> Any:
    proc = subprocess.run(args, cwd=SCRIPTS, capture_output=True, text=True, timeout=timeout)
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


def _compute_percentiles(items: List[Dict[str, Any]]) -> None:
    by_source: Dict[str, List[Dict[str, Any]]] = {}
    for item in items:
        by_source.setdefault(item.get("source") or "unknown", []).append(item)
    for source, source_items in by_source.items():
        raw_scores = []
        for item in source_items:
            extra = item.get("extra") or {}
            sr = as_int(extra.get("source_rank")) or 0
            s = item.get("score") or 0
            c = item.get("comments") or 0
            raw_scores.append(sr + s * 10 + c * 5)
        if not raw_scores:
            continue
        sorted_scores = sorted(raw_scores)
        n = len(sorted_scores)
        for i, item in enumerate(source_items):
            extra = item.get("extra") or {}
            sr = as_int(extra.get("source_rank")) or 0
            s = item.get("score") or 0
            c = item.get("comments") or 0
            raw = sr + s * 10 + c * 5
            rank_pos = sorted_scores.index(raw)
            percentile = (rank_pos / n) * 100 if n > 1 else 50
            item.setdefault("extra", {})["percentile"] = round(percentile, 1)


def _contains_any(text: str, terms: set[str]) -> List[str]:
    lowered = text.lower()
    found = []
    for term in terms:
        needle = term.lower()
        # For Latin tokens, require token boundaries so "ai" does not match "aid".
        if re.fullmatch(r"[a-z0-9][a-z0-9 +._/-]*", needle):
            pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
            matched = re.search(pattern, lowered) is not None
        else:
            matched = needle in lowered
        if matched:
            found.append(term)
    return sorted(found, key=len, reverse=True)


def _signal_profile_for_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Return transparent signal-quality metadata for ranking and summaries."""
    extra = item.get("extra") or {}
    text = " ".join(filter(None, [
        item.get("title") or "",
        item.get("summary") or "",
        str(extra.get("source_gallery_name") or ""),
        str(extra.get("source_gallery_hint") or ""),
        str(extra.get("subreddit") or ""),
        " ".join(extra.get("topics") or []) if isinstance(extra.get("topics"), list) else "",
    ]))

    matched_high = _contains_any(text, HIGH_SIGNAL_TERMS)
    matched_low = _contains_any(text, LOW_SIGNAL_TERMS)
    score = 0
    reasons: List[str] = []
    penalties: List[str] = []

    if matched_high:
        score += min(45, 12 + len(matched_high[:4]) * 8)
        reasons.append("topic:" + ",".join(matched_high[:3]))

    source = item.get("source") or ""
    kind = item.get("kind") or ""
    if source in {"hn", "github", "searxng"}:
        score += 18
        reasons.append(f"source:{source}")
    elif source == "reddit":
        subreddit = (extra.get("subreddit") or "").lower()
        if subreddit in HIGH_SIGNAL_SUBREDDITS:
            score += 14
            reasons.append(f"subreddit:{subreddit}")
    elif source == "youtube":
        # YouTube search is useful, but can be clickbait-heavy; require topic or views to shine.
        score += 4
    elif source == "dc":
        gallery_hint = str(extra.get("source_gallery_hint") or extra.get("gallery") or "")
        if gallery_hint in LOW_SIGNAL_DC_HINTS:
            score -= 12
            penalties.append(f"dc_low_signal_gallery:{gallery_hint}")

    comments = item.get("comments") or 0
    popularity = (item.get("score") or 0) + comments * 2
    if popularity >= 500:
        score += 12
        reasons.append("strong_reaction")
    elif popularity >= 100:
        score += 7
        reasons.append("reaction")

    if kind in {"issue", "pr"}:
        score += 10
        reasons.append("dev_thread")
    elif kind == "repo":
        score += 6
        reasons.append("repo")

    if extra.get("watch_matches"):
        score += 30
        reasons.append("watchlist")

    if matched_low:
        penalty = min(35, 12 + len(matched_low[:3]) * 8)
        score -= penalty
        penalties.append("low_signal:" + ",".join(matched_low[:3]))

    # Recency matters for a radar, but avoid over-penalizing sources that only expose relative dates.
    decay = _time_decay_factor(item.get("published"))
    if decay < 0.5:
        score -= 8
        penalties.append("stale")

    score = max(0, min(100, score))
    label = "high" if score >= 60 else "medium" if score >= 35 else "low"
    return {
        "quality_score": score,
        "quality_label": label,
        "why": reasons[:4],
        "penalties": penalties[:3],
    }


def annotate_signal_quality(items: List[Dict[str, Any]]) -> None:
    for item in items:
        item.setdefault("extra", {}).update(_signal_profile_for_item(item))


def _quality_threshold(args: argparse.Namespace) -> Optional[int]:
    explicit = getattr(args, "min_quality", None)
    if explicit is not None:
        return explicit
    return QUALITY_MODE_DEFAULT_MIN.get(getattr(args, "quality_mode", "balanced"))


def _time_decay_factor(published: Optional[str]) -> float:
    if not published:
        return 1.0
    try:
        if published.endswith("Z"):
            published = published[:-1] + "+00:00"
        pub_dt = datetime.fromisoformat(published)
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_hours = (now - pub_dt).total_seconds() / 3600.0
        if age_hours <= TIME_DECAY_HALF_LIFE_HOURS:
            return 1.0
        decay = 0.5 ** (age_hours / TIME_DECAY_HALF_LIFE_HOURS)
        return max(decay, TIME_DECAY_FLOOR)
    except (ValueError, TypeError):
        return 1.0


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

    percentile = extra.get("percentile")
    if percentile is not None:
        rank = int(rank * 0.6 + percentile * 8)

    decay = _time_decay_factor(item.get("published"))
    rank = int(rank * decay)

    q = (query or "").strip().lower()
    if q in GENERIC_HOT_QUERIES:
        if source == "github" and kind == "repo":
            rank -= 180
        if source == "hn" and kind == "comment":
            rank -= 160
        if source == "youtube":
            rank -= 60

    quality_score = as_int(extra.get("quality_score"))
    if quality_score is not None:
        # Pull meaningful structural signals upward and push meme/gossip traffic down.
        rank += int((quality_score - 35) * 12)
        if quality_score < 20:
            rank -= 300
        if extra.get("penalties"):
            rank -= 120 * len(extra.get("penalties") or [])
        if extra.get("watch_matches"):
            rank += 500
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

    _compute_percentiles(out)

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

    annotate_signal_quality(out)

    min_quality = _quality_threshold(args)
    if min_quality is not None:
        out = [
            item for item in out
            if (as_int((item.get("extra") or {}).get("quality_score")) or 0) >= min_quality
        ]

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
        "quality_mode": getattr(args, "quality_mode", "balanced"),
        "min_quality": min_quality,
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
    extra = item.get("extra") or {}
    if extra.get("quality_score") is not None:
        why = ",".join(extra.get("why") or [])
        label = extra.get("quality_label") or "?"
        quality = f"signal: {extra['quality_score']}/{label}"
        if why:
            quality += f" ({why})"
        meta.append(quality)
    if item.get('source') == 'dc':
        dc_gallery = extra.get('source_gallery_name') or extra.get('gallery_name') or extra.get('gallery')
        if dc_gallery:
            meta.append(f"gallery: {dc_gallery}")
    elif item.get('source') == 'reddit':
        subreddit = extra.get('subreddit')
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
    quality = payload.get("quality_mode") or "balanced"
    min_quality = payload.get("min_quality")
    quality_suffix = f" quality={quality}" + (f" min={min_quality}" if min_quality is not None else "")
    lines = [f"community aggregate query={payload.get('query')!r} count={payload.get('count')} sources={','.join(payload.get('sources') or [])}{quality_suffix}"]
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


def emit_telegram(payload: Dict[str, Any]) -> str:
    lines = []
    items = payload.get("items") or []
    if not items:
        return "(no items)"
    for item in items:
        source = item.get("source") or ""
        emoji = SOURCE_EMOJI.get(source, "\U0001f4cc")
        title = item.get("title") or "(no title)"
        url = item.get("url") or ""
        score = item.get("score")
        comments = item.get("comments")
        meta_parts = []
        if score is not None:
            meta_parts.append(f"\u2b06{score}")
        if comments is not None:
            meta_parts.append(f"\U0001f4ac{comments}")
        q = (item.get("extra") or {}).get("quality_score")
        if q is not None:
            meta_parts.append(f"\U0001f4a1{q}")
        meta = " ".join(meta_parts)
        line = f"{emoji} {title}"
        if meta:
            line += f" [{meta}]"
        if url:
            line += f"\n   {url}"
        lines.append(line)
    watch_alerts = payload.get("watch_alerts")
    if watch_alerts:
        lines.append("")
        lines.append("\u26a0\ufe0f Watch Alerts:")
        for item in watch_alerts:
            wm = (item.get("extra") or {}).get("watch_matches") or []
            lines.append(f"  {', '.join(wm)}: {item.get('title')}")
    return "\n".join(lines)


def emit_markdown(payload: Dict[str, Any]) -> str:
    lines = []
    q = payload.get("query") or ""
    lines.append(f"# Community Radar: {q}")
    lines.append(f"*{payload.get('count', 0)} items from {', '.join(payload.get('sources') or [])}*")
    lines.append("")
    buckets = payload.get("buckets") or {}
    ordered_buckets = [b for b in ["news", "community", "dev", "other"] if buckets.get(b)]
    if ordered_buckets:
        for bucket in ordered_buckets:
            bucket_items = buckets[bucket]
            lines.append(f"## {bucket.title()} ({len(bucket_items)})")
            lines.append("")
            for item in bucket_items:
                source = item.get("source") or ""
                emoji = SOURCE_EMOJI.get(source, "\U0001f4cc")
                title = item.get("title") or "(no title)"
                url = item.get("url") or ""
                score = item.get("score")
                comments = item.get("comments")
                meta_parts = []
                if score is not None:
                    meta_parts.append(f"\u2b06{score}")
                if comments is not None:
                    meta_parts.append(f"\U0001f4ac{comments}")
                q = (item.get("extra") or {}).get("quality_score")
                if q is not None:
                    meta_parts.append(f"\U0001f4a1{q}")
                meta = " ".join(meta_parts)
                if url:
                    lines.append(f"- {emoji} [{title}]({url}) {meta}")
                else:
                    lines.append(f"- {emoji} {title} {meta}")
            lines.append("")
    else:
        for item in (payload.get("items") or []):
            source = item.get("source") or ""
            emoji = SOURCE_EMOJI.get(source, "\U0001f4cc")
            title = item.get("title") or "(no title)"
            url = item.get("url") or ""
            if url:
                lines.append(f"- {emoji} [{title}]({url})")
            else:
                lines.append(f"- {emoji} {title}")
        lines.append("")
    if payload.get("errors"):
        lines.append("---")
        lines.append("**Warnings:**")
        for err in payload["errors"]:
            lines.append(f"- {err.get('source')}: {err.get('error')}")
    return "\n".join(lines).rstrip()


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
    parser.add_argument("--format", choices=("json", "text", "markdown", "telegram"), default="text")
    parser.add_argument("--output-mode", choices=("brief", "default", "full"), default="default", help="Text output verbosity")
    parser.add_argument("--brief", action="store_true", help="Alias for --output-mode brief")
    parser.add_argument("--full", action="store_true", help="Alias for --output-mode full")
    parser.add_argument("--reddit-subreddit", help="Optional subreddit filter for Reddit search")
    parser.add_argument("--dc-gallery", help="Optional DC gallery filter")
    parser.add_argument("--dc-mode", choices=("auto", "search", "best", "popular"), default="auto", help="DC collection mode")
    parser.add_argument("--github-repo", help="Optional owner/repo to search repo issues/PRs instead of global GitHub search")
    parser.add_argument("--bucket", choices=("news", "community", "dev", "other"), help="Filter final output to one bucket")
    parser.add_argument("--quality-mode", choices=("raw", "balanced", "signal"), default="balanced", help="Signal-quality mode: raw disables filtering, signal filters low-value meme/gossip traffic")
    parser.add_argument("--min-quality", type=int, default=None, help="Minimum signal score 0-100; defaults to 35 in --quality-mode signal")
    parser.add_argument("--searxng-categories", default=None, help="SearXNG categories (comma-separated, e.g. general,news,it)")
    parser.add_argument("--discord-channel", action="append", help="Discord channel id to fetch. Repeatable.")
    parser.add_argument("--incremental", action="store_true", help="Only collect items newer than last run (uses state.json)")
    parser.add_argument("--watch", action="store_true", help="Filter results to watchlist matches only (uses watchlist.json)")
    parser.add_argument("--watch-alert", action="store_true", help="Output watchlist matches in alert format for Telegram delivery")
    parser.add_argument("--snapshot", action="store_true", help="Save results to snapshots/YYYY-MM-DD.json")
    parser.add_argument("--history", type=int, nargs="?", const=7, default=None, help="Show snapshot history (default 7 days)")
    parser.add_argument("--compare", type=int, default=None, metavar="N", help="Compare current results with N-days-ago snapshot")
    return parser


def save_snapshot(payload: Dict[str, Any], date_str: Optional[str] = None) -> Path:
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    path = SNAPSHOTS_DIR / f"{date_str}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_snapshot(date_str: str) -> Optional[Dict[str, Any]]:
    path = SNAPSHOTS_DIR / f"{date_str}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def list_snapshots() -> List[str]:
    if not SNAPSHOTS_DIR.exists():
        return []
    return sorted(f.stem for f in SNAPSHOTS_DIR.glob("*.json"))


def compare_snapshots(current: Dict[str, Any], past: Dict[str, Any]) -> Dict[str, Any]:
    current_titles = {item.get("title", "") for item in (current.get("items") or [])}
    past_titles = {item.get("title", "") for item in (past.get("items") or [])}
    new_items = [i for i in (current.get("items") or []) if i.get("title") not in past_titles]
    gone_items = [i for i in (past.get("items") or []) if i.get("title") not in current_titles]
    common = current_titles & past_titles
    return {
        "current_date": current.get("fetched_at", ""),
        "past_date": past.get("fetched_at", ""),
        "new_count": len(new_items),
        "gone_count": len(gone_items),
        "common_count": len(common),
        "new_items": new_items[:10],
        "gone_items": gone_items[:10],
    }


def emit_history(days: int) -> str:
    snaps = list_snapshots()
    if not snaps:
        return "No snapshots found. Run with --snapshot first."
    recent = snaps[-days:] if days else snaps
    lines = [f"📋 Snapshot History ({len(recent)} days)", ""]
    for snap_date in recent:
        data = load_snapshot(snap_date)
        count = (data or {}).get("count", "?")
        sources = ", ".join((data or {}).get("sources") or [])
        lines.append(f"  {snap_date}: {count} items [{sources}]")
    return "\n".join(lines)


def emit_compare(cmp: Dict[str, Any]) -> str:
    lines = ["📊 Snapshot Comparison"]
    lines.append(f"  Then: {cmp['past_date']}")
    lines.append(f"  Now:  {cmp['current_date']}")
    lines.append(f"")
    lines.append(f"  🆕 New: {cmp['new_count']} | ❌ Gone: {cmp['gone_count']} | 📌 Common: {cmp['common_count']}")
    if cmp.get("new_items"):
        lines.append(f"")
        lines.append("  New items:")
        for item in cmp["new_items"][:5]:
            source = item.get("source", "")
            emoji = SOURCE_EMOJI.get(source, "📌")
            lines.append(f"    {emoji} {item.get('title', '')}")
    if cmp.get("gone_items"):
        lines.append(f"")
        lines.append("  Gone items:")
        for item in cmp["gone_items"][:5]:
            source = item.get("source", "")
            emoji = SOURCE_EMOJI.get(source, "📌")
            lines.append(f"    {emoji} {item.get('title', '')}")
    return "\n".join(lines)


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

    # Handle --history subcommand (no collection needed)
    if args.history is not None:
        print(emit_history(args.history))
        return 0

    if args.preset:
        preset = PRESET_CONFIGS[args.preset]
        if not args.query or args.query == parser.get_default("query"):
            args.query = preset["query"]
        if args.sources is None:
            args.sources = list(preset["sources"])
        if args.bucket is None and preset.get("bucket") is not None:
            args.bucket = preset["bucket"]
        if preset.get("quality_mode") and args.quality_mode == parser.get_default("quality_mode"):
            args.quality_mode = preset["quality_mode"]
        if preset.get("max_items") and args.max_items == parser.get_default("max_items"):
            args.max_items = preset["max_items"]

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

    # Handle --snapshot
    if args.snapshot:
        snap_path = save_snapshot(payload)
        print(f"Snapshot saved: {snap_path}")

    # Handle --compare
    if args.compare is not None:
        compare_date = (datetime.now() - timedelta(days=args.compare)).strftime("%Y-%m-%d")
        past = load_snapshot(compare_date)
        if not past:
            print(f"No snapshot found for {compare_date}. Run with --snapshot first.", file=sys.stderr)
            return 1
        cmp = compare_snapshots(payload, past)
        print(emit_compare(cmp))
        return 0

    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif args.format == "markdown":
        print(emit_markdown(payload))
    elif args.format == "telegram":
        print(emit_telegram(payload))
    else:
        print(emit_text(payload, output_mode=args.output_mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
