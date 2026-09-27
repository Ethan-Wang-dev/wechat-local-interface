from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .protocol import WeChatProtocol
from .source import WeChatSource


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Query a user-supplied, decrypted Mac WeChat vault.")
    p.add_argument("--snapshot", type=Path, required=True, help="已解密微信快照目录")
    p.add_argument("--source-id", required=True, help="稳定的数据源标识")
    p.add_argument("--account-username", help="当前微信账号，用于判断消息收发方向")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看连接器状态")
    rpc = sub.add_parser("rpc", help="通过 stdin/stdout 运行语言无关 JSON 协议")
    rpc.add_argument("--file", type=Path, help="逐行读取 JSON 请求；省略时读取 stdin")
    conversations = sub.add_parser("conversations", help="列出会话")
    conversations.add_argument("--query")
    conversations.add_argument("--kind", action="append", choices=["person", "group"])
    conversations.add_argument("--has-messages", action=argparse.BooleanOptionalAction, default=None)
    members = sub.add_parser("members", help="列出群成员及其联系人关系")
    members.add_argument("conversation_id")
    members.add_argument("--query")
    members.add_argument("--friend", action=argparse.BooleanOptionalAction, default=None, help="只看本地标记为好友/非好友的成员；未知状态不匹配")
    members.add_argument("--owner", action=argparse.BooleanOptionalAction, default=None, help="只看/排除群主")
    members.add_argument("--limit", type=int, default=5000)
    contact_groups = sub.add_parser("contact-groups", help="列出一个联系人所在的群聊")
    contact_groups.add_argument("actor_id", help="actor_id、username 或唯一显示名")
    contact_groups.add_argument("--query")
    contact_groups.add_argument("--limit", type=int, default=1000)
    common_groups = sub.add_parser("common-groups", help="列出多个联系人共同所在的群聊")
    common_groups.add_argument("actor_id", nargs="+", help="至少两个 actor_id、username 或唯一显示名")
    common_groups.add_argument("--query")
    common_groups.add_argument("--limit", type=int, default=1000)
    relations = sub.add_parser("relations", help="列出联系人与群聊之间的关系边")
    relations.add_argument("--subject")
    relations.add_argument("--object")
    relations.add_argument("--type", action="append", choices=["member_of", "owns"])
    relations.add_argument("--limit", type=int, default=5000)
    contacts = sub.add_parser("contacts", help="列出联系人和发言人")
    contacts.add_argument("--query")
    contacts.add_argument("--kind", action="append", choices=["person", "group"])
    contacts.add_argument("--subscription", action=argparse.BooleanOptionalAction, default=None)
    contacts.add_argument("--friend", action=argparse.BooleanOptionalAction, default=None)
    contacts.add_argument("--limit", type=int, default=1000)
    official = sub.add_parser("official", help="列出公众号联系人和对应会话")
    official.add_argument("--query")
    official.add_argument("--limit", type=int, default=1000)
    labels = sub.add_parser("labels", help="列出联系人标签")
    labels.add_argument("--query")
    labels.add_argument("--limit", type=int, default=1000)
    sessions = sub.add_parser("sessions", help="列出会话状态")
    sessions.add_argument("--query")
    sessions.add_argument("--unread-only", action=argparse.BooleanOptionalAction)
    sessions.add_argument("--limit", type=int, default=1000)
    tags = sub.add_parser("favorite-tags", help="列出收藏标签")
    tags.add_argument("--query")
    tags.add_argument("--limit", type=int, default=1000)
    interactions = sub.add_parser("moment-interactions", help="列出朋友圈互动")
    interactions.add_argument("--feed-id")
    interactions.add_argument("--author", action="append")
    interactions.add_argument("--unread-only", action=argparse.BooleanOptionalAction)
    interactions.add_argument("--limit", type=int, default=1000)
    events = sub.add_parser("events", help="列出红包、转账等特殊事件")
    events.add_argument("--kind", choices=["red_envelope", "transfer", "friend_request", "revoked_message"])
    events.add_argument("--limit", type=int, default=1000)
    assets = sub.add_parser("assets", help="列出本地媒体索引")
    assets.add_argument("--kind", choices=["file", "image", "video", "avatar"])
    assets.add_argument("--limit", type=int, default=1000)
    emoticons = sub.add_parser("emoticons", help="列出表情包索引")
    emoticons.add_argument("--limit", type=int, default=1000)

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
        if resource == "moments":
            command.add_argument("--is-pinned", action=argparse.BooleanOptionalAction)
            command.add_argument("--is-private", action=argparse.BooleanOptionalAction)
            command.add_argument("--has-location", action=argparse.BooleanOptionalAction)
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
        "is_pinned": getattr(args, "is_pinned", None),
        "is_private": getattr(args, "is_private", None),
        "has_location": getattr(args, "has_location", None),
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
        if args.command == "rpc":
            protocol = WeChatProtocol(source)
            if args.file:
                with args.file.open("r", encoding="utf-8") as stream:
                    for line in protocol.handle_lines(stream):
                        print(line)
            else:
                for line in protocol.handle_lines(sys.stdin):
                    print(line)
        elif args.command == "status":
            output(source.status())
        elif args.command == "conversations":
            output(source.list_conversations(args.query, kinds=args.kind, has_messages=args.has_messages))
        elif args.command == "members":
            output(source.list_group_members(args.conversation_id, query=args.query, is_friend=args.friend, is_owner=args.owner, limit=args.limit))
        elif args.command == "contact-groups":
            output(source.list_contact_groups(args.actor_id, query=args.query, limit=args.limit))
        elif args.command == "common-groups":
            output(source.list_common_groups(args.actor_id, query=args.query, limit=args.limit))
        elif args.command == "relations":
            output(source.list_relationships(subject_id=args.subject, object_id=args.object, relationship_types=args.type, limit=args.limit))
        elif args.command == "contacts":
            output(source.list_contacts(args.query, kinds=args.kind, is_subscription=args.subscription, is_friend=args.friend, limit=args.limit))
        elif args.command == "official":
            output(source.list_official_accounts(args.query, limit=args.limit))
        elif args.command == "labels":
            output(source.list_contact_labels(args.query, limit=args.limit))
        elif args.command == "sessions":
            output(source.list_sessions(args.query, unread_only=args.unread_only, limit=args.limit))
        elif args.command == "favorite-tags":
            output(source.list_favorite_tags(args.query, limit=args.limit))
        elif args.command == "moment-interactions":
            output(source.list_moment_interactions(feed_id=args.feed_id, author_usernames=args.author, unread_only=args.unread_only, limit=args.limit))
        elif args.command == "events":
            output(source.list_special_events(args.kind, limit=args.limit))
        elif args.command == "assets":
            output(source.list_media_assets(args.kind, limit=args.limit))
        elif args.command == "emoticons":
            output(source.list_emoticons(limit=args.limit))
        elif args.command == "search":
            if args.scope == "messages":
                filters = _filters(args, include_query=False)
                for key in ("is_pinned", "is_private", "has_location"):
                    filters.pop(key, None)
                output(source.search_items(args.query, args.conversation_id or None, start=args.start, end=args.end, limit=args.limit, cursor=args.cursor, **filters))
            elif args.scope in {"favorites", "moments"}:
                method = source.search_favorites if args.scope == "favorites" else source.search_moments
                filters = _filters(args, include_query=False)
                filters.pop("official_only", None)
                filters.pop("directions", None)
                filters.pop("qualities", None)
                if args.scope == "favorites":
                    for key in ("is_pinned", "is_private", "has_location"):
                        filters.pop(key, None)
                output(method(args.query, start=args.start, end=args.end, limit=args.limit, **filters))
            else:
                output(source.search_all(args.query, start=args.start, end=args.end, limit=args.limit))
        elif args.command == "items":
            filters = _filters(args)
            for key in ("is_pinned", "is_private", "has_location"):
                filters.pop(key, None)
            output(source.read_items(args.conversation_id, **filters))
        elif args.command == "export":
            filters = _filters(args)
            for key in ("is_pinned", "is_private", "has_location"):
                filters.pop(key, None)
            output(source.export_bundle(args.output, args.conversation_id, previous=args.previous, **filters))
        elif args.command in {"favorites", "moments"}:
            method = source.list_favorites if args.command == "favorites" else source.list_moments
            filters = _filters(args)
            filters.pop("official_only", None)
            filters.pop("directions", None)
            filters.pop("qualities", None)
            if args.command == "favorites":
                for key in ("is_pinned", "is_private", "has_location"):
                    filters.pop(key, None)
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
