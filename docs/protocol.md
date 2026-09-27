# 语言无关接口协议

版本：`wechat.local.protocol.v1`

Python `WeChatSource` 是一个本地实现；跨语言集成应使用本协议。协议只使用 UTF-8 JSON，不暴露 SQLite 类型、Python 类型或内部表名。TypeScript、Go、Rust、Swift、Java 和其他语言都可以按同一份请求/响应定义接入。

机器可读定义：

- [`schemas/wechat.local.protocol.v1.json`](../schemas/wechat.local.protocol.v1.json)：请求/响应信封、错误和通用字段；
- [`schemas/wechat.local.operations.v1.json`](../schemas/wechat.local.operations.v1.json)：全部操作的参数定义和结果类别。
- [`schemas/wechat.local.records.v0.json`](../schemas/wechat.local.records.v0.json)：actor、conversation、member、relationship、message、favorite、moment、分页和导出 manifest 的字段定义。

## 1. 传输方式

当前实现提供 stdin/stdout 的 NDJSON（newline-delimited JSON）通道：一行请求对应一行响应。它适合任何本地应用启动一个子进程，也适合未来封装为 HTTP、Unix domain socket 或 MCP adapter。

```bash
wechat-local-interface \
  --snapshot /path/to/decrypted/current \
  --source-id my-wechat \
  rpc < requests.ndjson > responses.ndjson
```

也可以从文件读取请求：

```bash
wechat-local-interface \
  --snapshot /path/to/decrypted/current \
  --source-id my-wechat \
  rpc --file requests.ndjson
```

每行必须是一个 JSON 对象。空行会跳过；单行 JSON 错误只影响该行，协议会返回错误对象并继续处理后续请求。

## 2. 请求信封

```json
{
  "protocol_version": "wechat.local.protocol.v1",
  "request_id": "req-0001",
  "source_id": "my-wechat",
  "operation": "groups.members",
  "params": {
    "conversation_id": "conversation_<hash>",
    "is_friend": true,
    "limit": 100
  }
}
```

| 字段 | 类型 | 规范 |
|---|---|---|
| `protocol_version` | string | 必须是 `wechat.local.protocol.v1`。 |
| `request_id` | string | 调用方生成的 1–128 字符 ID；响应原样返回，用于并发和日志关联。 |
| `source_id` | string | 可选；提供时必须与启动连接器的来源 ID 相同。 |
| `operation` | string | 使用操作目录中的稳定名称，例如 `messages.search`。 |
| `params` | object | 操作参数；未知字段会被拒绝。没有参数时使用 `{}`。 |

所有 ID 都是字符串。时间使用带时区的 RFC 3339 字符串，例如 `2026-09-01T00:00:00Z`。SQLite 整数、哈希和路径不会作为语言相关的整数或对象暴露。

## 3. 响应信封

成功响应：

```json
{
  "ok": true,
  "data": {
    "schema_version": "wechat.local.v0",
    "source_id": "my-wechat",
    "members": []
  },
  "meta": {
    "protocol_version": "wechat.local.protocol.v1",
    "schema_version": "wechat.local.v0",
    "request_id": "req-0001",
    "source_id": "my-wechat",
    "operation": "groups.members",
    "snapshot": "<snapshot-token>"
  }
}
```

失败响应：

```json
{
  "ok": false,
  "error": {
    "code": "invalid_argument",
    "message": "未知 conversation_id: conversation_x",
    "details": {}
  },
  "meta": {
    "protocol_version": "wechat.local.protocol.v1",
    "schema_version": "wechat.local.v0",
    "request_id": "req-0001",
    "source_id": "my-wechat",
    "operation": null,
    "snapshot": "<snapshot-token>"
  }
}
```

`ok=true` 时只读取 `data`；`ok=false` 时只读取 `error`。客户端不应依赖 Python 异常、stderr 文本或 SQLite 错误。

