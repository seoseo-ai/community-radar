#!/usr/bin/env python3
"""DC Inside gallery fetcher — read-only CLI for gallery lists, post views, and search.

Uses m.dcinside.com (mobile endpoint) which is accessible from Termux/Android.

Usage:
  python3 dc_fetch.py gallery openclaw --limit 10
  python3 dc_fetch.py gallery thesingularity --sort recommend --limit 5
  python3 dc_fetch.py post thesingularity 986090
  python3 dc_fetch.py post thesingularity 986090 --comments
  python3 dc_fetch.py search "오픈클로" --limit 10
  python3 dc_fetch.py best --limit 10

Output: JSON to stdout (for agent consumption). Use --format text for readable output.
"""
from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; SM-G781N) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36"
)
BASE_M = "https://m.dcinside.com"
BASE_GALL = "https://gall.dcinside.com"
DEFAULT_TIMEOUT = 20
DEFAULT_RETRIES = 3
RETRYABLE_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


class DCFetchError(RuntimeError):
    pass


def request_text(url: str, timeout: int = DEFAULT_TIMEOUT, retries: int = DEFAULT_RETRIES) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.reason
            try:
                body = exc.read().decode("utf-8", "replace")
                if body:
                    detail = f"{detail}: {body[:220]}"
            except Exception:
                pass
            last_error = DCFetchError(f"HTTP {exc.code} for {url} ({detail})")
            if exc.code not in RETRYABLE_HTTP_CODES or attempt >= retries:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            last_error = DCFetchError(f"Network error for {url}: {reason}")
            if attempt >= retries:
                raise last_error from exc
        time.sleep(min(0.8 * (2 ** (attempt - 1)), 3.0))
    if last_error:
        raise last_error
    raise DCFetchError(f"Failed to fetch {url}")


# ---------------------------------------------------------------------------
# Regex-based extractors (more reliable than HTMLParser for messy DC HTML)
# ---------------------------------------------------------------------------

def _extract_posts_mobile(html: str, gallery_id: str) -> List[Dict[str, Any]]:
    """Extract post list from mobile DC HTML using regex.

    Supports both common gallery URLs like `/board/{slug}/{no}?id={gallery_id}`
    and special pages like dcbest that use `/board/{slug}/{no}` without query params.
    """
    posts = []
    escaped_gid = re.escape(gallery_id)
    anchor_pattern = re.compile(
        r'<a\s+href="(?P<href>[^"]*/board/(?P<slug>\w+)/(?P<post_no>\d+)(?:\?[^"]*)?)"[^>]*class="lt"[^>]*>'
        r'(?P<block>.*?)</a>',
        re.DOTALL,
    )

    matches = []
    for m in anchor_pattern.finditer(html):
        href = m.group('href')
        slug = m.group('slug')
        if gallery_id == 'dcbest':
            if slug != 'dcbest':
                continue
        else:
            if f'id={gallery_id}' not in href:
                continue
        matches.append(m)

    url_slug = gallery_id
    if matches:
        url_slug = matches[0].group('slug')

    for m in matches:
        post_no = m.group('post_no')
        block = m.group('block')

        # Title: inside <span class="subjectin">
        title_m = re.search(r'<span\s+class="subjectin"[^>]*>(.*?)</span>', block, re.DOTALL)
        title = title_m.group(1).strip() if title_m else "(untitled)"
        title = re.sub(r'<[^>]+>', '', title).strip()

        # Writer: <li class="list-nick">...</li>
        writer_m = re.search(r'class="list-nick"[^>]*>(.*?)</li>', block, re.DOTALL)
        writer = writer_m.group(1).strip() if writer_m else "?"
        writer = re.sub(r'<[^>]+>', '', writer).strip()

        # Date, hits, recommend from ginfo <li> tags
        info_items = re.findall(r'<li[^>]*>(.*?)</li>', block, re.DOTALL)
        date_str = ""
        hits = ""
        recommend = ""
        for item in info_items:
            item_clean = re.sub(r'<[^>]+>', '', item).strip()
            if re.match(r'\d{2}\.\d{2}', item_clean) or re.match(r'\d{2}:\d{2}', item_clean):
                date_str = item_clean
            elif item_clean.startswith("조회"):
                hits = item_clean.replace("조회", "").strip()
            elif "추천" in item_clean:
                # 추천 count may be in a child <span>: "추천 <span>25</span>"
                rec_m = re.search(r'(\d+)', item)
                recommend = rec_m.group(1) if rec_m else "0"

        # Comment count: <span class="ct ...">N</span> is a sibling AFTER the lt anchor,
        # typically inside a <div> that follows </a>. Extract from the tail
        # of html starting after the current match end.
        comment_m = re.search(r'<span\s+class="ct[^"]*">\s*(\d+)\s*</span>', block)
        if not comment_m:
            # Look in the ~300 chars after this anchor closes
            tail_start = m.end()
            tail = html[tail_start:tail_start + 300]
            comment_m = re.search(r'<span\s+class="ct[^"]*">\s*(\d+)\s*</span>', tail)
        comments_count = comment_m.group(1) if comment_m else "0"

        # Has image marker varies by page type
        has_img = "sp-lst-img" in block or "sp-lst-best" in block

        post_url = f"{BASE_M}/board/{url_slug}/{post_no}"
        if gallery_id != 'dcbest':
            post_url += f"?id={gallery_id}"
        # Also build desktop URL for reference
        desktop_url = f"{BASE_GALL}/mgallery/board/view/?id={gallery_id}&no={post_no}"

        posts.append({
            "num": post_no,
            "title": title,
            "writer": writer,
            "date": date_str,
            "hits": hits,
            "recommend": recommend,
            "comments": comments_count,
            "has_image": has_img,
            "url": post_url,
            "desktop_url": desktop_url,
        })

    return posts


