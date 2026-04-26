#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

USER_AGENT = "gongyung-github-cli/0.1"
BASE = "https://api.github.com"
DEFAULT_TIMEOUT = 20


class GitHubFetchError(RuntimeError):
    pass


def utc_iso(ts: Optional[str]) -> Optional[str]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc).isoformat()
    except Exception:
        return ts


def excerpt(text: Optional[str], limit: int = 220) -> str:
    if not text:
        return ""
    clean = " ".join(str(text).split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3].rstrip() + "..."


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_api(path: str, params: Optional[Dict[str, Any]] = None, *, jq: Optional[str] = None) -> Any:
    if not gh_available():
        raise GitHubFetchError("gh CLI is not installed")
    cmd = ["gh", "api", path]
    for key, value in (params or {}).items():
        if value is None:
            continue
        cmd.extend(["-f", f"{key}={value}"])
    if jq:
        cmd.extend(["--jq", jq])
    proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=DEFAULT_TIMEOUT)
    if proc.returncode != 0:
        raise GitHubFetchError(proc.stderr.strip() or proc.stdout.strip() or f"gh api failed for {path}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout


def http_api(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
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
        raise GitHubFetchError(f"HTTP {exc.code} for {url} ({detail})") from exc
    except urllib.error.URLError as exc:
        raise GitHubFetchError(f"Network error for {url}: {exc.reason}") from exc


def api(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    try:
        return gh_api(path, params)
    except GitHubFetchError:
        return http_api(path, params)


def normalize_repo(item: Dict[str, Any]) -> Dict[str, Any]:
    owner = (item.get("owner") or {}).get("login")
    return {
        "full_name": item.get("full_name"),
        "owner": owner,
        "name": item.get("name"),
        "description": item.get("description") or "",
        "private": item.get("private"),
        "fork": item.get("fork"),
        "language": item.get("language"),
        "topics": item.get("topics") or [],
        "stars": item.get("stargazers_count"),
        "watchers": item.get("watchers_count"),
        "forks": item.get("forks_count"),
        "open_issues": item.get("open_issues_count"),
        "default_branch": item.get("default_branch"),
        "pushed_at": utc_iso(item.get("pushed_at")),
        "updated_at": utc_iso(item.get("updated_at")),
        "created_at": utc_iso(item.get("created_at")),
        "url": item.get("html_url"),
    }


def normalize_issue(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "number": item.get("number"),
        "state": item.get("state"),
        "title": item.get("title"),
        "author": (item.get("user") or {}).get("login"),
        "comments": item.get("comments"),
        "labels": [label.get("name") for label in item.get("labels", []) if isinstance(label, dict)],
        "created_at": utc_iso(item.get("created_at")),
        "updated_at": utc_iso(item.get("updated_at")),
        "closed_at": utc_iso(item.get("closed_at")),
        "draft": item.get("draft"),
        "pull_request": bool(item.get("pull_request")),
        "url": item.get("html_url"),
        "body_excerpt": excerpt(item.get("body")),
    }


def normalize_release(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item.get("id"),
        "tag_name": item.get("tag_name"),
        "name": item.get("name"),
        "draft": item.get("draft"),
        "prerelease": item.get("prerelease"),
        "author": (item.get("author") or {}).get("login"),
        "published_at": utc_iso(item.get("published_at")),
        "created_at": utc_iso(item.get("created_at")),
        "url": item.get("html_url"),
        "body_excerpt": excerpt(item.get("body")),
    }


def fetch_repo(full_name: str) -> Dict[str, Any]:
    payload = api(f"/repos/{full_name}")
    return {
        "mode": "repo",
        "repo": normalize_repo(payload),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_repo_issues(full_name: str, state: str, limit: int) -> Dict[str, Any]:
    payload = api(f"/repos/{full_name}/issues", {"state": state, "per_page": limit})
    items = [normalize_issue(item) for item in payload]
    return {
        "mode": "issues",
        "repo": full_name,
        "state": state,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_repo_prs(full_name: str, state: str, limit: int) -> Dict[str, Any]:
    payload = api(f"/repos/{full_name}/pulls", {"state": state, "per_page": limit})
    items = []
    for item in payload:
        items.append({
            "number": item.get("number"),
            "state": item.get("state"),
            "title": item.get("title"),
            "author": (item.get("user") or {}).get("login"),
            "draft": item.get("draft"),
            "created_at": utc_iso(item.get("created_at")),
            "updated_at": utc_iso(item.get("updated_at")),
            "merged_at": utc_iso(item.get("merged_at")),
            "comments": item.get("comments"),
            "commits": item.get("commits"),
            "url": item.get("html_url"),
            "body_excerpt": excerpt(item.get("body")),
        })
    return {
        "mode": "prs",
        "repo": full_name,
        "state": state,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_releases(full_name: str, limit: int) -> Dict[str, Any]:
    payload = api(f"/repos/{full_name}/releases", {"per_page": limit})
    items = [normalize_release(item) for item in payload]
    return {
        "mode": "releases",
        "repo": full_name,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def search_repos(query: str, sort: str, limit: int) -> Dict[str, Any]:
    payload = api("/search/repositories", {"q": query, "sort": sort, "order": "desc", "per_page": limit})
    items = [normalize_repo(item) for item in payload.get("items", [])]
    return {
        "mode": "search-repos",
        "query": query,
        "sort": sort,
        "count": len(items),
        "items": items,
        "total_count": payload.get("total_count"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def search_issues(query: str, sort: str, limit: int) -> Dict[str, Any]:
    payload = api("/search/issues", {"q": query, "sort": sort, "order": "desc", "per_page": limit})
    items = [normalize_issue(item) for item in payload.get("items", [])]
    return {
        "mode": "search-issues",
        "query": query,
        "sort": sort,
        "count": len(items),
        "items": items,
        "total_count": payload.get("total_count"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def render_repo(repo: Dict[str, Any]) -> str:
    lines = [
        f"- {repo.get('full_name')} | ★ {repo.get('stars')} | forks {repo.get('forks')} | issues {repo.get('open_issues')}",
        f"  language: {repo.get('language')} | pushed: {repo.get('pushed_at')}",
        f"  url: {repo.get('url')}",
    ]
    if repo.get("description"):
        lines.append(f"  desc: {repo.get('description')}")
    if repo.get("topics"):
        lines.append(f"  topics: {', '.join(repo.get('topics')[:8])}")
    return "\n".join(lines)


def render_issue(item: Dict[str, Any]) -> str:
    kind = "PR" if item.get("pull_request") else "Issue"
    lines = [
        f"- {kind} #{item.get('number')} [{item.get('state')}] {item.get('title')}",
        f"  author: {item.get('author')} | comments: {item.get('comments')} | updated: {item.get('updated_at')}",
        f"  url: {item.get('url')}",
    ]
    if item.get("labels"):
        lines.append(f"  labels: {', '.join(item.get('labels'))}")
    if item.get("body_excerpt"):
        lines.append(f"  text: {item.get('body_excerpt')}")
    return "\n".join(lines)


def render_release(item: Dict[str, Any]) -> str:
    lines = [
        f"- {item.get('tag_name')} | {item.get('name') or '(no title)'}",
        f"  published: {item.get('published_at')} | prerelease: {item.get('prerelease')} | draft: {item.get('draft')}",
        f"  url: {item.get('url')}",
    ]
    if item.get("body_excerpt"):
        lines.append(f"  notes: {item.get('body_excerpt')}")
    return "\n".join(lines)


def emit_text(payload: Dict[str, Any]) -> str:
    mode = payload.get("mode")
    if mode == "repo":
        return render_repo(payload.get("repo", {}))
    if mode in {"issues", "search-issues"}:
        header = f"{mode} repo={payload.get('repo') or '-'} query={payload.get('query') or '-'} count={payload.get('count')}"
        body = "\n\n".join(render_issue(item) for item in payload.get("items", [])) or "(no items)"
        return f"{header}\n\n{body}"
    if mode == "prs":
        header = f"prs repo={payload.get('repo')} count={payload.get('count')}"
        body = "\n\n".join(render_issue({**item, 'pull_request': True}) for item in payload.get("items", [])) or "(no prs)"
        return f"{header}\n\n{body}"
    if mode in {"releases"}:
        header = f"releases repo={payload.get('repo')} count={payload.get('count')}"
        body = "\n\n".join(render_release(item) for item in payload.get("items", [])) or "(no releases)"
        return f"{header}\n\n{body}"
    if mode == "search-repos":
        header = f"search-repos query={payload.get('query')!r} count={payload.get('count')} total={payload.get('total_count')}"
        body = "\n\n".join(render_repo(item) for item in payload.get("items", [])) or "(no repos)"
        return f"{header}\n\n{body}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "text"), default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch GitHub repo, issue, PR, and release data for agent use.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    sub = parser.add_subparsers(dest="command", required=True)

    repo = sub.add_parser("repo")
    add_format_arg(repo)
    repo.add_argument("full_name", help="owner/repo")

    issues = sub.add_parser("issues")
    add_format_arg(issues)
    issues.add_argument("full_name", help="owner/repo")
    issues.add_argument("--state", default="open", choices=("open", "closed", "all"))
    issues.add_argument("--limit", type=int, default=10)

    prs = sub.add_parser("prs")
    add_format_arg(prs)
    prs.add_argument("full_name", help="owner/repo")
    prs.add_argument("--state", default="open", choices=("open", "closed", "all"))
    prs.add_argument("--limit", type=int, default=10)

    releases = sub.add_parser("releases")
    add_format_arg(releases)
    releases.add_argument("full_name", help="owner/repo")
    releases.add_argument("--limit", type=int, default=10)

    search_repos_parser = sub.add_parser("search-repos")
    add_format_arg(search_repos_parser)
    search_repos_parser.add_argument("query")
    search_repos_parser.add_argument("--sort", default="stars", choices=("stars", "forks", "updated"))
    search_repos_parser.add_argument("--limit", type=int, default=10)

    search_issues_parser = sub.add_parser("search-issues")
    add_format_arg(search_issues_parser)
    search_issues_parser.add_argument("query")
    search_issues_parser.add_argument("--sort", default="updated", choices=("comments", "created", "updated"))
    search_issues_parser.add_argument("--limit", type=int, default=10)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "repo":
            payload = fetch_repo(args.full_name)
        elif args.command == "issues":
            payload = fetch_repo_issues(args.full_name, args.state, args.limit)
        elif args.command == "prs":
            payload = fetch_repo_prs(args.full_name, args.state, args.limit)
        elif args.command == "releases":
            payload = fetch_releases(args.full_name, args.limit)
        elif args.command == "search-repos":
            payload = search_repos(args.query, args.sort, args.limit)
        elif args.command == "search-issues":
            payload = search_issues(args.query, args.sort, args.limit)
        else:
            raise GitHubFetchError(f"Unknown command: {args.command}")
    except (GitHubFetchError, subprocess.TimeoutExpired) as exc:
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
