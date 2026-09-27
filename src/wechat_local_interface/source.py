"""Read-only adapter from an already-decrypted Mac WeChat vault.

This module deliberately knows nothing about decryption, downstream knowledge
stores, model providers, or network services.  It turns a stable subset of the
local SQLite schema into a portable, source-scoped JSONL bundle.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from xml.etree import ElementTree as ET

try:  # Optional until a compressed message is encountered.
    import zstandard as zstd
except ImportError:
    zstd = None


SCHEMA_VERSION = "wechat.local.v0"
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TABLE_RE = re.compile(r"^(?:message|biz_message)_\d+\.db$")
_MSG_TABLE_RE = re.compile(r"^Msg_[0-9a-fA-F]{32}$")
_MEDIA_TYPES = {3: "image", 34: "voice", 43: "video", 47: "sticker"}
_FILTER_KINDS = {"text", "image", "voice", "video", "sticker", "contact_card", "location", "link", "file", "quote", "call", "system", "unsupported"}
_FILTER_DIRECTIONS = {"incoming", "outgoing", "unknown"}
_FILTER_QUALITY = {"complete", "metadata_only", "unsupported", "decode_error"}
_CONTACT_KINDS = {"person", "group"}
_FAVORITE_KINDS = {"text", "image", "article", "contact_card", "video", "unknown"}
_MOMENT_KINDS = {"text", "image", "video", "link", "mixed", "unknown"}
_FAVORITE_TYPE_MAP = {1: "text", 2: "image", 5: "article", 19: "contact_card", 20: "video"}
_REQUIRED_COLUMN_ALIASES = {
    "local_id": ("local_id", "id", "rowid"),
    "server_id": ("server_id",),
    "local_type": ("local_type", "type"),
    "create_time": ("create_time", "timestamp"),
}
_OPTIONAL_COLUMN_ALIASES = {
    "sender_id": ("real_sender_id", "sender_id"),
    "content": ("message_content", "content"),
    "compressed_content": ("compress_content",),
    "compression_flag": ("WCDB_CT_message_content",),
}
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_MAX_DECODED_BYTES = 4 * 1024 * 1024


def _sha(value: bytes | str) -> str:
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _opaque(source_id: str, kind: str, value: str) -> str:
    raw = source_id + "\0" + kind + "\0" + value
    return f"{kind}_{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


def _md5_username(username: str) -> str:
    return hashlib.md5(username.encode("utf-8")).hexdigest()


def _utc_timestamp(value: Any) -> str | None:
    try:
        timestamp = int(value or 0)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError, OSError):
        return None


def _type_parts(value: Any) -> tuple[int, int]:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return 0, 0
    if raw > 0xFFFFFFFF:
        return raw & 0xFFFFFFFF, raw >> 32
    return raw, 0


def _decode_bytes(value: Any, compression_flag: Any = None) -> tuple[str, str | None]:
    if value is None:
        return "", None
    if isinstance(value, str):
        return (value, None) if len(value.encode("utf-8")) <= _MAX_DECODED_BYTES else ("", "content exceeds safety limit")
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return "", "content has an unsupported storage type"
    data = bytes(value)
    compressed = data.startswith(_ZSTD_MAGIC) or compression_flag == 4
    if compressed:
        if zstd is None:
            return "", "zstandard dependency is required for compressed content"
        try:
            with zstd.ZstdDecompressor(max_window_size=8 * 1024 * 1024).stream_reader(data) as reader:
                data = reader.read(_MAX_DECODED_BYTES + 1)
        except Exception as exc:
            return "", f"compressed content decode failed: {type(exc).__name__}"
    if len(data) > _MAX_DECODED_BYTES:
        return "", "decoded content exceeds safety limit"
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return "", "content is not valid UTF-8"


def _parse_xml(content: str) -> ET.Element | None:
    if not content or not content.lstrip().startswith("<"):
        return None
    try:
        return ET.fromstring(content)
    except ET.ParseError:
        cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", content)
        if cleaned == content:
            return None
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError:
            return None


def _xml_text(root: ET.Element, *paths: str) -> str:
    for path in paths:
        value = root.findtext(path)
        if value:
            return value.strip()
    return ""


def _extract_urls(value: str) -> list[str]:
    # URL extraction is deliberately local; the connector never dereferences URLs.
    found = re.findall(r"https?://[^\s<>\"']+", value or "")
    result: list[str] = []
    for url in found:
        url = url.rstrip(".,;)]}>")
        if url and url not in result:
            result.append(url)
    return result


def _safe_stat(path: Path) -> int:
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ValueError(f"不接受符号链接: {path}")
    if not stat.S_ISREG(mode):
        raise ValueError(f"数据库必须是普通文件: {path.name}")
    return mode


class WeChatSource:
    """Read and export a user-supplied, detached, decrypted WeChat vault."""

    def __init__(self, snapshot: str | Path, source_id: str, account_username: str | None = None):
        if not _SOURCE_ID_RE.fullmatch(source_id):
            raise ValueError("source_id 只能包含字母、数字、点、下划线和短横线")
        root = Path(snapshot).expanduser()
        if not root.exists() or not root.is_dir() or root.is_symlink():
            raise ValueError("snapshot 必须是非符号链接目录")
        self.root = root.resolve()
        self.source_id = source_id
        self.account_username = account_username
        self.contact_db = self.root / "contact" / "contact.db"
        self.message_dir = self.root / "message"
        if self.message_dir.is_symlink():
            raise ValueError("不接受符号链接路径: message")
        self.message_dbs = sorted(p for p in self.message_dir.iterdir() if _TABLE_RE.fullmatch(p.name)) if self.message_dir.is_dir() else []
        self.resource_db = self.message_dir / "message_resource.db"
        self.favorite_db = self.root / "favorite" / "favorite.db"
        self.sns_db = self.root / "sns" / "sns.db"
        try:
            self._validate_snapshot()
        except sqlite3.Error as exc:
            raise ValueError("snapshot 包含不可读的 SQLite 数据库，请提供已解密快照") from exc
        self._catalog_stamp = self._source_stamp()
        self.contacts, self.contact_ids = self._load_contacts()
        self._group_relationships = self._load_group_relationships()
        self._name2id_by_db: dict[str, dict[int, str]] = {}
        self._conversation_has_messages: dict[str, bool] = {}
        self._conversation_usernames = self._discover_conversations()
        self._observed_members: dict[str, list[dict]] = {}
        self._conversation_by_id = {
            self.conversation_id(username): username for username in self._conversation_usernames
        }
        self.actors = {
            self.actor_id(username): self._actor_record(username)
            for username in self._known_actor_usernames()
        }
        if self._source_stamp() != self._catalog_stamp:
            raise ValueError("初始化期间 snapshot 已变化，请使用稳定的快照重试")

    def _validate_snapshot(self) -> None:
        self._check_no_links(self.root)
        if not self.contact_db.is_file():
            raise ValueError("缺少 contact/contact.db")
        if not self.message_dbs:
            raise ValueError("缺少 message/message_<数字>.db 或 biz_message_<数字>.db")
        optional_dbs = [db for db in (self.resource_db, self.favorite_db, self.sns_db) if db.exists()]
        for db in [self.contact_db, *self.message_dbs, *optional_dbs]:
            if not db.exists():
                continue
            _safe_stat(db)
            for suffix in ("-wal", "-shm", "-journal"):
                if db.with_name(db.name + suffix).exists():
                    raise ValueError(f"源数据库存在活动 sidecar: {db.name + suffix}")
        with self._connect(self.contact_db) as con:
            if not self._table_exists(con, "contact"):
                raise ValueError("contact.db 缺少 contact 表")
            columns = {row[1] for row in con.execute("PRAGMA table_info(contact)")}
            if not ({"username", "userName"} & columns):
                raise ValueError("contact 表缺少 username/userName")
        for db in self.message_dbs:
            with self._connect(db) as con:
                tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
                if not any(_MSG_TABLE_RE.fullmatch(table) for table in tables):
                    raise ValueError(f"消息库没有可识别的 Msg 表: {db.name}")
                for table in tables:
                    if not _MSG_TABLE_RE.fullmatch(table):
                        continue
                    columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
                    if any(not (set(names) & columns or key == "local_id") for key, names in _REQUIRED_COLUMN_ALIASES.items()):
                        raise ValueError(f"消息表字段不兼容: {db.name}/{table}")
        for db in (self.resource_db, self.favorite_db, self.sns_db):
            if db.exists():
                _safe_stat(db)

    @staticmethod
    def _check_no_links(root: Path) -> None:
        for path in (root, root / "contact", root / "message", root / "favorite", root / "sns"):
            if path.exists() and path.is_symlink():
                raise ValueError(f"不接受符号链接路径: {path}")
            if path.is_dir():
                for child in path.iterdir():
                    if child.is_symlink():
                        raise ValueError(f"不接受符号链接路径: {child}")

    @staticmethod
    @contextmanager
    def _connect(path: Path) -> Iterator[sqlite3.Connection]:
        _safe_stat(path)
        uri = path.as_uri() + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True)
        try:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            yield con
        finally:
            con.close()

    @staticmethod
    def _table_exists(con: sqlite3.Connection, table: str) -> bool:
        return bool(con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())

    def _load_contacts(self) -> tuple[dict[str, dict], dict[int, str]]:
        contacts: dict[str, dict] = {}
        ids: dict[int, str] = {}
        with self._connect(self.contact_db) as con:
            columns = {row[1] for row in con.execute("PRAGMA table_info(contact)")}
            username_col = "username" if "username" in columns else "userName"
            for row in con.execute("SELECT * FROM contact"):
                data = dict(row)
                username = str(data.get(username_col) or "")
                if not username:
                    continue
                try:
                    if data.get("id") is not None:
                        ids[int(data["id"])] = username
                except (TypeError, ValueError):
                    pass
                remark = str(data.get("remark") or "")
                nickname = str(data.get("nick_name") or data.get("nickname") or data.get("nickName") or "")
                alias = str(data.get("alias") or "")
                subscription_value = data.get("is_subscription", data.get("isSubscription", data.get("isSubscribed", False)))
                contacts[username] = {
                    "username": username,
                    "display_name": remark or nickname or alias or username,
                    "remark": remark,
                    "nickname": nickname,
                    "alias": alias,
                    "is_group": "@chatroom" in username,
                    "is_subscription": bool(subscription_value) or username.startswith("gh_"),
                    "local_type": self._optional_int(data.get("local_type")),
                    "delete_flag": self._optional_int(data.get("delete_flag")),
                }
        return contacts, ids

    def _load_group_relationships(self) -> dict[str, dict]:
        """Load chat-room membership edges from contact.db when available.

        The Mac schema stores a chat room's numeric contact id in ``chat_room``
        and its member contact ids in ``chatroom_member``.  Keeping this as a
        separate catalog lets callers distinguish a complete contact-db list
        from the message-observed fallback used by older/incomplete exports.
        """
        relationships: dict[str, dict] = {}
        with self._connect(self.contact_db) as con:
            if not (self._table_exists(con, "chat_room") and self._table_exists(con, "chatroom_member")):
                return relationships
            room_columns = {row[1] for row in con.execute("PRAGMA table_info(chat_room)")}
            username_col = self._pick_column(room_columns, ("username", "userName", "user_name"))
            owner_col = self._pick_column(room_columns, ("owner", "owner_username", "ownerUserName"))
            if username_col is None or "id" not in room_columns:
                return relationships
            owner_expr = f'"{owner_col}"' if owner_col else "NULL"
            query = f'SELECT "id", "{username_col}" AS username, {owner_expr} AS owner FROM "chat_room"'
            member_columns = {row[1] for row in con.execute("PRAGMA table_info(chatroom_member)")}
            room_id_col = self._pick_column(member_columns, ("room_id", "roomId"))
            member_id_col = self._pick_column(member_columns, ("member_id", "memberId"))
            if room_id_col is None or member_id_col is None:
                return relationships
            member_rows: dict[int, list[Any]] = {}
            for row in con.execute(f'SELECT "{room_id_col}", "{member_id_col}" FROM "chatroom_member"'):
                try:
                    room_id = int(row[0])
                except (TypeError, ValueError):
                    continue
                member_rows.setdefault(room_id, []).append(row[1])
            for row in con.execute(query):
                username = str(row["username"] or "")
                if not username or "@chatroom" not in username:
                    continue
                try:
                    room_id = int(row["id"])
                except (TypeError, ValueError):
                    continue
                owner_username = str(row["owner"] or "") or None
                members = []
                seen: set[str] = set()
                for raw_member_id in member_rows.get(room_id, []):
                    try:
                        numeric_id = int(raw_member_id)
                        member_id = str(numeric_id)
                        member_username = self.contact_ids.get(numeric_id)
                    except (TypeError, ValueError):
                        member_id = str(raw_member_id or "")
                        member_username = member_id if member_id in self.contacts else None
                    dedupe_key = member_username or f"id:{member_id}"
                    if not member_id or dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    members.append({
                        "member_id": member_id,
                        "username": member_username,
                        "source": "contact_db",
                        "provenance": {
                            "database": "contact/contact.db",
                            "table": "chatroom_member",
                            "room_id": str(room_id),
                        },
                    })
                relationships[username] = {
                    "room_id": str(room_id),
                    "owner_username": owner_username,
                    "members": members,
                    "membership_available": True,
                }
        return relationships

    def _discover_conversations(self) -> list[str]:
        found: set[str] = set()
        tables_by_db: dict[str, list[str]] = {}
        for db in self.message_dbs:
            with self._connect(db) as con:
                db_name2id: dict[int, str] = {}
                if self._table_exists(con, "Name2Id"):
                    cols = {row[1] for row in con.execute("PRAGMA table_info(Name2Id)")}
                    user_col = (
                        "user_name" if "user_name" in cols
                        else "username" if "username" in cols
                        else "userName" if "userName" in cols
                        else None
                    )
                    if user_col:
                        for row in con.execute(f'SELECT rowid, "{user_col}" FROM Name2Id'):
                            username = str(row[1] or "")
                            if username:
                                db_name2id[int(row[0])] = username
                self._name2id_by_db[db.name] = db_name2id
                tables_by_db[db.name] = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
        known_usernames = set(self.contacts)
        known_usernames.update(self._group_relationships)
        for sender_map in self._name2id_by_db.values():
            known_usernames.update(sender_map.values())
        hash_to_username = {_md5_username(username): username for username in known_usernames}
        for tables in tables_by_db.values():
            for table in tables:
                if _MSG_TABLE_RE.fullmatch(table) and table[4:].lower() in hash_to_username:
                    found.add(hash_to_username[table[4:].lower()])
        # Retain cached groups even if no message table was included in this
        # snapshot. They still have useful member and owner relationships.
        found.update(username for username in known_usernames if "@chatroom" in username)
        for username in found:
            table = "Msg_" + _md5_username(username)
            self._conversation_has_messages[username] = False
            for db in self.message_dbs:
                with self._connect(db) as con:
                    if self._table_exists(con, table) and con.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone():
                        self._conversation_has_messages[username] = True
                        break
        return sorted(found, key=lambda u: (self.display_name(u).casefold(), u))

    def _known_actor_usernames(self) -> set[str]:
        values = set(self.contacts) | set(self._conversation_usernames)
        for sender_map in self._name2id_by_db.values():
            values.update(sender_map.values())
        if self.account_username:
            values.add(self.account_username)
        for relationship in self._group_relationships.values():
            if relationship.get("owner_username"):
                values.add(relationship["owner_username"])
        return values

    def display_name(self, username: str) -> str:
        return self.contacts.get(username, {}).get("display_name") or username

    def conversation_id(self, username: str) -> str:
        return _opaque(self.source_id, "conversation", username)

    def actor_id(self, username: str) -> str:
        return _opaque(self.source_id, "actor", username)

    def _actor_record(self, username: str) -> dict:
        contact = self.contacts.get(username, {})
        group = "@chatroom" in username
        return {
            "id": self.actor_id(username),
            "username": username,
            "display_name": self.display_name(username),
            "kind": "group" if group else "person",
            "account_kind": "official_account" if contact.get("is_subscription") else ("group" if group else "person"),
            "is_subscription": bool(contact.get("is_subscription")),
            "in_contact_database": username in self.contacts,
            **self._friendship_record(username),
        }

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _friendship_record(self, username: str) -> dict:
        contact = self.contacts.get(username, {})
        local_type = contact.get("local_type")
        if username == self.account_username:
            status, is_friend = "self", None
        elif "@chatroom" in username or contact.get("is_subscription"):
            status, is_friend = "not_applicable", None
        elif contact.get("delete_flag") not in (None, 0):
            status, is_friend = "deleted", False
        elif local_type in (1, 5):
            status, is_friend = "friend", True
        elif local_type in (3, 6):
            status, is_friend = "non_friend", False
        else:
            status, is_friend = "unknown", None
        return {
            "is_friend": is_friend,
            "friend_status": status,
            "is_self": username == self.account_username if self.account_username else None,
            "friendship_evidence": {
                "database": "contact/contact.db" if contact else None,
                "local_type": local_type,
                "delete_flag": contact.get("delete_flag"),
            },
        }

    def status(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "snapshot": str(self.root),
            "source_kind": "wechat_mac_decrypted_vault",
            "message_databases": [db.name for db in self.message_dbs],
            "conversation_count": len(self._conversation_usernames),
            "resource_index": self.resource_db.exists(),
            "favorite_index": self.favorite_db.exists(),
            "moments_index": self.sns_db.exists(),
            "group_membership_index": bool(self._group_relationships),
            "friendship_classification": any(c.get("local_type") in (1, 3, 5, 6) for c in self.contacts.values()),
            "decoders": {"utf8": True, "zstandard": zstd is not None},
            "snapshot_version": self._snapshot_token([], None, None),
            "capabilities": [
                "contacts", "conversations", "messages", "search", "filters", "export",
                "official_accounts", "favorites", "moments", "group_members", "relationships",
            ],
            "limitations": ["no_media_body_decode", "no_network", "no_knowledge_store_write"],
        }

    def list_conversations(
        self,
        query: str | None = None,
        *,
        kinds: list[str] | None = None,
        has_messages: bool | None = None,
    ) -> list[dict]:
        snapshot = self._snapshot_token([], None, None)
        normalized_kinds = self._normalize_choice_filter(kinds, _CONTACT_KINDS, "conversation kind")
        q = query.casefold() if query else ""
        rows = []
        for username in self._conversation_usernames:
            display = self.display_name(username)
            kind = "group" if "@chatroom" in username else "person"
            conversation_has_messages = self._conversation_has_messages.get(username, False)
            if normalized_kinds and kind not in normalized_kinds:
                continue
            if has_messages is not None and has_messages != conversation_has_messages:
                continue
            if q and q not in display.casefold() and q not in username.casefold():
                continue
            relationship = self._group_relationships.get(username) if kind == "group" else None
            rows.append({
                "id": self.conversation_id(username),
                "display_name": display,
                "kind": kind,
                "account_kind": "official_account" if (username.startswith("gh_") or self.contacts.get(username, {}).get("is_subscription")) else kind,
                "is_official": bool(username.startswith("gh_") or self.contacts.get(username, {}).get("is_subscription")),
                "message_table": "Msg_" + _md5_username(username),
                "has_messages": conversation_has_messages,
                "member_count": len(relationship["members"]) if relationship and relationship.get("membership_available") else None,
                "owner_actor_id": self.actor_id(relationship["owner_username"])
                if relationship and relationship.get("owner_username") else None,
                "membership_source": "contact_db" if relationship and relationship.get("membership_available") else None,
            })
        if self._snapshot_token([], None, None) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        return rows

    def list_group_members(
        self,
        conversation_id: str,
        *,
        query: str | None = None,
        is_friend: bool | None = None,
        is_owner: bool | None = None,
        limit: int = 5000,
    ) -> dict:
        """List cached group members with local friendship classification."""
        snapshot = self._snapshot_token([], None, None)
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        username = self._conversation_by_id.get(conversation_id)
        if not username:
            raise ValueError(f"未知 conversation_id: {conversation_id}")
        if "@chatroom" not in username:
            raise ValueError("conversation_id 必须指向群聊")
        members, source, complete, owner_username = self._members_for_group(username)
        q = query.casefold().strip() if query else ""
        rows = []
        for member in members:
            member_username = member.get("username")
            owner = bool(owner_username and member_username == owner_username)
            friendship = self._friendship_record(member_username or "")
            display = self.display_name(member_username) if member_username else member.get("member_id", "")
            if is_friend is not None and friendship["is_friend"] is not is_friend:
                continue
            if is_owner is not None and owner is not is_owner:
                continue
            if q and q not in display.casefold() and q not in str(member_username or "").casefold():
                continue
            rows.append({
                "member_id": str(member.get("member_id") or ""),
                "actor_id": self.actor_id(member_username) if member_username else None,
                "username": member_username,
                "display_name": display,
                "in_contact_database": bool(member_username and member_username in self.contacts),
                **friendship,
                "is_owner": owner,
                "provenance": member.get("provenance", {}),
            })
        rows.sort(key=lambda row: (not row["is_owner"], row["display_name"].casefold(), row["member_id"]))
        all_count = len(members)
        conversation = self._conversation_record(conversation_id)
        owner = self._actor_record(owner_username) if owner_username else None
        if owner:
            owner["is_owner"] = True
        counts = {key: 0 for key in ("friend", "non_friend", "unknown", "self", "deleted", "not_applicable")}
        for member in members:
            counts[self._friendship_record(member.get("username") or "")["friend_status"]] += 1
        if self._snapshot_token([], None, None) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "conversation": conversation,
            "owner": owner,
            "member_count": all_count,
            "total_in_scope": len(rows),
            "members": rows[:limit],
            "membership_source": source,
            "complete": complete,
            "friend_counts": counts,
        }

    def list_contact_groups(
        self,
        actor_id: str,
        *,
        query: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Return groups containing one actor, resolved by actor id or username."""
        snapshot = self._snapshot_token([], None, None)
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        username = self._resolve_actor_username(actor_id)
        rows = self._contact_group_rows(username, query)
        if self._snapshot_token([], None, None) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        return rows[:limit]

    def _contact_group_rows(self, username: str, query: str | None) -> list[dict]:
        q = query.casefold().strip() if query else ""
        rows = []
        for group_username in self._conversation_usernames:
            if "@chatroom" not in group_username:
                continue
            members, source, complete, owner_username = self._members_for_group(group_username)
            match = next((item for item in members if item.get("username") == username), None)
            if match is None:
                continue
            display = self.display_name(group_username)
            if q and q not in display.casefold() and q not in group_username.casefold():
                continue
            conversation = self._conversation_record(self.conversation_id(group_username))
            rows.append({
                **conversation,
                "is_owner": username == owner_username,
                "membership_source": source,
                "complete": complete,
            })
        rows.sort(key=lambda row: (row["display_name"].casefold(), row["id"]))
        return rows

    def list_common_groups(
        self,
        actor_ids: list[str],
        *,
        query: str | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Return locally known groups containing every selected actor."""
        snapshot = self._snapshot_token([], None, None)
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        usernames = {self._resolve_actor_username(value) for value in actor_ids}
        if len(usernames) < 2:
            raise ValueError("至少选择两个不同的联系人")
        # Each reverse lookup shares the immutable catalog and observation
        # cache; intersect group ids without inferring missing membership.
        common: set[str] | None = None
        groups = {}
        for username in sorted(usernames):
            rows = self._contact_group_rows(username, query)
            by_id = {row["id"]: row for row in rows}
            common = set(by_id) if common is None else common & set(by_id)
            groups.update(by_id)
        rows = [{key: value for key, value in groups[cid].items() if key != "is_owner"} for cid in common or set()]
        rows.sort(key=lambda row: (row["display_name"].casefold(), row["id"]))
        if self._snapshot_token([], None, None) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        return rows[:limit]

    def list_relationships(
        self,
        *,
        subject_id: str | None = None,
        object_id: str | None = None,
        relationship_types: list[str] | None = None,
        limit: int = 5000,
    ) -> list[dict]:
        """Return normalized actor-to-group relationship edges.

        Current edge types are ``member_of`` and ``owns``.  The shape is kept
        generic so future contacts, messages, and resource relations can use
        the same object graph without changing the identifier contract.
        """
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        snapshot = self._snapshot_token([], None, None)
        allowed = {"member_of", "owns"}
        types = self._normalize_choice_filter(relationship_types, allowed, "relationship type")
        subject_username = self._resolve_actor_username(subject_id) if subject_id else None
        if object_id:
            usernames = self._resolve_usernames([object_id])
            if "@chatroom" not in usernames[0]:
                raise ValueError("object_id 必须指向群聊")
        else:
            usernames = self._conversation_usernames
        rows = self._relationship_rows(usernames)
        rows = [row for row in rows if
                (not types or row["type"] in types) and
                (not subject_username or row["subject_id"] == self.actor_id(subject_username))]
        if self._snapshot_token([], None, None) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        return rows[:limit]

    def _relationship_rows(self, usernames: list[str]) -> list[dict]:
        rows = []
        for group_username in usernames:
            if "@chatroom" not in group_username:
                continue
            conversation_id = self.conversation_id(group_username)
            members, source, complete, owner_username = self._members_for_group(group_username)
            endpoints = []
            for member in members:
                member_username = member.get("username")
                if not member_username:
                    continue
                endpoints.append(("member_of", member_username, member.get("provenance", {}), source, complete))
            if owner_username:
                endpoints.append(("owns", owner_username, {
                    "database": "contact/contact.db", "table": "chat_room",
                    "room_id": self._group_relationships[group_username]["room_id"],
                }, "contact_db", True))
            for relation_type, member_username, provenance, edge_source, edge_complete in endpoints:
                actor_id = self.actor_id(member_username)
                rows.append({
                    "id": _opaque(self.source_id, "relationship", f"{relation_type}:{actor_id}:{conversation_id}"),
                    "type": relation_type,
                    "subject_id": actor_id,
                    "object_id": conversation_id,
                    "subject": self._actor_record(member_username),
                    "object": self._conversation_record(conversation_id),
                    "membership_source": edge_source,
                    "complete": edge_complete,
                    "provenance": provenance,
                })
        rows.sort(key=lambda row: (row["object_id"], row["type"], row["subject_id"]))
        return rows

    def _members_for_group(self, username: str) -> tuple[list[dict], str, bool, str | None]:
        relationship = self._group_relationships.get(username)
        owner_username = relationship.get("owner_username") if relationship else None
        if relationship and relationship.get("membership_available"):
            return list(relationship.get("members", [])), "contact_db", True, owner_username
        if username in self._observed_members:
            rows = self._observed_members[username]
            return rows, "message_observed" if rows else "unavailable", False, owner_username
        # A few old exports omit contact membership tables.  Preserve useful
        # information by returning distinct senders observed in this group's
        # message table and explicitly mark the result incomplete.
        observed: dict[str, dict] = {}
        table = "Msg_" + _md5_username(username)
        for db in self.message_dbs:
            with self._connect(db) as con:
                if not self._table_exists(con, table):
                    continue
                columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
                sender_col = self._pick_column(columns, ("real_sender_id", "sender_id"))
                if not sender_col:
                    continue
                for row in con.execute(f'SELECT DISTINCT "{sender_col}" FROM "{table}" WHERE "{sender_col}" IS NOT NULL'):
                    try:
                        sender_id = int(row[0])
                    except (TypeError, ValueError):
                        continue
                    member_username = self._name2id_by_db.get(db.name, {}).get(sender_id)
                    if not member_username or "@chatroom" in member_username:
                        continue
                    observed.setdefault(member_username, {
                        "member_id": str(sender_id),
                        "username": member_username,
                        "source": "message_observed",
                        "provenance": {
                            "database": f"message/{db.name}",
                            "table": table,
                        },
                    })
        self._observed_members[username] = list(observed.values())
        return self._observed_members[username], "message_observed" if observed else "unavailable", False, owner_username

    def _resolve_actor_username(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("actor_id 不能为空")
        candidate = value.strip()
        known = self._known_actor_usernames()
        if candidate in known:
            return candidate
        for username in known:
            if self.actor_id(username) == candidate:
                return username
        matches = [username for username in known if self.display_name(username).casefold() == candidate.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"联系人名称不唯一，请使用 actor_id 或 username: {candidate}")
        raise ValueError(f"未知 actor_id 或 username: {candidate}")

    def list_contacts(
        self,
        query: str | None = None,
        *,
        kinds: list[str] | None = None,
        is_subscription: bool | None = None,
        is_friend: bool | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """List actors known by the contact database or message sender maps."""
        snapshot = self._snapshot_token([], None, None)
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        normalized_kinds = self._normalize_choice_filter(kinds, _CONTACT_KINDS, "contact kind")
        q = query.casefold().strip() if query else ""
        rows = []
        for username in self._known_actor_usernames():
            record = self._actor_record(username)
            if normalized_kinds and record["kind"] not in normalized_kinds:
                continue
            if is_subscription is not None and record["is_subscription"] is not is_subscription:
                continue
            if is_friend is not None and record["is_friend"] is not is_friend:
                continue
            if q and q not in self.display_name(username).casefold() and q not in username.casefold():
                continue
            rows.append(record)
        rows.sort(key=lambda row: (row["display_name"].casefold(), row["id"]))
        if self._snapshot_token([], None, None) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        return rows[:limit]

    def list_official_accounts(self, query: str | None = None, *, limit: int = 1000) -> list[dict]:
        """List public/official accounts known by contact.db.

        WeChat uses ``gh_`` usernames for most official accounts.  The contact
        table's subscription flag is preferred, with the username prefix as a
        conservative fallback for incomplete contact exports.
        """
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        q = query.casefold().strip() if query else ""
        rows = []
        for username in self._known_actor_usernames():
            contact = self.contacts.get(username, {})
            is_official = bool(contact.get("is_subscription")) or username.startswith("gh_")
            if not is_official:
                continue
            if q and q not in self.display_name(username).casefold() and q not in username.casefold():
                continue
            rows.append({
                **self._actor_record(username),
                "kind": "official_account",
                "account_kind": "official_account",
                "has_messages": bool(self._conversation_has_messages.get(username, False)),
                "conversation_id": self.conversation_id(username) if username in self._conversation_by_id.values() else None,
            })
        rows.sort(key=lambda row: (row["display_name"].casefold(), row["username"]))
        return rows[:limit]

    def list_favorites(
        self,
        query: str | None = None,
        *,
        start: str | None = None,
        end: str | None = None,
        author_usernames: list[str] | None = None,
        author_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        has_links: bool | None = None,
        has_attachments: bool | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Read normalized rows from ``favorite/favorite.db``."""
        self._require_optional_db(self.favorite_db, "favorite/favorite.db")
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        self._validate_bounds(start, end)
        filters = self._normalize_collection_filters(
            author_usernames=author_usernames,
            author_ids=author_ids,
            kinds=kinds,
            allowed_kinds=_FAVORITE_KINDS,
            query=query,
            has_links=has_links,
            has_attachments=has_attachments,
        )
        filters.update({"start": start, "end": end})
        rows = [row for row in self._iter_favorites(start, end) if self._matches_collection(row, filters)]
        return self._page_collection("favorites", rows, filters, limit, cursor)

    def search_favorites(self, query: str, **filters: Any) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        return self.list_favorites(query=query, **filters)

    def list_moments(
        self,
        query: str | None = None,
        *,
        start: str | None = None,
        end: str | None = None,
        author_usernames: list[str] | None = None,
        author_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        has_links: bool | None = None,
        has_attachments: bool | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Read normalized rows from ``sns/sns.db`` without fetching media."""
        self._require_optional_db(self.sns_db, "sns/sns.db")
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        self._validate_bounds(start, end)
        filters = self._normalize_collection_filters(
            author_usernames=author_usernames,
            author_ids=author_ids,
            kinds=kinds,
            allowed_kinds=_MOMENT_KINDS,
            query=query,
            has_links=has_links,
            has_attachments=has_attachments,
        )
        filters.update({"start": start, "end": end})
        rows = [row for row in self._iter_moments(start, end) if self._matches_collection(row, filters)]
        return self._page_collection("moments", rows, filters, limit, cursor)

    def search_moments(self, query: str, **filters: Any) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        return self.list_moments(query=query, **filters)

    def search_all(self, query: str, *, scopes: list[str] | None = None, start: str | None = None, end: str | None = None, limit: int = 100) -> dict:
        """Search messages, favorites, and moments using one stable envelope."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        selected = scopes or ["messages", "favorites", "moments"]
        allowed = {"messages", "favorites", "moments"}
        unknown = set(selected) - allowed
        if unknown:
            raise ValueError(f"不支持的搜索范围: {sorted(unknown)}")
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        results: list[dict] = []
        unavailable: dict[str, str] = {}
        if "messages" in selected:
            results.extend(self.search_items(query, start=start, end=end, limit=5000)["items"])
        if "favorites" in selected:
            try:
                results.extend(self.search_favorites(query, start=start, end=end, limit=5000)["items"])
            except ValueError as exc:
                unavailable["favorites"] = str(exc)
        if "moments" in selected:
            try:
                results.extend(self.search_moments(query, start=start, end=end, limit=5000)["items"])
            except ValueError as exc:
                unavailable["moments"] = str(exc)
        results.sort(key=lambda item: (item.get("created_at") or "", item.get("id", "")), reverse=True)
        page = results[:limit]
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "query": query,
            "items": page,
            "total_in_scope": len(results),
            "unavailable": unavailable,
        }

    def export_favorites(self, output_root: str | Path, *, previous: str | Path | None = None, **filters: Any) -> dict:
        self._require_optional_db(self.favorite_db, "favorite/favorite.db")
        normalized = self._normalize_collection_filters_from_kwargs(filters, _FAVORITE_KINDS)
        rows = [row for row in self._iter_favorites(filters.get("start"), filters.get("end")) if self._matches_collection(row, normalized)]
        return self._export_collection_bundle(output_root, "favorites", rows, normalized, previous)

    def export_moments(self, output_root: str | Path, *, previous: str | Path | None = None, **filters: Any) -> dict:
        self._require_optional_db(self.sns_db, "sns/sns.db")
        normalized = self._normalize_collection_filters_from_kwargs(filters, _MOMENT_KINDS)
        rows = [row for row in self._iter_moments(filters.get("start"), filters.get("end")) if self._matches_collection(row, normalized)]
        return self._export_collection_bundle(output_root, "moments", rows, normalized, previous)

    @staticmethod
    def _require_optional_db(path: Path, label: str) -> None:
        if not path.is_file():
            raise ValueError(f"缺少 {label}")

    @staticmethod
    def _row_value(row: sqlite3.Row, aliases: tuple[str, ...], default: Any = None) -> Any:
        keys = {str(key).casefold(): key for key in row.keys()}
        for alias in aliases:
            key = keys.get(alias.casefold())
            if key is not None:
                return row[key]
        return default

    @staticmethod
    def _pick_column(columns: set[str], aliases: tuple[str, ...]) -> str | None:
        by_lower = {column.casefold(): column for column in columns}
        return next((by_lower[name.casefold()] for name in aliases if name.casefold() in by_lower), None)

    @staticmethod
    def _quote_identifier(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def _epoch(value: Any) -> int:
        try:
            timestamp = int(value or 0)
        except (TypeError, ValueError):
            return 0
        if timestamp > 100_000_000_000:
            timestamp //= 1000
        return timestamp

    def _normalize_collection_filters_from_kwargs(self, filters: dict[str, Any], allowed_kinds: set[str]) -> dict:
        self._validate_bounds(filters.get("start"), filters.get("end"))
        normalized = self._normalize_collection_filters(
            author_usernames=filters.get("author_usernames"),
            author_ids=filters.get("author_ids"),
            kinds=filters.get("kinds"),
            allowed_kinds=allowed_kinds,
            query=filters.get("query"),
            has_links=filters.get("has_links"),
            has_attachments=filters.get("has_attachments"),
        )
        normalized.update({"start": filters.get("start"), "end": filters.get("end")})
        return normalized

    def _normalize_collection_filters(
        self,
        *,
        author_usernames: list[str] | None,
        author_ids: list[str] | None,
        kinds: list[str] | None,
        allowed_kinds: set[str],
        query: str | None,
        has_links: bool | None,
        has_attachments: bool | None,
    ) -> dict:
        known = self._known_actor_usernames()
        by_display: dict[str, list[str]] = {}
        for username in known:
            by_display.setdefault(self.display_name(username).casefold(), []).append(username)
        resolved_usernames: set[str] = set()
        for value in author_usernames or []:
            candidate = str(value).strip()
            if not candidate:
                raise ValueError("author_usernames 不能包含空值")
            if candidate in known:
                resolved_usernames.add(candidate)
                continue
            matches = by_display.get(candidate.casefold(), [])
            if len(matches) > 1:
                raise ValueError(f"发言人名称不唯一，请使用 username: {candidate}")
            resolved_usernames.add(matches[0] if matches else candidate)
        ids = {str(value).strip() for value in (author_ids or []) if str(value).strip()}
        known_ids = {self.actor_id(username) for username in known}
        if ids - known_ids:
            raise ValueError(f"未知 author_id: {sorted(ids - known_ids)[0]}")
        normalized_kinds = self._normalize_choice_filter(kinds, allowed_kinds, "resource kind")
        return {
            "author_usernames": sorted(resolved_usernames),
            "author_ids": sorted(ids | {self.actor_id(username) for username in resolved_usernames}),
            "kinds": normalized_kinds,
            "query": str(query or "").casefold().strip(),
            "has_links": has_links,
            "has_attachments": has_attachments,
        }

    @staticmethod
    def _matches_collection(item: dict, filters: dict) -> bool:
        usernames = filters.get("author_usernames", [])
        ids = filters.get("author_ids", [])
        if usernames and item.get("author_username") not in usernames:
            return False
        if ids and item.get("author_id") not in ids:
            return False
        if filters.get("kinds") and item.get("kind") not in filters["kinds"]:
            return False
        if filters.get("has_links") is not None and bool(item.get("links")) is not filters["has_links"]:
            return False
        if filters.get("has_attachments") is not None and bool(item.get("attachments")) is not filters["has_attachments"]:
            return False
        query = filters.get("query", "")
        if query:
            haystack = " ".join([
                str(item.get("text") or ""), str(item.get("title") or ""),
                str(item.get("author_username") or ""),
                " ".join(item.get("links") or []),
                " ".join(str(x.get("name") or "") for x in item.get("attachments") or []),
            ]).casefold()
            if query not in haystack:
                return False
        return True

    def _page_collection(self, resource: str, rows: list[dict], filters: dict, limit: int, cursor: str | None) -> dict:
        rows.sort(key=lambda item: (item.get("created_at") or "", item.get("id", "")), reverse=True)
        snapshot = self._collection_snapshot(resource, filters)
        position = 0
        if cursor:
            try:
                decoded = json.loads(bytes.fromhex(cursor).decode("utf-8"))
                if decoded.get("resource") != resource or decoded.get("snapshot") != snapshot:
                    raise ValueError
                position = int(decoded["position"])
                if position < 0:
                    raise ValueError
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("无效或过期 cursor") from exc
        if self._collection_snapshot(resource, filters) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        page = rows[position:position + limit]
        next_cursor = None
        if position + limit < len(rows):
            next_cursor = self._encode_cursor({"resource": resource, "snapshot": snapshot, "position": position + limit})
        return {"schema_version": SCHEMA_VERSION, "source_id": self.source_id, "resource_type": resource, "snapshot": snapshot, "items": page, "next_cursor": next_cursor, "total_in_scope": len(rows)}

    def _collection_snapshot(self, resource: str, filters: dict) -> str:
        return _sha("\0".join([self.source_id, resource, self._source_stamp(), json.dumps(filters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]))[:32]

    def _iter_favorites(self, start: str | None, end: str | None) -> Iterator[dict]:
        start_ts = self._parse_iso(start) if start else None
        end_ts = self._parse_iso(end) if end else None
        with self._connect(self.favorite_db) as con:
            if not self._table_exists(con, "fav_db_item"):
                raise ValueError("favorite.db 缺少 fav_db_item 表")
            columns = {row[1] for row in con.execute('PRAGMA table_info("fav_db_item")')}
            selected = ['rowid AS __rowid__']
            column_aliases = {
                "local_id": ("local_id", "localId", "id"),
                "type": ("type", "fav_type"),
                "update_time": ("update_time", "updateTime", "create_time", "timestamp", "time"),
                "content": ("content", "fav_content", "favContent", "data"),
                "fromusr": ("fromusr", "from_user", "fromUser"),
                "realchatname": ("realchatname", "real_chat_name", "chat_name"),
            }
            for alias, names in column_aliases.items():
                column = self._pick_column(columns, names)
                selected.append(f'{self._quote_identifier(column)} AS "{alias}"' if column else f'NULL AS "{alias}"')
            for row in con.execute(f'SELECT {", ".join(selected)} FROM "fav_db_item"'):
                local_id = str(self._row_value(row, ("local_id", "localId", "id"), row["__rowid__"]))
                fav_type = int(self._row_value(row, ("type", "fav_type"), 0) or 0)
                timestamp = self._epoch(self._row_value(row, ("update_time", "updateTime", "create_time", "timestamp", "time"), 0))
                if start_ts and (not timestamp or timestamp < start_ts):
                    continue
                if end_ts and (not timestamp or timestamp >= end_ts):
                    continue
                raw = self._row_value(row, ("content", "fav_content", "favContent", "data"), "")
                content, decode_error = _decode_bytes(raw)
                parsed = self._parse_favorite_payload(content, fav_type)
                author_username = str(self._row_value(row, ("fromusr", "from_user", "fromUser"), "") or "") or None
                source_chat = str(self._row_value(row, ("realchatname", "real_chat_name", "chat_name"), "") or "") or None
                item = {
                    "schema_version": SCHEMA_VERSION,
                    "resource_type": "favorite",
                    "id": _opaque(self.source_id, "favorite", local_id),
                    "source_id": self.source_id,
                    "author_id": self.actor_id(author_username) if author_username else None,
                    "author_username": author_username,
                    "created_at": _utc_timestamp(timestamp),
                    "kind": _FAVORITE_TYPE_MAP.get(fav_type, "unknown"),
                    "text": parsed["text"],
                    "title": parsed["title"] or None,
                    "links": parsed["links"],
                    "attachments": parsed["attachments"],
                    "source_chat": source_chat,
                    "quality": {"status": "decode_error" if decode_error else ("metadata_only" if parsed["attachments"] and not parsed["text"] else "complete"), "reason": decode_error},
                    "provenance": {"database": self.favorite_db.name, "table": "fav_db_item", "local_id": local_id, "favorite_type": fav_type, "raw_content_sha256": _sha(raw if isinstance(raw, bytes) else str(raw or ""))},
                }
                item["revision"] = _sha(json.dumps({key: item[key] for key in ("author_id", "created_at", "kind", "text", "title", "links", "attachments", "source_chat", "quality")}, ensure_ascii=False, sort_keys=True))
                yield item

    @staticmethod
    def _parse_favorite_payload(content: str, fav_type: int) -> dict:
        root = _parse_xml(content)
        if root is None:
            return {"text": content.strip()[:_MAX_DECODED_BYTES], "title": "", "links": _extract_urls(content), "attachments": []}
        title = _xml_text(root, ".//pagetitle", ".//title", ".//filename", ".//nickname")
        text = _xml_text(root, ".//desc", ".//content", ".//description")
        if fav_type == 1 and not text:
            text = " ".join(part.strip() for part in root.itertext() if part.strip())
        links = _extract_urls(" ".join(root.itertext()))
        attachments = []
        for media in root.findall(".//media"):
            media_type = _xml_text(media, ".//type") or "media"
            url = _xml_text(media, ".//url", ".//thumb")
            attachments.append({"kind": media_type, "name": _xml_text(media, ".//name", ".//filename") or None, "url": url or None, "availability": "metadata_only"})
        if fav_type == 2 and not attachments:
            attachments.append({"kind": "image", "name": None, "availability": "metadata_only"})
        return {"text": text.strip() or None, "title": title, "links": sorted(set(links)), "attachments": attachments}

    def _iter_moments(self, start: str | None, end: str | None) -> Iterator[dict]:
        start_ts = self._parse_iso(start) if start else None
        end_ts = self._parse_iso(end) if end else None
        with self._connect(self.sns_db) as con:
            if not self._table_exists(con, "SnsTimeLine"):
                raise ValueError("sns.db 缺少 SnsTimeLine 表")
            columns = {row[1] for row in con.execute('PRAGMA table_info("SnsTimeLine")')}
            tid_column = self._pick_column(columns, ("tid", "id"))
            username_column = self._pick_column(columns, ("user_name", "username", "userName"))
            content_column = self._pick_column(columns, ("content", "xml", "content_xml"))
            if content_column is None:
                raise ValueError("sns.db 的 SnsTimeLine 表缺少 content/xml 字段")
            selected = ['rowid AS __rowid__']
            selected.append(f'{self._quote_identifier(tid_column)} AS "tid"' if tid_column else 'NULL AS "tid"')
            selected.append(f'{self._quote_identifier(username_column)} AS "user_name"' if username_column else 'NULL AS "user_name"')
            selected.append(f'{self._quote_identifier(content_column)} AS "content"')
            for row in con.execute(f'SELECT {", ".join(selected)} FROM "SnsTimeLine"'):
                local_id = str(self._row_value(row, ("tid", "id"), row["__rowid__"]))
                db_username = str(self._row_value(row, ("user_name", "username", "userName"), "") or "")
                raw = self._row_value(row, ("content", "xml", "content_xml"), "")
                content, decode_error = _decode_bytes(raw)
                parsed = self._parse_moment_payload(content, db_username)
                timestamp = self._epoch(parsed.pop("_timestamp", 0))
                if start_ts and (not timestamp or timestamp < start_ts):
                    continue
                if end_ts and (not timestamp or timestamp >= end_ts):
                    continue
                username = parsed.pop("_username") or db_username or None
                attachments = parsed["attachments"]
                item = {
                    "schema_version": SCHEMA_VERSION,
                    "resource_type": "moment",
                    "id": _opaque(self.source_id, "moment", local_id),
                    "source_id": self.source_id,
                    "author_id": self.actor_id(username) if username else None,
                    "author_username": username,
                    "created_at": _utc_timestamp(timestamp),
                    "kind": parsed["kind"],
                    "text": parsed["text"],
                    "title": parsed["title"] or None,
                    "links": parsed["links"],
                    "attachments": attachments,
                    "post_type": parsed["post_type"],
                    "quality": {"status": "decode_error" if decode_error else ("metadata_only" if attachments and not parsed["text"] else "complete"), "reason": decode_error},
                    "provenance": {"database": self.sns_db.name, "table": "SnsTimeLine", "local_id": local_id, "raw_content_sha256": _sha(raw if isinstance(raw, bytes) else str(raw or ""))},
                }
                item["revision"] = _sha(json.dumps({key: item[key] for key in ("author_id", "created_at", "kind", "text", "title", "links", "attachments", "post_type", "quality")}, ensure_ascii=False, sort_keys=True))
                yield item

    @staticmethod
    def _parse_moment_payload(content: str, fallback_username: str) -> dict:
        root = _parse_xml(content)
        if root is None:
            return {"_username": fallback_username, "_timestamp": 0, "kind": "unknown", "text": content.strip() or None, "title": "", "links": _extract_urls(content), "attachments": [], "post_type": ""}
        if root.tag == "TimelineObject":
            timeline = root
        else:
            timeline = root.find(".//TimelineObject")
            if timeline is None:
                timeline = root
        timestamp = _xml_text(timeline, "createTime", "create_time", ".//createTime")
        username = _xml_text(timeline, "username", ".//username") or fallback_username
        text = _xml_text(timeline, "contentDesc", ".//contentDesc", ".//desc", ".//description")
        title = _xml_text(timeline, ".//title")
        post_type = _xml_text(timeline, "ContentObject/contentStyle", ".//contentStyle", ".//contentSubStyle")
        links = _extract_urls(" ".join(timeline.itertext()))
        attachments = []
        media_types = []
        for media in timeline.findall(".//media"):
            media_type = _xml_text(media, "type") or "media"
            media_types.append(media_type.casefold())
            attachments.append({"kind": media_type, "name": _xml_text(media, "name", "filename") or None, "url": _xml_text(media, "url", "thumb") or None, "availability": "metadata_only"})
        if any("video" in value for value in media_types) or post_type in {"6", "2"}:
            kind = "video"
        elif attachments:
            kind = "image"
        elif links:
            kind = "link"
        elif text:
            kind = "text"
        else:
            kind = "unknown"
        if attachments and text and kind in {"image", "video"}:
            kind = "mixed"
        return {"_username": username, "_timestamp": int(timestamp) if timestamp.isdigit() else 0, "kind": kind, "text": text.strip() or None, "title": title, "links": sorted(set(links)), "attachments": attachments, "post_type": post_type}

    def _export_collection_bundle(self, output_root: str | Path, resource: str, rows: list[dict], filters: dict, previous: str | Path | None) -> dict:
        requested = Path(output_root).expanduser().resolve()
        if self.root == requested or self.root in requested.parents or requested in self.root.parents:
            raise ValueError("导出目录不能与 snapshot 重叠")
        output = requested
        suffix = 1
        while output.exists():
            output = requested.with_name(f"{requested.name}-{suffix:03d}")
            suffix += 1
        snapshot = self._collection_snapshot(resource, filters)
        old_items = []
        if previous:
            previous_path = Path(previous).expanduser().resolve()
            if not previous_path.is_dir() or not (previous_path / "manifest.json").is_file():
                raise ValueError("previous 必须是之前的资源导出目录")
            previous_manifest = json.loads((previous_path / "manifest.json").read_text(encoding="utf-8"))
            if previous_manifest.get("schema_version") != SCHEMA_VERSION or previous_manifest.get("resource_type") != resource or previous_manifest.get("source_id") != self.source_id or previous_manifest.get("filters") != filters:
                raise ValueError("previous 与当前 source 或筛选范围不匹配")
            old_items = self._load_jsonl(previous_path / "items.jsonl")
        old_by_id = {item["id"]: item for item in old_items}
        new_by_id = {item["id"]: item for item in rows}
        changes = []
        for item_id, item in new_by_id.items():
            if item_id not in old_by_id:
                changes.append({"op": "added", "item": item})
            elif old_by_id[item_id].get("revision") != item.get("revision"):
                changes.append({"op": "updated", "item": item})
        manifest = {"schema_version": SCHEMA_VERSION, "source_id": self.source_id, "source_kind": "wechat_mac_decrypted_vault", "resource_type": resource, "privacy": {"visibility": "private", "storage": "local", "trust": "untrusted"}, "snapshot": snapshot, "filters": filters, "counts": {"items": len(new_by_id), "added": sum(change["op"] == "added" for change in changes), "updated": sum(change["op"] == "updated" for change in changes), "missing_from_previous": len(set(old_by_id) - set(new_by_id))}}
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(output, 0o700)
        actors = {}
        for item in new_by_id.values():
            username = item.get("author_username")
            if username:
                actors[self.actor_id(username)] = self._actor_record(username)
        try:
            self._write_jsonl(output / "actors.jsonl", [actors[key] for key in sorted(actors)])
            self._write_jsonl(output / "items.jsonl", list(new_by_id.values()))
            self._write_jsonl(output / "changes.jsonl", changes)
            self._write_json(output / "manifest.json", manifest)
        except Exception:
            shutil.rmtree(output)
            raise
        return {"path": str(output), "manifest": manifest, "files": sorted(path.name for path in output.iterdir())}

    def search_items(self, query: str, conversation_ids: list[str] | None = None, **filters: Any) -> dict:
        """Search message records globally or within selected conversations."""
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query 不能为空")
        if "query" in filters:
            raise ValueError("search_items 不接受重复的 query 参数")
        ids = conversation_ids or [self.conversation_id(username) for username in self._conversation_usernames]
        return self.read_items(ids, query=query, **filters)

    def read_items(
        self,
        conversation_ids: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        author_usernames: list[str] | None = None,
        author_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        directions: list[str] | None = None,
        qualities: list[str] | None = None,
        query: str | None = None,
        has_links: bool | None = None,
        has_attachments: bool | None = None,
        limit: int = 100,
        cursor: str | None = None,
        official_only: bool | None = None,
    ) -> dict:
        if not conversation_ids:
            raise ValueError("至少选择一个 conversation_id")
        if not 1 <= limit <= 5000:
            raise ValueError("limit 必须在 1 到 5000 之间")
        self._validate_bounds(start, end)
        usernames = self._resolve_usernames(conversation_ids)
        if official_only is not None:
            usernames = [username for username in usernames if (username.startswith("gh_") or bool(self.contacts.get(username, {}).get("is_subscription"))) is official_only]
        filters = self._normalize_filters(
            author_usernames=author_usernames,
            author_ids=author_ids,
            kinds=kinds,
            directions=directions,
            qualities=qualities,
            query=query,
            has_links=has_links,
            has_attachments=has_attachments,
        )
        filters["official_only"] = official_only
        snapshot = self._snapshot_token(usernames, start, end, filters)
        position = 0
        if cursor:
            try:
                decoded = json.loads(bytes.fromhex(cursor).decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise ValueError("cursor 必须包含一个对象")
                if decoded.get("snapshot") != snapshot:
                    raise ValueError("cursor 与当前筛选范围或源版本不匹配")
                position = int(decoded["position"])
                if position < 0:
                    raise ValueError("cursor position 不能为负数")
            except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("无效或过期 cursor") from exc
        observations = self._sorted_items(usernames, start, end, filters)
        if self._snapshot_token(usernames, start, end, filters) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        page = observations[position:position + limit]
        next_cursor = None
        if position + limit < len(observations):
            next_cursor = self._encode_cursor({"snapshot": snapshot, "position": position + limit})
        return {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "snapshot": snapshot,
            "items": page,
            "next_cursor": next_cursor,
            "total_in_scope": len(observations),
        }

    def export_bundle(
        self,
        output_root: str | Path,
        conversation_ids: list[str],
        *,
        start: str | None = None,
        end: str | None = None,
        author_usernames: list[str] | None = None,
        author_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        directions: list[str] | None = None,
        qualities: list[str] | None = None,
        query: str | None = None,
        has_links: bool | None = None,
        has_attachments: bool | None = None,
        previous: str | Path | None = None,
        official_only: bool | None = None,
    ) -> dict:
        if not conversation_ids:
            raise ValueError("至少选择一个 conversation_id")
        requested_output = Path(output_root).expanduser().resolve()
        if self.root == requested_output or self.root in requested_output.parents or requested_output in self.root.parents:
            raise ValueError("导出目录不能与 snapshot 重叠")
        output = requested_output
        suffix = 1
        while output.exists():
            output = requested_output.with_name(f"{requested_output.name}-{suffix:03d}")
            suffix += 1
        self._validate_bounds(start, end)
        usernames = self._resolve_usernames(conversation_ids)
        if official_only is not None:
            usernames = [username for username in usernames if (username.startswith("gh_") or bool(self.contacts.get(username, {}).get("is_subscription"))) is official_only]
        filters = self._normalize_filters(
            author_usernames=author_usernames,
            author_ids=author_ids,
            kinds=kinds,
            directions=directions,
            qualities=qualities,
            query=query,
            has_links=has_links,
            has_attachments=has_attachments,
        )
        filters["official_only"] = official_only
        snapshot = self._snapshot_token(usernames, start, end, filters)
        items = self._sorted_items(usernames, start, end, filters)
        if self._snapshot_token(usernames, start, end, filters) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        actors = {item["author_id"] for item in items if item.get("author_id")}
        conversations = {self.conversation_id(username) for username in usernames}
        relationships = self._relationship_rows(usernames)
        if self._snapshot_token(usernames, start, end, filters) != snapshot:
            raise ValueError("读取期间 snapshot 已变化，请使用稳定的快照重试")
        actors.update(row["subject_id"] for row in relationships)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "source_id": self.source_id,
            "source_kind": "wechat_mac_decrypted_vault",
            "privacy": {"visibility": "private", "storage": "local", "trust": "untrusted"},
            "snapshot": snapshot,
            "filters": filters,
            "scope": {
                "conversation_ids": sorted(set(conversation_ids)),
                "start": start,
                "end": end,
                "filters": filters,
            },
            "counts": {"items": len(items), "actors": len(actors), "conversations": len(conversations), "relationships": len(relationships), "duplicate_observations": 0},
            "limitations": ["media_body_not_decoded", "source_is_untrusted_data"],
        }
        if previous:
            previous_path = Path(previous).expanduser().resolve()
            if not previous_path.is_dir():
                raise ValueError("previous 必须是之前的导出目录")
            previous_manifest_path = previous_path / "manifest.json"
            if not previous_manifest_path.is_file():
                raise ValueError("previous 缺少 manifest.json")
            previous_manifest = json.loads(previous_manifest_path.read_text(encoding="utf-8"))
            previous_scope = previous_manifest.get("scope")
            legacy_scope = {"conversation_ids": manifest["scope"]["conversation_ids"], "start": start, "end": end}
            same_scope = previous_scope == manifest["scope"] or (
                not any(filters.values()) and previous_scope == legacy_scope
            )
            if previous_manifest.get("schema_version") != SCHEMA_VERSION or previous_manifest.get("source_id") != self.source_id or not same_scope:
                raise ValueError("previous 与当前 source 或筛选范围不匹配")
            old_items = self._load_jsonl(previous_path / "items.jsonl")
        else:
            old_items = []
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(output, 0o700)
        old_by_id = {item["id"]: item for item in old_items}
        new_by_id: dict[str, dict] = {}
        for item in items:
            existing = new_by_id.get(item["id"])
            if existing is None or bool(item.get("author_id")) > bool(existing.get("author_id")):
                new_by_id[item["id"]] = item
        if len(items) != len(new_by_id):
            manifest["counts"]["duplicate_observations"] = len(items) - len(new_by_id)
            items = list(new_by_id.values())
        changes = []
        for item_id, item in new_by_id.items():
            if item_id not in old_by_id:
                changes.append({"op": "added", "item": item})
            elif old_by_id[item_id].get("revision") != item.get("revision"):
                changes.append({"op": "updated", "item": item})
        manifest["counts"].update({
            "items": len(new_by_id),
            "added": sum(change["op"] == "added" for change in changes),
            "updated": sum(change["op"] == "updated" for change in changes),
            "missing_from_previous": len(set(old_by_id) - set(new_by_id)),
        })
        actors = {item["author_id"] for item in items if item.get("author_id")}
        actors.update(row["subject_id"] for row in relationships)
        manifest["counts"]["actors"] = len(actors)
        try:
            self._write_jsonl(output / "actors.jsonl", [self.actors[actor_id] for actor_id in sorted(actors)])
            self._write_jsonl(output / "conversations.jsonl", [self._conversation_record(cid) for cid in sorted(conversations)])
            self._write_jsonl(output / "relationships.jsonl", relationships)
            self._write_jsonl(output / "items.jsonl", items)
            self._write_jsonl(output / "changes.jsonl", changes)
            self._write_json(output / "manifest.json", manifest)
        except Exception:
            shutil.rmtree(output)
            raise
        return {"path": str(output), "manifest": manifest, "files": sorted(p.name for p in output.iterdir())}

    def _conversation_record(self, conversation_id: str) -> dict:
        username = self._conversation_by_id[conversation_id]
        kind = "group" if "@chatroom" in username else "person"
        relationship = self._group_relationships.get(username) if kind == "group" else None
        return {
            "id": conversation_id,
            "display_name": self.display_name(username),
            "kind": kind,
            "member_count": len(relationship["members"]) if relationship and relationship.get("membership_available") else None,
            "owner_actor_id": self.actor_id(relationship["owner_username"])
            if relationship and relationship.get("owner_username") else None,
            "membership_source": "contact_db" if relationship and relationship.get("membership_available") else None,
        }

    @staticmethod
    def _validate_bounds(start: str | None, end: str | None) -> None:
        for label, value in (("start", start), ("end", end)):
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{label} 必须是有效的 ISO 8601 时间并包含时区") from exc
            if parsed.tzinfo is None:
                raise ValueError(f"{label} 必须使用带时区的 ISO 8601 时间")
        start_ts = WeChatSource._parse_iso(start) if start else None
        end_ts = WeChatSource._parse_iso(end) if end else None
        if start_ts is not None and end_ts is not None and start_ts >= end_ts:
            raise ValueError("start 必须早于 end")

    def _resolve_usernames(self, conversation_ids: list[str]) -> list[str]:
        usernames = []
        for conversation_id in conversation_ids:
            username = self._conversation_by_id.get(conversation_id)
            if not username:
                raise ValueError(f"未知 conversation_id: {conversation_id}")
            if username not in usernames:
                usernames.append(username)
        return usernames

    def _sorted_items(self, usernames: list[str], start: str | None, end: str | None, filters: dict) -> list[dict]:
        observations = list(self._iter_items(usernames, start, end))
        observations = [item for item in observations if self._matches_filters(item, filters)]
        observations.sort(key=lambda item: (
            item.get("created_at") or "",
            (0, int(item.get("provenance", {}).get("local_id", 0)))
            if str(item.get("provenance", {}).get("local_id", "")).lstrip("-").isdigit()
            else (1, str(item.get("provenance", {}).get("local_id", ""))),
            item["id"],
        ))
        return observations

    def _snapshot_token(self, usernames: list[str], start: str | None, end: str | None, filters: dict | None = None) -> str:
        stamp = self._source_stamp()
        if stamp != self._catalog_stamp:
            raise ValueError("snapshot 已变化，请重新创建连接器后开始读取")
        parts = [
            self.source_id,
            self.account_username or "",
            stamp,
            start or "",
            end or "",
            json.dumps(filters or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            *sorted(usernames),
        ]
        return _sha("\0".join(parts))[:32]

    def _normalize_filters(
        self,
        *,
        author_usernames: list[str] | None,
        author_ids: list[str] | None,
        kinds: list[str] | None,
        directions: list[str] | None,
        qualities: list[str] | None,
        query: str | None,
        has_links: bool | None,
        has_attachments: bool | None,
    ) -> dict:
        usernames = self._known_actor_usernames()
        by_display: dict[str, list[str]] = {}
        for username in usernames:
            by_display.setdefault(self.display_name(username).casefold(), []).append(username)
        resolved_authors: set[str] = set()
        for value in author_usernames or []:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("author_usernames 不能包含空值")
            candidate = value.strip()
            if candidate in usernames:
                resolved_authors.add(self.actor_id(candidate))
                continue
            matches = by_display.get(candidate.casefold(), [])
            if not matches:
                raise ValueError(f"未知发言人或联系人名称: {candidate}")
            if len(matches) > 1:
                raise ValueError(f"发言人名称不唯一，请使用 username: {candidate}")
            resolved_authors.add(self.actor_id(matches[0]))
        ids = {str(value).strip() for value in (author_ids or []) if str(value).strip()}
        known_actor_ids = {self.actor_id(username) for username in usernames}
        unknown_ids = ids - known_actor_ids
        if unknown_ids:
            raise ValueError(f"未知 author_id: {sorted(unknown_ids)[0]}")
        resolved_authors.update(ids)
        normalized_kinds = self._normalize_choice_filter(kinds, _FILTER_KINDS, "kind")
        normalized_directions = self._normalize_choice_filter(directions, _FILTER_DIRECTIONS, "direction")
        normalized_qualities = self._normalize_choice_filter(qualities, _FILTER_QUALITY, "quality")
        normalized_query = query.casefold().strip() if query else ""
        return {
            "author_ids": sorted(resolved_authors),
            "kinds": normalized_kinds,
            "directions": normalized_directions,
            "qualities": normalized_qualities,
            "query": normalized_query,
            "has_links": has_links,
            "has_attachments": has_attachments,
        }

    @staticmethod
    def _normalize_choice_filter(values: list[str] | None, allowed: set[str], label: str) -> list[str]:
        if not values:
            return []
        normalized = sorted({str(value).strip().casefold() for value in values if str(value).strip()})
        invalid = set(normalized) - allowed
        if invalid:
            raise ValueError(f"不支持的 {label} 过滤值: {sorted(invalid)}")
        return normalized

    @staticmethod
    def _matches_filters(item: dict, filters: dict) -> bool:
        if filters["author_ids"] and item.get("author_id") not in filters["author_ids"]:
            return False
        if filters["kinds"] and item.get("kind") not in filters["kinds"]:
            return False
        if filters["directions"] and item.get("direction") not in filters["directions"]:
            return False
        if filters["qualities"] and item.get("quality", {}).get("status") not in filters["qualities"]:
            return False
        if filters["has_links"] is not None and bool(item.get("links")) is not filters["has_links"]:
            return False
        if filters["has_attachments"] is not None and bool(item.get("attachments")) is not filters["has_attachments"]:
            return False
        query = filters["query"]
        if query:
            haystack = " ".join([
                str(item.get("text") or ""),
                str(item.get("title") or ""),
                " ".join(str(link) for link in item.get("links", [])),
                " ".join(str(attachment.get("name") or "") for attachment in item.get("attachments", [])),
            ]).casefold()
            if query not in haystack:
                return False
        return True

    def _source_stamp(self) -> str:
        self._check_no_links(self.root)
        # Discover the file list again so added/removed shards also invalidate a
        # connector whose contact and sender catalogs were already loaded.
        paths = sorted(p for p in self.message_dir.iterdir() if _TABLE_RE.fullmatch(p.name))
        source_paths = [*paths, self.contact_db]
        for optional_db in (self.resource_db, self.favorite_db, self.sns_db):
            if optional_db.exists():
                source_paths.append(optional_db)
        parts = []
        for path in source_paths:
            _safe_stat(path)
            for suffix in ("-wal", "-shm", "-journal"):
                if path.with_name(path.name + suffix).exists():
                    raise ValueError("snapshot 存在活动 sidecar，请提供稳定的离线快照")
            st = path.stat()
            parts.extend([path.name, str(st.st_ino), str(st.st_size), str(st.st_mtime_ns), str(st.st_ctime_ns)])
        return _sha("\0".join(parts))

    @staticmethod
    def _encode_cursor(value: dict) -> str:
        return json.dumps(value, separators=(",", ":")).encode().hex()

    def _iter_items(self, usernames: list[str], start: str | None, end: str | None) -> Iterator[dict]:
        start_ts = self._parse_iso(start) if start else None
        end_ts = self._parse_iso(end) if end else None
        for username in usernames:
            table = "Msg_" + _md5_username(username)
            for db in self.message_dbs:
                with self._connect(db) as con:
                    if not self._table_exists(con, table):
                        continue
                    cols = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
                    aliases = self._aliases(cols)
                    select = [f'"{aliases["local_id"]}" AS local_id', f'"{aliases["server_id"]}" AS server_id', f'"{aliases["local_type"]}" AS local_type', f'"{aliases["create_time"]}" AS create_time']
                    for key in ("sender_id", "content", "compressed_content", "compression_flag"):
                        column = aliases.get(key)
                        select.append(f'"{column}" AS "{key}"' if column else f'NULL AS "{key}"')
                    clauses: list[str] = []
                    params: list[Any] = []
                    if start_ts is not None:
                        clauses.append(f'"{aliases["create_time"]}" >= ?'); params.append(start_ts)
                    if end_ts is not None:
                        clauses.append(f'"{aliases["create_time"]}" < ?'); params.append(end_ts)
                    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
                    sql = f'SELECT {", ".join(select)} FROM "{table}"{where} ORDER BY "{aliases["create_time"]}" ASC, "{aliases["local_id"]}" ASC'
                    for row in con.execute(sql, params):
                        yield self._normalize_row(row, username, db.name, table, self._name2id_by_db.get(db.name, {}))

    @staticmethod
    def _parse_iso(value: str) -> int:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError as exc:
            raise ValueError(f"无效 ISO 8601 时间: {value}") from exc

    @staticmethod
    def _aliases(columns: set[str]) -> dict[str, str]:
        aliases = {key: next((name for name in names if name in columns), "") for key, names in _OPTIONAL_COLUMN_ALIASES.items()}
        aliases.update({key: next((name for name in names if name in columns), "") for key, names in _REQUIRED_COLUMN_ALIASES.items()})
        if not aliases["local_id"]:
            aliases["local_id"] = "rowid"
        return aliases

    def _normalize_row(self, row: sqlite3.Row, username: str, db_name: str, table: str, sender_map: dict[int, str]) -> dict:
        local_id = str(row["local_id"])
        server_id = str(row["server_id"] or "")
        local_type = int(row["local_type"] or 0)
        base_type, sub_type = _type_parts(local_type)
        raw_value = row["content"] if row["content"] not in (None, "", b"") else row["compressed_content"]
        text, decode_error = _decode_bytes(raw_value, row["compression_flag"])
        sender_username = sender_map.get(int(row["sender_id"])) if row["sender_id"] is not None and str(row["sender_id"]).lstrip("-").isdigit() else None
        author_id = self.actor_id(sender_username) if sender_username else None
        conversation_id = self.conversation_id(username)
        kind, title, links, attachments, relations = self._kind_content(base_type, sub_type, text, username)
        structured_root = _parse_xml(text)
        if structured_root is not None and kind == "link":
            text = _xml_text(structured_root, ".//des", ".//description")
        elif kind == "quote":
            text = title
        elif structured_root is not None and kind in {"contact_card", "location"}:
            text = title
        elif kind == "unsupported" or (base_type in _MEDIA_TYPES and structured_root is not None) or kind == "file":
            text = ""
        if base_type in _MEDIA_TYPES and kind == "text":
            kind = _MEDIA_TYPES[base_type]
        quality_status = "complete" if decode_error is None else "decode_error"
        if kind in {"image", "voice", "video", "sticker", "file", "contact_card", "location", "call", "system"} and not text:
            quality_status = "metadata_only"
        if kind == "unsupported":
            quality_status = "unsupported"
        if decode_error:
            quality_status = "decode_error"
            text = None
        if kind == "voice" and not text:
            text = None
        created_at = _utc_timestamp(row["create_time"])
        # Server IDs deduplicate observations across shards in a conversation.
        # Local-only IDs also include the shard since local IDs can be reused.
        item_key = username + "\0" + (server_id or (db_name + "\0" + local_id))
        item_id = _opaque(self.source_id, "item", item_key)
        resource_attachments = self._resource_metadata(local_id, server_id, username) if kind == "file" else []
        if resource_attachments:
            for resource in resource_attachments:
                if attachments and attachments[0].get("name") is None:
                    attachments[0].update({key: value for key, value in resource.items() if value is not None})
                else:
                    attachments.append(resource)
        provenance = {
            "database": db_name,
            "table": table,
            "local_id": local_id,
            "server_id": server_id,
            "local_type": local_type,
            "raw_content_sha256": _sha(raw_value if isinstance(raw_value, bytes) else str(raw_value or "")),
        }
        normalized = {
            "schema_version": SCHEMA_VERSION,
            "resource_type": "message",
            "id": item_id,
            "source_id": self.source_id,
            "conversation_id": conversation_id,
            "author_id": author_id,
            "direction": self._direction(sender_username, username),
            "kind": kind,
            "created_at": created_at,
            "text": text.strip() if isinstance(text, str) else text,
            "title": title or None,
            "links": links,
            "attachments": attachments,
            "relations": relations,
            "quality": {"status": quality_status, "reason": decode_error},
            "provenance": provenance,
        }
        normalized["revision"] = _sha(json.dumps({key: normalized[key] for key in ("conversation_id", "author_id", "direction", "kind", "created_at", "text", "title", "links", "attachments", "relations", "quality")}, ensure_ascii=False, sort_keys=True))
        return normalized

    def _direction(self, sender_username: str | None, conversation_username: str) -> str:
        if not self.account_username or not sender_username:
            return "unknown"
        return "outgoing" if sender_username == self.account_username else "incoming"

    def _kind_content(self, base_type: int, sub_type: int, text: str, username: str) -> tuple[str, str, list[str], list[dict], list[dict]]:
        root = _parse_xml(text)
        title = ""
        links = _extract_urls(" ".join(root.itertext()) if root is not None else text)
        attachments: list[dict] = []
        relations: list[dict] = []
        try:
            app_type = int(_xml_text(root, ".//appmsg/type", ".//type") or 0) if root is not None else 0
        except ValueError:
            app_type = 0
        if base_type == 57 or (base_type == 49 and (sub_type == 57 or app_type == 57)):
            if root is not None:
                target = _xml_text(root, ".//refermsg/svrid")
                if target:
                    relations.append({"kind": "quotes", "target_server_id": target, "target_id": _opaque(self.source_id, "item", username + "\0" + target)})
            return "quote", _xml_text(root, ".//title") if root is not None else "", links, attachments, relations
        if base_type == 49:
            if root is not None:
                title = _xml_text(root, ".//title", ".//filename")
                url = _xml_text(root, ".//url")
                if url and url not in links: links.append(url)
                if app_type == 6 or sub_type == 6:
                    size = _xml_text(root, ".//appattach/totallen")
                    attachments.append({"kind": "file", "name": title or None, "size_bytes": int(size) if size.isdigit() else None, "availability": "metadata_only"})
                    return "file", title, links, attachments, relations
                if app_type == 5 or sub_type == 5:
                    return "link", title, links, attachments, relations
            return "unsupported", title, links, attachments, relations
        if base_type in _MEDIA_TYPES:
            return _MEDIA_TYPES[base_type], "", links, attachments, relations
        if base_type == 42:
            return "contact_card", _xml_text(root, ".//nickname", ".//title") if root is not None else "", links, attachments, relations
        if base_type == 48:
            return "location", _xml_text(root, ".//label", ".//title") if root is not None else "", links, attachments, relations
        if base_type == 50:
            return "call", "", links, attachments, relations
        if base_type == 10000:
            return "system", "", links, attachments, relations
        if base_type not in (1, 0):
            return "unsupported", "", links, attachments, relations
        return "text", "", links, attachments, relations

    def _resource_metadata(self, local_id: str, server_id: str, username: str) -> list[dict]:
        # Resource tables vary between builds. This intentionally only reads
        # filenames/sizes already indexed by WeChat and never opens media.
        if not self.resource_db.exists():
            return []
        try:
            with self._connect(self.resource_db) as con:
                tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"MessageResourceInfo", "MessageResourceDetail"} <= tables:
                    return []
                info_cols = {row[1] for row in con.execute("PRAGMA table_info(MessageResourceInfo)")}
                detail_cols = {row[1] for row in con.execute("PRAGMA table_info(MessageResourceDetail)")}
                if not {"message_local_id", "message_svr_id", "message_id"} <= info_cols:
                    return []
                if not {"message_id", "size", "packed_info"} <= detail_cols:
                    return []
                # Server IDs are the preferred index; local IDs require a
                # conversation match because every chat can reuse them.
                if server_id:
                    infos = con.execute("SELECT message_id FROM MessageResourceInfo WHERE message_svr_id=?", (server_id,)).fetchall()
                elif "chat_id" in info_cols and "ChatName2Id" in tables:
                    chat_cols = {row[1] for row in con.execute("PRAGMA table_info(ChatName2Id)")}
                    if "user_name" not in chat_cols:
                        return []
                    chat = con.execute("SELECT rowid FROM ChatName2Id WHERE user_name=?", (username,)).fetchone()
                    if chat is None:
                        return []
                    infos = con.execute("SELECT message_id FROM MessageResourceInfo WHERE chat_id=? AND message_local_id=?", (chat[0], local_id)).fetchall()
                else:
                    return []
                result = []
                for info in infos:
                    for row in con.execute("SELECT size, packed_info FROM MessageResourceDetail WHERE message_id=?", (info[0],)):
                        packed = row[1]
                        decoded = packed.decode("utf-8", "ignore") if isinstance(packed, bytes) else str(packed or "")
                        names = re.findall(r"(?:[A-Za-z0-9_ .\-()\u4e00-\u9fff]+\.(?:zip|pdf|docx?|xlsx?|pptx?|txt|jpg|png|mp3|mp4|m4a))", decoded, flags=re.I)
                        result.append({"kind": "file", "name": names[-1] if names else None, "size_bytes": int(row[0] or 0) or None, "availability": "metadata_only"})
                return result
        except (sqlite3.Error, ValueError, OSError):
            return []

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    @staticmethod
    def _write_json(path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict]) -> None:
        path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
        os.chmod(path, 0o600)