def _extract_post_content(html: str) -> Dict[str, Any]:
    """Extract post content from mobile view page using regex."""
    result = {
        "title": "",
        "writer": "",
        "date": "",
        "hits": "",
        "recommend": "",
        "content": "",
        "comments": [],
    }

    # ── Title ──
    og_title = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    if og_title:
        title_raw = og_title.group(1)
        # DC og:title is "POST_TITLE - GALLERY_NAME", extract just the title
        parts = title_raw.rsplit(" - ", 1)
        result["title"] = parts[0].strip() if len(parts) > 1 else title_raw.strip()
    else:
        title_m = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
        if title_m:
            raw = title_m.group(1).strip()
            parts = raw.rsplit(" - ", 1)
            result["title"] = parts[0].strip() if len(parts) > 1 else raw

    # ── Content: try LD+JSON articleBody first (most reliable) ──
    ld_body = re.search(r'"articleBody":"((?:[^"\\]|\\.)*)"', html)
    if ld_body:
        raw = ld_body.group(1)
        raw = raw.replace('\\n', '\n').replace('\\t', '\t').replace('\"', '"')
        result["content"] = raw.strip()
    else:
        # Fallback: <div class="thum-txtin">
        content_m = re.search(r'<div\s+class="thum-txtin">(.*?)</div>\s*</div>', html, re.DOTALL)
        if not content_m:
            content_m = re.search(r'<div\s+class="thum-txtin">(.*?)</div>', html, re.DOTALL)
        if content_m:
            raw = content_m.group(1)
            raw = re.sub(r'<script[^>]*>.*?</script>', '', raw, flags=re.DOTALL)
            raw = re.sub(r'<br\s*/?\s*>', '\n', raw)
            raw = re.sub(r'</p>', '\n', raw)
            text = re.sub(r'<[^>]+>', '', raw)
            text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
            text = text.replace("&amp;", "&").replace("&quot;", '"')
            text = re.sub(r'\n{3,}', '\n\n', text).strip()
            result["content"] = text

    # ── Writer: <button type="button" class="nick">NAME ──
    writer_m = re.search(r'<button[^>]*class="nick">(.*?)(?:<span|</button>)', html, re.DOTALL)
    if writer_m:
        result["writer"] = re.sub(r'<[^>]+>', '', writer_m.group(1)).strip()

    # ── Hits & Recommend ──
    hits_m = re.search(r'조회수\s*(\d+)', html)
    if hits_m:
        result["hits"] = hits_m.group(1)
    rec_m = re.search(r'추천\s*<span>\s*(\d+)\s*</span>', html)
    if rec_m:
        result["recommend"] = rec_m.group(1)

    # ── Date ──
    date_m = re.search(r'<span\s+class="date">(.*?)</span>', html)
    if date_m:
        result["date"] = date_m.group(1).strip()
    else:
        date_m2 = re.search(r'"datePublished":"([^"]+)"', html)
        if date_m2:
            result["date"] = date_m2.group(1)

    # ── Comments: <li class="comment"> ──
    comment_blocks = re.findall(
        r'<li[^>]*class="comment[^"]*"[^>]*>(.*?)</li>',
        html, re.DOTALL,
    )
    for block in comment_blocks:
        nick_m = re.search(r'class="nick">(.*?)(?:<span|</button>)', block, re.DOTALL)
        text_m = re.search(r'<p\s+class="txt">(.*?)</p>', block, re.DOTALL)
        nick = re.sub(r'<[^>]+>', '', nick_m.group(1)).strip() if nick_m else "?"
        text = ""
        if text_m:
            raw = text_m.group(1)
            raw = re.sub(r'<br\s*/?\s*>', '\n', raw)
            text = re.sub(r'<[^>]+>', '', raw).strip()
            text = text.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
            text = text.replace("&amp;", "&")
        if text:
            result["comments"].append({"writer": nick, "text": text})

    return result


