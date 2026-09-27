from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .source import WeChatSource


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Query a user-supplied, decrypted Mac WeChat vault.")
    p.add_argument("--snapshot", type=Path, required=True, help="已解密微信快照目录")
    p.add_argument("--source-id", required=True, help="稳定的数据源标识")
    p.add_argument("--account-username", help="当前微信账号，用于判断消息收发方向")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看连接器状态")
    conversations = sub.add_parser("conversations", help="列出会话")
    conversations.add_argument("--query")
    conversations.add_argument("--kind", action="append", choices=["person", "group"])
    conversations.add_argument("--has-messages", action=argparse.BooleanOptionalAction, default=None)
    contacts = sub.add_parser("contacts", help="列出联系人和发言人")
    contacts.add_argument("--query")
    contacts.add_argument("--kind", action="append", choices=["person", "group"])
    contacts.add_argument("--subscription", action=argparse.BooleanOptionalAction, default=None)
    contacts.add_argument("--limit", type=int, default=1000)
    official = sub.add_parser("official", help="列出公众号联系人和对应会话")
    official.add_argument("--query")
    official.add_argument("--limit", type=int, default=1000)

    search = sub.add_parser("search", help="搜索消息、收藏夹或朋友圈")
    search.add_argument("query")
    search.add_argument("conversation_id", nargs="*")
    search.add_argument("--scope", choices=["messages", "favorites", "moments", "all"], default="messages")
    search.add_argument("--start")
    search.add_argument("--end")
    search.add_argument("--author", action="append")
    search.add_argument("--kind", action="append", choices=["text", "image", "voice", "video", "sticker", "contact_card", "location", "link", "file", "quote", "call", "system", "unsupported"])
    search.add_argument("--direction", action="append", choices=["incoming", "outgoing", "unknown"])
    search.add_argument("--quality", action="append", choices=["complete", "metadata_only", "unsupported", "decode_error"])
    search.add_argument("--has-link", action=argparse.BooleanOptionalAction)
    search.add_argument("--has-attachment", action=argparse.BooleanOptionalAction)
    search.add_argument("--official-only", action=argparse.BooleanOptionalAction, default=None)
    search.add_argument("--limit", type=int, default=100)
    search.add_argument("--cursor")

    items = sub.add_parser("items", help="读取标准化消息")
    items.add_argument("conversation_id", nargs="+")
    _add_message_filters(items, include_query=True)

    export = sub.add_parser("export", help="导出消息 JSONL 数据包")
    export.add_argument("conversation_id", nargs="+")
    _add_message_filters(export, include_query=True)
    export.add_argument("--previous", type=Path)
    export.add_argument("--output", type=Path, required=True)

    for resource, kinds in (("favorites", ["text", "image", "article", "contact_card", "video", "unknown"]), ("moments", ["text", "image", "video", "link", "mixed", "unknown"])):
        command = sub.add_parser(resource, help=f"查询{resource}")
        command.add_argument("--query")
        command.add_argument("--start")
        command.add_argument("--end")
        command.add_argument("--author", action="append")
        command.add_argument("--kind", action="append", choices=kinds)
        command.add_argument("--has-link", action=argparse.BooleanOptionalAction)
        command.add_argument("--has-attachment", action=argparse.BooleanOptionalAction)
        command.add_argument("--limit", type=int, default=100)
        command.add_argument("--cursor")
        command.add_argument("--output", type=Path)
        command.add_argument("--previous", type=Path)
    return p


def _add_message_filters(command: argparse.ArgumentParser, *, include_query: bool) -> None:
    command.add_argument("--start")
    command.add_argument("--end")
    command.add_argument("--author", action="append")
    command.add_argument("--kind", action="append", choices=["text", "image", "voice", "video", "sticker", "contact_card", "location", "link", "file", "quote", "call", "system", "unsupported"])
    command.add_argument("--direction", action="append", choices=["incoming", "outgoing", "unknown"])
    command.add_argument("--quality", action="append", choices=["complete", "metadata_only", "unsupported", "decode_error"])
    if include_query:
        command.add_argument("--query", help="匹配正文、标题、链接或附件名")
    command.add_argument("--has-link", action=argparse.BooleanOptionalAction)
    command.add_argument("--has-attachment", action=argparse.BooleanOptionalAction)
    command.add_argument("--official-only", action=argparse.BooleanOptionalAction, default=None)
    command.add_argument("--limit", type=int, default=100)
    command.add_argument("--cursor")


def _filters(args: argparse.Namespace, *, include_query: bool = True) -> dict:
    authors = getattr(args, "author", None) or []
    result = {
        "author_usernames": [value for value in authors if not value.startswith("actor_")],
        "author_ids": [value for value in authors if value.startswith("actor_")],
        "kinds": getattr(args, "kind", None),
        "directions": getattr(args, "direction", None),
        "qualities": getattr(args, "quality", None),
        "query": getattr(args, "query", None) if include_query else None,
        "has_links": getattr(args, "has_link", None),
        "has_attachments": getattr(args, "has_attachment", None),
        "official_only": getattr(args, "official_only", None),
    }
    if not include_query:
        result.pop("query")
    return result


def output(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        source = WeChatSource(args.snapshot, args.source_id, args.account_username)
        if args.command == "status":
            output(source.status())
        elif args.command == "conversations":
            output(source.list_conversations(args.query, kinds=args.kind, has_messages=args.has_messages))
        elif args.command == "contacts":
            output(source.list_contacts(args.query, kinds=args.kind, is_subscription=args.subscription, limit=args.limit))
        elif args.command == "official":
            output(source.list_official_accounts(args.query, limit=args.limit))
        elif args.command == "search":
            if args.scope == "messages":
                output(source.search_items(args.query, args.conversation_id or None, start=args.start, end=args.end, limit=args.limit, cursor=args.cursor, **_filters(args, include_query=False)))
            elif args.scope in {"favorites", "moments"}:
                method = source.search_favorites if args.scope == "favorites" else source.search_moments
                filters = _filters(args, include_query=False)
                filters.pop("official_only", None)
                filters.pop("directions", None)
                filters.pop("qualities", None)
                output(method(args.query, start=args.start, end=args.end, limit=args.limit, **filters))
            else:
                output(source.search_all(args.query, start=args.start, end=args.end, limit=args.limit))
        elif args.command == "items":
            output(source.read_items(args.conversation_id, **_filters(args)))
        elif args.command == "export":
            output(source.export_bundle(args.output, args.conversation_id, previous=args.previous, **_filters(args)))
        elif args.command in {"favorites", "moments"}:
            method = source.list_favorites if args.command == "favorites" else source.list_moments
            filters = _filters(args)
            filters.pop("official_only", None)
            filters.pop("directions", None)
            filters.pop("qualities", None)
            result = method(start=args.start, end=args.end, limit=args.limit, cursor=args.cursor, **filters)
            if args.output:
                export_method = source.export_favorites if args.command == "favorites" else source.export_moments
                output(export_method(args.output, previous=args.previous, **filters, start=args.start, end=args.end))
            else:
                output(result)
        return 0
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
