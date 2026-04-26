#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

BASE = "https://discord.com/api/v10"
DEFAULT_TIMEOUT = 20
TOKEN_ENV = "DISCORD_BOT_TOKEN"
USER_AGENT = "gongyung-discord-cli/0.1"


class DiscordFetchError(RuntimeError):
    pass


def get_token(explicit: Optional[str]) -> str:
    token = explicit or os.environ.get(TOKEN_ENV) or ""
    token = token.strip()
    if not token:
        raise DiscordFetchError(f"Missing Discord bot token. Pass --token or set {TOKEN_ENV}.")
    return token


def request_json(path: str, token: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE}{path}"
    if query:
        url = f"{url}?{query}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Authorization": f"Bot {token}",
            "Accept": "application/json",
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
        raise DiscordFetchError(f"HTTP {exc.code} for {url} ({detail})") from exc
    except urllib.error.URLError as exc:
        raise DiscordFetchError(f"Network error for {url}: {exc.reason}") from exc


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


def make_empty_payload(mode: str, target_key: str, target_value: str, warning: str) -> Dict[str, Any]:
    return {
        "mode": mode,
        target_key: target_value,
        "count": 0,
        "items": [],
        "warning": warning,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_guild_channels(guild_id: str, token: str) -> Dict[str, Any]:
    payload = request_json(f"/guilds/{guild_id}/channels", token)
    items = []
    for item in payload:
        items.append({
            "id": item.get("id"),
            "name": item.get("name"),
            "type": item.get("type"),
            "topic": item.get("topic") or "",
            "parent_id": item.get("parent_id"),
            "position": item.get("position"),
            "nsfw": item.get("nsfw"),
        })
    return {
        "mode": "channels",
        "guild_id": guild_id,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_messages(channel_id: str, token: str, limit: int, before: Optional[str]) -> Dict[str, Any]:
    payload = request_json(f"/channels/{channel_id}/messages", token, {"limit": limit, "before": before})
    items = []
    for item in payload:
        author = item.get("author") or {}
        items.append({
            "id": item.get("id"),
            "author": author.get("username"),
            "author_id": author.get("id"),
            "timestamp": utc_iso(item.get("timestamp")),
            "edited_timestamp": utc_iso(item.get("edited_timestamp")),
            "content": item.get("content") or "",
            "content_excerpt": excerpt(item.get("content")),
            "attachments": len(item.get("attachments") or []),
            "embeds": len(item.get("embeds") or []),
            "reply_to": (item.get("referenced_message") or {}).get("id") if item.get("referenced_message") else None,
        })
    return {
        "mode": "messages",
        "channel_id": channel_id,
        "count": len(items),
        "items": items,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def render_channel(item: Dict[str, Any]) -> str:
    lines = [f"- #{item.get('name')} | id {item.get('id')} | type {item.get('type')} | pos {item.get('position')}"]
    if item.get("topic"):
        lines.append(f"  topic: {item.get('topic')}")
    return "\n".join(lines)


def render_message(item: Dict[str, Any]) -> str:
    lines = [
        f"- {item.get('timestamp')} | {item.get('author')} ({item.get('author_id')})",
        f"  id: {item.get('id')} | attachments: {item.get('attachments')} | embeds: {item.get('embeds')}",
    ]
    if item.get("content_excerpt"):
        lines.append(f"  text: {item.get('content_excerpt')}")
    return "\n".join(lines)


def emit_text(payload: Dict[str, Any]) -> str:
    warning = payload.get("warning")
    if payload.get("mode") == "channels":
        header = f"discord channels guild={payload.get('guild_id')} count={payload.get('count')}"
        body = "\n\n".join(render_channel(item) for item in payload.get("items", [])) or "(no channels)"
        return f"{header}" + (f"\nwarning: {warning}" if warning else "") + f"\n\n{body}"
    if payload.get("mode") == "messages":
        header = f"discord messages channel={payload.get('channel_id')} count={payload.get('count')}"
        body = "\n\n".join(render_message(item) for item in payload.get("items", [])) or "(no messages)"
        return f"{header}" + (f"\nwarning: {warning}" if warning else "") + f"\n\n{body}"
    return json.dumps(payload, ensure_ascii=False, indent=2)


def add_format_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("json", "text"), default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch Discord channels or recent messages with a bot token.")
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--token", help=f"Discord bot token. Defaults to ${TOKEN_ENV}.")
    parser.add_argument("--allow-missing-token", action="store_true", help="Return an empty payload with a warning instead of failing when the bot token is missing.")
    sub = parser.add_subparsers(dest="command", required=True)

    channels = sub.add_parser("channels")
    add_format_arg(channels)
    channels.add_argument("guild_id")

    messages = sub.add_parser("messages")
    add_format_arg(messages)
    messages.add_argument("channel_id")
    messages.add_argument("--limit", type=int, default=20)
    messages.add_argument("--before")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        token = get_token(args.token)
        if args.command == "channels":
            payload = fetch_guild_channels(args.guild_id, token)
        elif args.command == "messages":
            payload = fetch_messages(args.channel_id, token, args.limit, args.before)
        else:
            raise DiscordFetchError(f"Unknown command: {args.command}")
    except DiscordFetchError as exc:
        if args.allow_missing_token and "Missing Discord bot token" in str(exc):
            if args.command == "channels":
                payload = make_empty_payload("channels", "guild_id", args.guild_id, str(exc))
            else:
                payload = make_empty_payload("messages", "channel_id", args.channel_id, str(exc))
        else:
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