# ---------------------------------------------------------------------------
# API functions
# ---------------------------------------------------------------------------

def _detect_mobile_slug(html: str, gallery_id: str) -> str:
    """Detect the actual mobile URL slug for a gallery."""
    escaped = re.escape(gallery_id)
    m = re.search(r'/board/(\w+)/\d+\?id=' + escaped, html)
    if m:
        return m.group(1)
    m = re.search(r'/board/(\w+)\?id=' + escaped, html)
    if m:
        return m.group(1)
    return gallery_id


def fetch_gallery(
    gallery_id: str,
    sort: str = "new",
    page: int = 1,
    limit: int = 20,
) -> Dict[str, Any]:
    """Fetch gallery post list via mobile endpoint."""
    sort = {"popular": "recommend", "hot": "recommend", "concept": "recommend"}.get(sort, sort)
    # First try to detect the slug by fetching the gallery page
    # URL format: /board/{slug}?id={gallery_id} — slug may differ from gallery_id
    url = f"{BASE_M}/board/{gallery_id}?id={gallery_id}&page={page}"
    if sort == "recommend":
        url += "&recommend=1"

    html = request_text(url)
    slug = _detect_mobile_slug(html, gallery_id)
    posts = _extract_posts_mobile(html, gallery_id)

    # Fix URLs with detected slug
    for p in posts:
        if slug != gallery_id and slug in p.get("url", ""):
            pass  # already correct from extractor
        elif slug != gallery_id:
            p["url"] = f"{BASE_M}/board/{slug}/{p['num']}?id={gallery_id}"

    # Extract gallery name from og:title
    og_m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
    gallery_name = og_m.group(1) if og_m else gallery_id

    return {
        "gallery_id": gallery_id,
        "gallery_name": gallery_name,
        "page": page,
        "sort": sort,
        "count": len(posts[:limit]),
        "posts": posts[:limit],
    }


def fetch_post(
    gallery_id: str,
    post_no: int,
    with_comments: bool = False,
) -> Dict[str, Any]:
    """Fetch a single post via mobile endpoint."""
    # First fetch gallery list to detect the correct slug
    list_html = request_text(f"{BASE_M}/board/{gallery_id}?id={gallery_id}")
    slug = _detect_mobile_slug(list_html, gallery_id)
    url = f"{BASE_M}/board/{slug}/{post_no}"
    if gallery_id != 'dcbest':
        url += f"?id={gallery_id}"
    html = request_text(url)

    data = _extract_post_content(html)
    desktop_url = f"{BASE_GALL}/mgallery/board/view/?id={gallery_id}&no={post_no}"

    result = {
        "gallery_id": gallery_id,
        "post_no": post_no,
        "url": url,
        "desktop_url": desktop_url,
        "title": data["title"],
        "writer": data["writer"],
        "date": data["date"],
        "hits": data["hits"],
        "recommend": data["recommend"],
        "content": data["content"],
        "content_length": len(data["content"]),
    }
    if gallery_id == "dcbest":
        source = _extract_original_gallery_ref(html)
        if source:
            result.update(source)
        title_hint = _extract_title_gallery_hint(data["title"])
        if title_hint:
            result.setdefault("source_gallery_hint", title_hint)
    if data["comments"]:
        result["comments"] = data["comments"]
        result["comment_count"] = len(data["comments"])

    return result


