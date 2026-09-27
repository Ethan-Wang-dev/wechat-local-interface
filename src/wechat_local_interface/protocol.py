"""Language-neutral JSON protocol for the read-only WeChat source.

The Python class remains useful for in-process callers, but this module is the
boundary that other languages should target.  Requests and responses are plain
JSON objects and can be transported over stdin/stdout, a subprocess pipe, or a
future local HTTP/MCP adapter without changing the data contract.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from .source import SCHEMA_VERSION, WeChatSource


PROTOCOL_VERSION = "wechat.local.protocol.v1"

_COMMON_MESSAGE_FILTERS = {
    "start", "end", "author_usernames", "author_ids", "kinds", "directions",
    "qualities", "query", "has_links", "has_attachments", "limit", "cursor",
    "official_only",
}
_COLLECTION_FILTERS = {
    "query", "start", "end", "author_usernames", "author_ids", "kinds",
    "has_links", "has_attachments", "is_pinned", "is_private", "has_location", "limit", "cursor",
}
_MESSAGE_EXPORT_FILTERS = _COMMON_MESSAGE_FILTERS - {"cursor"}
_COLLECTION_EXPORT_FILTERS = _COLLECTION_FILTERS - {"cursor"}

OPERATION_FIELDS: dict[str, frozenset[str]] = {
    "status": frozenset(),
    "contacts.list": frozenset({"query", "kinds", "is_subscription", "is_friend", "limit"}),
    "conversations.list": frozenset({"query", "kinds", "has_messages"}),
    "official.list": frozenset({"query", "limit"}),
    "groups.members": frozenset({"conversation_id", "query", "is_friend", "is_owner", "limit"}),
    "contacts.groups": frozenset({"actor_id", "query", "limit"}),
    "groups.common": frozenset({"actor_ids", "query", "limit"}),
    "relationships.list": frozenset({"subject_id", "object_id", "relationship_types", "limit"}),
    "messages.read": frozenset(_COMMON_MESSAGE_FILTERS | {"conversation_ids"}),
    "messages.search": frozenset(_COMMON_MESSAGE_FILTERS | {"conversation_ids"}),
    "messages.export": frozenset(_MESSAGE_EXPORT_FILTERS | {"conversation_ids", "output", "previous"}),
    "favorites.list": frozenset(_COLLECTION_FILTERS),
    "favorites.search": frozenset(_COLLECTION_FILTERS),
    "favorites.export": frozenset(_COLLECTION_EXPORT_FILTERS | {"output", "previous"}),
    "moments.list": frozenset(_COLLECTION_FILTERS),
    "moments.search": frozenset(_COLLECTION_FILTERS),
    "moments.export": frozenset(_COLLECTION_EXPORT_FILTERS | {"output", "previous"}),
    "search.all": frozenset({"query", "scopes", "start", "end", "limit"}),
}

_REQUIRED_PARAMS: dict[str, frozenset[str]] = {
    "groups.members": frozenset({"conversation_id"}),
    "contacts.groups": frozenset({"actor_id"}),
    "groups.common": frozenset({"actor_ids"}),
    "messages.read": frozenset({"conversation_ids"}),
    "messages.search": frozenset({"query"}),
    "messages.export": frozenset({"conversation_ids", "output"}),
    "favorites.search": frozenset({"query"}),
    "favorites.export": frozenset({"output"}),
    "moments.search": frozenset({"query"}),
    "moments.export": frozenset({"output"}),
    "search.all": frozenset({"query"}),
}
_ARRAY_PARAMS = {
    "actor_ids", "conversation_ids", "author_usernames", "author_ids", "kinds",
    "directions", "qualities", "relationship_types", "scopes",
}
_BOOL_PARAMS = {
    "is_subscription", "is_friend", "is_owner", "has_messages", "has_links",
    "has_attachments", "official_only",
    "is_pinned", "is_private", "has_location",
}
_STRING_PARAMS = {
    "conversation_id", "actor_id", "object_id", "subject_id", "start",
    "end", "cursor", "output", "previous",
}


class ProtocolFault(ValueError):
    """A safe, serializable client-facing protocol error."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class WeChatProtocol:
    """Dispatch protocol requests to one already-open ``WeChatSource``."""

    def __init__(self, source: WeChatSource):
        self.source = source

    def handle(self, request: Any) -> dict[str, Any]:
        request_id = request.get("request_id") if isinstance(request, dict) else None
        try:
            operation, params = self._validate_request(request)
            data = self._dispatch(operation, params)
            return self._success(request_id, operation, data)
        except ProtocolFault as exc:
            return self._failure(request_id, exc.code, exc.message, exc.details)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            message = str(exc) or "请求参数无效"
            code = "source_changed" if "snapshot" in message and ("变化" in message or "重试" in message) else "invalid_argument"
            return self._failure(request_id, code, message)
        except Exception:
            # Do not expose database paths, SQL, or traceback details through a
            # cross-process boundary.  The local caller can inspect its logs.
            return self._failure(request_id, "internal_error", "接口内部错误")

    def handle_line(self, line: str) -> str:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            return json.dumps(self._failure(None, "invalid_json", f"JSON 解析失败: {exc.msg}"), ensure_ascii=False, separators=(",", ":"))
        return json.dumps(self.handle(request), ensure_ascii=False, separators=(",", ":"))

    def handle_lines(self, lines: Iterable[str]) -> Iterable[str]:
        for line in lines:
            if line.strip():
                yield self.handle_line(line)

    def _validate_request(self, request: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(request, dict):
            raise ProtocolFault("invalid_request", "请求必须是 JSON 对象")
        allowed = {"protocol_version", "request_id", "source_id", "operation", "params"}
        unknown = sorted(set(request) - allowed)
        if unknown:
            raise ProtocolFault("invalid_request", "请求包含未知字段", {"fields": unknown})
        if request.get("protocol_version") != PROTOCOL_VERSION:
            raise ProtocolFault("protocol_version_mismatch", "不支持的 protocol_version", {"expected": PROTOCOL_VERSION})
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128:
            raise ProtocolFault("invalid_request", "request_id 必须是 1 到 128 个字符的字符串")
        requested_source = request.get("source_id")
        if requested_source is not None and requested_source != self.source.source_id:
            raise ProtocolFault("source_mismatch", "source_id 与当前连接器不匹配", {"source_id": self.source.source_id})
        operation = request.get("operation")
        if operation not in OPERATION_FIELDS:
            raise ProtocolFault("unsupported_operation", "不支持的 operation", {"operation": operation})
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise ProtocolFault("invalid_request", "params 必须是 JSON 对象")
        unknown_params = sorted(set(params) - OPERATION_FIELDS[operation])
        if unknown_params:
            raise ProtocolFault("invalid_argument", "params 包含未知字段", {"fields": unknown_params})
        missing = sorted(field for field in _REQUIRED_PARAMS.get(operation, ()) if field not in params)
        if missing:
            raise ProtocolFault("invalid_argument", "缺少必需参数", {"fields": missing})
        self._validate_param_values(operation, params)
        return operation, dict(params)

    @staticmethod
    def _validate_param_values(operation: str, params: dict[str, Any]) -> None:
        for key, value in params.items():
            if key in _ARRAY_PARAMS:
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ProtocolFault("invalid_argument", f"{key} 必须是字符串数组")
            elif key in _BOOL_PARAMS:
                if value is not None and not isinstance(value, bool):
                    raise ProtocolFault("invalid_argument", f"{key} 必须是布尔值或 null")
            elif key in _STRING_PARAMS:
                if not isinstance(value, str) or not value.strip():
                    raise ProtocolFault("invalid_argument", f"{key} 必须是非空字符串")
            elif key == "query":
                if not isinstance(value, str):
                    raise ProtocolFault("invalid_argument", "query 必须是字符串")
            elif key == "limit":
                if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5000:
                    raise ProtocolFault("invalid_argument", "limit 必须是 1 到 5000 之间的整数")
        for field in ("conversation_ids", "actor_ids"):
            if field in params and not params[field]:
                raise ProtocolFault("invalid_argument", f"{field} 不能为空")
        if operation == "groups.common" and len(set(params.get("actor_ids", []))) < 2:
            raise ProtocolFault("invalid_argument", "actor_ids 至少需要两个不同联系人")
        if operation in {"messages.search", "favorites.search", "moments.search", "search.all"}:
            if not isinstance(params.get("query"), str) or not params["query"].strip():
                raise ProtocolFault("invalid_argument", "query 不能为空")
        scopes = params.get("scopes")
        if scopes is not None and any(scope not in {"messages", "favorites", "moments"} for scope in scopes):
            raise ProtocolFault("invalid_argument", "scopes 只能包含 messages、favorites、moments")

    def _dispatch(self, operation: str, params: dict[str, Any]) -> Any:
        if operation == "status":
            return self.source.status()
        if operation == "contacts.list":
            return self.source.list_contacts(**params)
        if operation == "conversations.list":
            return self.source.list_conversations(**params)
        if operation == "official.list":
            return self.source.list_official_accounts(**params)
        if operation == "groups.members":
            conversation_id = params.pop("conversation_id")
            return self.source.list_group_members(conversation_id, **params)
        if operation == "contacts.groups":
            actor_id = params.pop("actor_id")
            return self.source.list_contact_groups(actor_id, **params)
        if operation == "groups.common":
            actor_ids = params.pop("actor_ids")
            return self.source.list_common_groups(actor_ids, **params)
        if operation == "relationships.list":
            return self.source.list_relationships(**params)
        if operation == "messages.read":
            conversation_ids = params.pop("conversation_ids")
            return self.source.read_items(conversation_ids, **params)
        if operation == "messages.search":
            query = params.pop("query")
            conversation_ids = params.pop("conversation_ids", None)
            return self.source.search_items(query, conversation_ids=conversation_ids, **params)
        if operation == "messages.export":
            output = params.pop("output")
            conversation_ids = params.pop("conversation_ids")
            return self.source.export_bundle(output, conversation_ids, **params)
        if operation == "favorites.list":
            return self.source.list_favorites(**params)
        if operation == "favorites.search":
            query = params.pop("query")
            return self.source.search_favorites(query, **params)
        if operation == "favorites.export":
            output = params.pop("output")
            return self.source.export_favorites(output, **params)
        if operation == "moments.list":
            return self.source.list_moments(**params)
        if operation == "moments.search":
            query = params.pop("query")
            return self.source.search_moments(query, **params)
        if operation == "moments.export":
            output = params.pop("output")
            return self.source.export_moments(output, **params)
        if operation == "search.all":
            query = params.pop("query")
            return self.source.search_all(query, **params)
        raise ProtocolFault("unsupported_operation", "不支持的 operation", {"operation": operation})

    def _meta(self, request_id: str | None, operation: str | None) -> dict[str, Any]:
        try:
            snapshot = self.source.status()["snapshot_version"]
        except Exception:
            snapshot = None
        return {
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "source_id": self.source.source_id,
            "operation": operation,
            "snapshot": snapshot,
        }

    def _success(self, request_id: str, operation: str, data: Any) -> dict[str, Any]:
        return {"ok": True, "data": data, "meta": self._meta(request_id, operation)}

    def _failure(self, request_id: str | None, code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"code": code, "message": message, "details": details or {}},
            "meta": self._meta(request_id, None),
        }