错误码：

| code | 含义 |
|---|---|
| `invalid_json` | 当前 NDJSON 行不是合法 JSON。 |
| `invalid_request` | 请求信封缺字段、类型错误或包含未知字段。 |
| `invalid_argument` | 操作参数不符合接口约束。 |
| `protocol_version_mismatch` | 协议版本不受支持。 |
| `source_mismatch` | 请求指定的 `source_id` 与当前进程不一致。 |
| `unsupported_operation` | 操作名称不在当前版本目录中。 |
| `source_changed` | 快照在读取期间发生变化，应重新创建稳定快照和连接器。 |
| `not_found` | 请求的资源不存在（保留给未来适配器；当前多数资源错误归为 `invalid_argument`）。 |
| `internal_error` | 未预期的内部错误；不会通过协议泄露 traceback、密钥或数据库路径。 |

## 4. 操作目录

| operation | 用途 | 结果 |
|---|---|---|
| `status` | 数据源能力和快照状态 | status object |
| `contacts.list` | 联系人、群聊、好友状态 | actor array |
| `conversations.list` | 会话和群成员元数据 | conversation array |
| `official.list` | 公众号 | actor array |
| `contacts.labels` | 联系人标签 | label array |
| `sessions.list` | 未读、草稿和最后消息 | session array |
| `groups.members` | 群成员、群主和好友分类 | group member envelope |
| `contacts.groups` | 联系人所在群 | conversation array |
| `groups.common` | 多个联系人共同群 | conversation array |
| `relationships.list` | `member_of`、`owns` 关系边 | relationship array |
| `messages.read` | 分页读取消息 | message page |
| `messages.search` | 搜索消息 | message page |
| `messages.export` | 导出完整消息包 | export manifest |
| `favorites.list/search/export` | 收藏夹读取、搜索、导出 | collection page/manifest |
| `favorites.tags` | 收藏标签 | tag array |
| `moments.list/search/export` | 朋友圈读取、搜索、导出 | collection page/manifest |
| `moments.interactions` | 朋友圈评论、回复和互动 | interaction array |
| `events.list` | 红包、转账、好友申请、撤回事件 | event array |
| `assets.list` | 文件、图片、视频、头像索引 | asset array |
| `emoticons.list` | 表情包包信息 | package array |
| `search.all` | 跨消息、收藏夹、朋友圈搜索 | unified search |

操作的完整参数在 `schemas/wechat.local.operations.v1.json` 中。参数命名使用 snake_case，避免绑定任何语言的命名风格；语言 SDK 可以在本地转换成 camelCase 或 idiomatic names，但在线路上必须使用协议字段名。

## 5. 下游对象模型

下游不要把消息、联系人、群聊分别当成互不相关的数组：

- actor 使用 `actor_id`；
- 会话使用 `conversation_id`；
- 消息使用 `author_id` 和 `conversation_id`；
- 收藏夹、朋友圈使用 `author_id`；
- 关系边使用 `subject_id` → `object_id`。

因此可以用同一套 join 逻辑表达：某联系人在哪些群、某群有哪些好友、某人在某群的消息、某联系人分享过哪些收藏或朋友圈内容。关系结果中的 `membership_source` 和 `complete` 必须保留，不能把消息观察到的发言人当成完整群成员。

## 6. 版本和兼容

- `protocol_version` 控制请求/响应信封和操作目录；发生不兼容变化时递增主版本。
- `schema_version`（当前 `wechat.local.v0`）控制业务记录字段；新增字段保持向后兼容。
- 客户端必须忽略未知响应字段，检查 `schema_version`，并把 `request_id` 与结果关联。
- 分页 `next_cursor` 是不透明字符串，只能原样传回同一个操作和筛选范围。
- 导出路径是本机进程路径，不应由远端客户端假定跨平台可访问；跨进程集成通常先导出到约定的本地目录，再读取返回的 manifest。