def fetch_comments(
    gallery_id: str,
    post_no: int,
    limit: int = 50,
) -> Dict[str, Any]:
    """Fetch comments for a post."""
    data = fetch_post(gallery_id, post_no, with_comments=True)
    comments = data.get("comments", [])[:limit]
    return {
        "gallery_id": gallery_id,
        "post_no": post_no,
        "title": data.get("title", ""),
        "comment_count": len(comments),
        "comments": comments,
    }


def fetch_best(limit: int = 20) -> Dict[str, Any]:
    """Fetch DC Inside main best posts."""
    url = f"{BASE_M}/board/dcbest"
    html = request_text(url)
    posts = _extract_posts_mobile(html, "dcbest")
    posts = posts[:limit]
    _enrich_dcbest_posts(posts)

    return {
        "gallery_id": "dcbest",
        "gallery_name": "디시베스트",
        "count": len(posts),
        "posts": posts,
    }


def fetch_search(
    query: str,
    gallery_id: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    """Search DC Inside posts."""
    if gallery_id:
        url = f"{BASE_M}/board/{gallery_id}?id={gallery_id}&s_type=search_subject_memo&s_keyword={urllib.parse.quote(query)}"
        html = request_text(url)
        posts = _extract_posts_mobile(html, gallery_id)
    else:
        # Global search via DC search page
        url = f"{BASE_M}/search/gall_content?keyword={urllib.parse.quote(query)}"
        html = request_text(url)
        posts = _extract_search_results(html)
        _enrich_search_posts(posts, fetch_details_limit=min(limit, 5))

    return {
        "query": query,
        "gallery_id": gallery_id,
        "count": len(posts[:limit]),
        "posts": posts[:limit],
    }


def _extract_original_gallery_ref(html: str) -> Dict[str, str]:
    """Extract original gallery/post reference from dcbest post pages."""
    m = re.search(
        r'<div\s+class="original-gall-go">\s*<a\s+href="(?P<url>[^"]+)"[^>]*>출처:\s*(?P<name>.*?)\s*\[원본 보기\]</a>',
        html,
        re.DOTALL,
    )
    if not m:
        return {}
    name = re.sub(r'<[^>]+>', '', m.group('name')).strip()
    url = m.group('url').strip()
    info: Dict[str, str] = {
        "source_gallery_name": name,
        "source_post_url": url,
    }
    url_m = re.search(r'/board/(?P<gallery_id>\w+)/(?:[^/?#]+/)?(?P<post_no>\d+)', url)
    if url_m:
        info["source_gallery_id"] = url_m.group('gallery_id')
        info["source_post_no"] = url_m.group('post_no')
    return info


def _extract_title_gallery_hint(title: str) -> Optional[str]:
    m = re.match(r'\[(.*?)\]', title or '')
    return m.group(1).strip() if m else None


def _enrich_dcbest_posts(posts: List[Dict[str, Any]]) -> None:
    for post in posts:
        title_hint = _extract_title_gallery_hint(post.get("title", ""))
        if title_hint:
            post["source_gallery_hint"] = title_hint
        url = post.get("url")
        if not url:
            continue
        try:
            html = request_text(url)
            source = _extract_original_gallery_ref(html)
            if source:
                post.update(source)
        except DCFetchError:
            continue


def _enrich_search_posts(posts: List[Dict[str, Any]], *, fetch_details_limit: int = 5) -> None:
    for post in posts[:fetch_details_limit]:
        gallery_id = post.get("gallery_id")
        post_no = post.get("num")
        if not gallery_id or not post_no or post_no == "?":
            continue
        try:
            detail = fetch_post(gallery_id, int(post_no), with_comments=True)
        except (ValueError, DCFetchError):
            continue
        if detail.get("writer"):
            post["writer"] = detail["writer"]
        if detail.get("hits"):
            post["hits"] = detail["hits"]
        if detail.get("recommend"):
            post["recommend"] = detail["recommend"]
        if detail.get("comment_count") is not None:
            post["comments"] = str(detail.get("comment_count"))
        elif detail.get("comments"):
            post["comments"] = str(len(detail.get("comments", [])))
        if detail.get("date"):
            post["date"] = detail["date"]


def _extract_search_results(html: str) -> List[Dict[str, Any]]:
    """Extract results from mobile global search page."""
    posts = []
    list_match = re.search(r'<ul\s+class="sch-lst">(.*?)</ul>', html, re.DOTALL)
    if not list_match:
        return posts
    block = list_match.group(1)
    pattern = re.compile(
        r'<li>\s*<a\s+href="(?P<url>[^"]+)">\s*'
        r'<div\s+class="sch-lnk">\s*<span\s+class="tit">(?P<title>.*?)</span>'
        r'(?:\s*<span\s+class="txt">(?P<summary>.*?)</span>)?\s*</div>\s*'
        r'<div\s+class="sch-lnk-sub">\s*<span\s+class="gallname-lnk">(?P<gallery>.*?)</span>'
        r'\s*<span\s+class="date">(?P<date>.*?)</span>\s*</div>\s*</a>\s*</li>',
        re.DOTALL,
    )
    for m in pattern.finditer(block):
        url = m.group('url').strip()
        title = re.sub(r'<[^>]+>', '', m.group('title')).strip()
        summary = re.sub(r'<[^>]+>', '', (m.group('summary') or '')).strip()
        gallery_name = re.sub(r'<[^>]+>', '', m.group('gallery')).strip()
        date = re.sub(r'<[^>]+>', '', m.group('date')).strip()
        url_full = url if url.startswith('http') else BASE_M + url
        post_no = '?'
        gallery_id = None
        url_m = re.search(r'/board/(?P<gallery_id>\w+)/(?P<post_no>\d+)', url_full)
        if url_m:
            gallery_id = url_m.group('gallery_id')
            post_no = url_m.group('post_no')
        posts.append({
            "num": post_no,
            "title": title,
            "subject": summary,
            "writer": "?",
            "date": date,
            "hits": "",
            "recommend": "",
            "comments": "0",
            "url": url_full,
            "desktop_url": "",
            "gallery_name": gallery_name,
            "gallery_id": gallery_id,
        })
    return posts


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------

def format_posts_text(data: Dict[str, Any]) -> str:
    lines = []
    gallery = data.get("gallery_name") or data.get("gallery_id") or data.get("query") or "?"
    lines.append(f"=== DC Inside: {gallery} ===")
    lines.append(f"page {data.get('page', '?')} | sort={data.get('sort', '?')} | {data.get('count', 0)}개")
    lines.append("")
    for i, post in enumerate(data.get("posts", []), 1):
        num = post.get("num", "?")
        title = post.get("title", "(untitled)")
        writer = post.get("writer", "?")
        date = post.get("date", "")
        hits = post.get("hits", "")
        rec = post.get("recommend", "")
        cmt = post.get("comments", "0")
        url = post.get("url", "")
        source_gallery = post.get("source_gallery_name") or post.get("source_gallery_hint") or post.get("gallery_name")
        lines.append(f"[{i}] #{num} {title}")
        lines.append(f"    {writer} | {date} | 조회{hits} 추천{rec} 댓글{cmt}")
        if source_gallery:
            label = "원본갤" if post.get("source_gallery_name") or post.get("source_gallery_hint") else "갤러리"
            lines.append(f"    {label}: {source_gallery}")
        if url:
            lines.append(f"    {url}")
        lines.append("")
    return "\n".join(lines)


def format_post_text(data: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"=== {data.get('title', '(untitled)')} ===")
    lines.append(f"갤러리: {data.get('gallery_id', '?')}")
    lines.append(f"작성자: {data.get('writer', '?')}")
    lines.append(f"날짜: {data.get('date', '?')}")
    lines.append(f"조회: {data.get('hits', '?')} | 추천: {data.get('recommend', '?')}")
    lines.append(f"URL: {data.get('url', '?')}")
    if data.get("source_gallery_name") or data.get("source_gallery_hint"):
        src_name = data.get("source_gallery_name") or data.get("source_gallery_hint")
        lines.append(f"원본갤: {src_name}")
    if data.get("source_post_url"):
        lines.append(f"원본URL: {data.get('source_post_url')}")
    lines.append("")
    lines.append(data.get("content", "(empty)"))
    if data.get("comments"):
        lines.append("")
        lines.append(f"--- 댓글 ({data.get('comment_count', len(data['comments']))}) ---")
        for c in data["comments"]:
            nick = c.get("writer", "?")
            text = c.get("text", "")
            lines.append(f"  {nick}: {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="DC Inside gallery fetcher (read-only, via mobile endpoint)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s gallery openclaw --limit 10
  %(prog)s gallery thesingularity --sort recommend --limit 5
  %(prog)s post thesingularity 986090
  %(prog)s post thesingularity 986090 --comments
  %(prog)s comments thesingularity 986090 --limit 20
  %(prog)s search "오픈클로" --limit 10
  %(prog)s search "AI" --gallery thesingularity --limit 5
  %(prog)s best --limit 10

Note: gall.dcinside.com is blocked from some networks; this uses m.dcinside.com.
        """,
    )
    sub = p.add_subparsers(dest="command")

    # gallery
    g = sub.add_parser("gallery", help="List gallery posts")
    g.add_argument("gallery_id", help="Gallery ID (e.g. openclaw, thesingularity)")
    g.add_argument("--sort", default="new", choices=["new", "recommend", "popular", "hot", "concept"], help="Sort order")
    g.add_argument("--page", type=int, default=1, help="Page number")
    g.add_argument("--limit", type=int, default=20, help="Max posts to return")

    # post
    pv = sub.add_parser("post", help="View a single post")
    pv.add_argument("gallery_id", help="Gallery ID")
    pv.add_argument("post_no", type=int, help="Post number")
    pv.add_argument("--comments", action="store_true", help="Include comments")

    # comments
    cm = sub.add_parser("comments", help="Fetch post comments")
    cm.add_argument("gallery_id", help="Gallery ID")
    cm.add_argument("post_no", type=int, help="Post number")
    cm.add_argument("--limit", type=int, default=50, help="Max comments")

    # search
    s = sub.add_parser("search", help="Search posts")
    s.add_argument("query", help="Search query")
    s.add_argument("--gallery", dest="gallery_id", help="Limit to gallery")
    s.add_argument("--limit", type=int, default=10, help="Max results")

    # best
    b = sub.add_parser("best", help="DC main best posts")
    b.add_argument("--limit", type=int, default=20, help="Max posts")

    # global
    p.add_argument("--format", choices=["json", "text"], default="json", help="Output format")
    p.add_argument("--compact", action="store_true", help="Compact JSON output")

    return p


def _normalize_global_args(argv: List[str]) -> List[str]:
    """Allow global args like --format after the subcommand for convenience."""
    if not argv:
        return argv
    known_global_with_value = {"--format"}
    known_global_flags = {"--compact"}
    globals_found: List[str] = []
    rest: List[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in known_global_with_value and i + 1 < len(argv):
            globals_found.extend([token, argv[i + 1]])
            i += 2
            continue
        if token in known_global_flags:
            globals_found.append(token)
            i += 1
            continue
        rest.append(token)
        i += 1
    return globals_found + rest


def main():
    parser = build_parser()
    args = parser.parse_args(_normalize_global_args(sys.argv[1:]))

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "gallery":
            data = fetch_gallery(
                args.gallery_id,
                sort=args.sort,
                page=args.page,
                limit=args.limit,
            )
        elif args.command == "post":
            data = fetch_post(
                args.gallery_id,
                args.post_no,
                with_comments=args.comments,
            )
        elif args.command == "comments":
            data = fetch_comments(
                args.gallery_id,
                args.post_no,
                limit=args.limit,
            )
        elif args.command == "search":
            data = fetch_search(
                args.query,
                gallery_id=getattr(args, "gallery_id", None),
                limit=args.limit,
            )
        elif args.command == "best":
            data = fetch_best(limit=args.limit)
        else:
            parser.print_help()
            sys.exit(1)

        if args.format == "text":
            if args.command == "post":
                print(format_post_text(data))
            else:
                print(format_posts_text(data))
        else:
            indent = None if args.compact else 2
            print(json.dumps(data, indent=indent, ensure_ascii=False))

    except DCFetchError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
