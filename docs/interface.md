# WeChat Local Interface 接口规范

版本：`v0`
数据契约：`wechat.local.v0`
Python 包：`wechat_local_interface`
CLI：`wechat-local-interface`

本文档描述当前代码实际提供的 Python API、CLI、返回结构、过滤规则、分页游标、导出包和错误行为。跨语言集成使用 [`docs/protocol.md`](protocol.md) 中的 JSON/NDJSON 协议和 [`schemas/`](../schemas)；不需要绑定 Python。它不描述解密过程；解密由用户选择的外部工具完成。

## 0. 解密前置工具

本接口不提取密钥，也不解密微信数据库。需要准备明文快照时，可以使用外部的 [`yichen-wechat-local-vault` Skill](https://github.com/mcncarl/yichen-skills/tree/main/yichen-wechat-local-vault)，该 Skill 面向 Mac 微信本地数据库提供密钥提取、全量解密和增量刷新能力。

两个项目保持独立：本仓库只读取已经解密的快照，不复制、调用或重新实现该 Skill 的解密代码。使用该 Skill 前应阅读其仓库说明，遵守它的许可证、使用限制以及适用的法律和平台规则。

## 1. 工作流和快照要求

标准使用流程如下：

1. 使用外部工具获得微信明文数据库。
2. 将数据库整理成稳定的离线快照。
3. 创建 `WeChatSource`。
4. 先调用 `status()`，确认可选数据库是否存在。
5. 用联系人、会话或公众号接口确定查询范围。
6. 用读取/搜索接口获取结构化记录，或用导出接口生成 JSONL 包。
7. 把 JSONL 或内存中的记录交给下游插件处理。

### 1.1 快照目录

```text
snapshot/
├── contact/
│   └── contact.db                    # 必需
├── message/
│   ├── message_0.db                  # 至少一个 message_*.db 或 biz_message_*.db
│   ├── biz_message_0.db              # 可选的公众号消息分片
│   └── message_resource.db           # 可选，文件元数据索引
├── favorite/
│   └── favorite.db                   # 可选，收藏夹
└── sns/
    └── sns.db                        # 可选，朋友圈
```

连接器只识别文件名符合以下规则的消息分片：

- `message_<数字>.db`
- `biz_message_<数字>.db`

`contact/contact.db` 和至少一个消息分片缺失时，构造连接器会失败。`favorite.db`、`sns.db`、`message_resource.db` 可以缺失；对应功能会在调用时报告缺失。

### 1.2 稳定快照

快照必须是不会继续被微信或解密程序写入的副本。连接器会拒绝：

- 快照根目录、`contact`、`message`、`favorite`、`sns` 目录或其直接子文件中的符号链接；
- 数据库旁存在活动的 `-wal`、`-shm` 或 `-journal` 文件；
- 加密库、非普通文件或缺少必要表/字段的数据库。

连接器使用 SQLite URI `mode=ro&immutable=1`，并额外执行 `PRAGMA query_only=ON`。它不会修改源数据库。

## 2. 安装和导入

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[zstandard]'
```

导入方式：

```python
from wechat_local_interface import SCHEMA_VERSION, WeChatSource

source = WeChatSource(
    snapshot="/path/to/decrypted/current",
    source_id="my-wechat",
    account_username="my-account",
)
```

### 2.1 构造参数

| 参数 | 类型 | 必需 | 规范 |
|---|---|---:|---|
| `snapshot` | `str \| pathlib.Path` | 是 | 已解密快照目录；不能是符号链接。 |
| `source_id` | `str` | 是 | 只能使用字母、数字、`.`、`_`、`-`，长度 1–64；用于隔离不同账号。 |
| `account_username` | `str \| None` | 否 | 当前登录账号的微信 username。提供后，能识别发言人时才判断 `incoming/outgoing`。 |

`source_id` 会进入稳定 ID 和游标。对同一个快照使用不同 `source_id`，会得到不同的外部 ID。

## 3. 通用约定

### 3.1 时间

所有 Python 查询接口的 `start`、`end` 都要求带时区的 ISO 8601 字符串，例如：

```text
2026-09-01T00:00:00Z
2026-09-30T23:59:59-07:00
```

时间范围是左闭右开：`[start, end)`。`start` 省略表示没有下界，`end` 省略表示没有上界。`start >= end` 会报错。

数据库中的 Unix 秒或毫秒时间会转换成 UTC ISO 8601，例如 `2023-11-14T22:13:20Z`。缺失或无效时间为 `null`；时间过滤不会把没有时间的记录猜测到范围内。

CLI 也把 `--start` 和 `--end` 原样交给 Python 接口，因此同样建议使用带时区的 ISO 8601。

### 3.2 ID

所有 ID 都是字符串，不把 SQLite 大整数暴露为 JSON number：

| ID | 含义 |
|---|---|
| `actor_<hash>` | 联系人、发言人或资源作者 |
| `conversation_<hash>` | 会话 |
| `item_<hash>` | 聊天消息 |
| `favorite_<hash>` | 收藏记录 |
| `moment_<hash>` | 朋友圈记录 |
| `relationship_<hash>` | 联系人与群聊之间的关系边 |

哈希输入包含 `source_id`。同一个微信 username 在不同 `source_id` 下不会得到相同的外部 ID。底层定位放在 `provenance` 中；它适合本地复查，不应当被当作跨账号的公共 ID。

### 3.3 过滤组合

不同过滤参数之间使用 AND；同一个列表参数中的多个值使用 OR。例如：

```python
source.read_items(
    conversation_ids=[conversation_id],
    author_usernames=["张三", "李四"],
    kinds=["text", "link"],
    has_links=True,
)
```

含义是：发言人是张三或李四，消息类型是文本或链接，并且包含链接。

查询字符串使用不区分大小写的子字符串匹配，匹配范围如下：

- 消息：`text`、`title`、`links`、附件名称；
- 收藏夹：`text`、`title`、作者 username、`links`、附件名称；
- 朋友圈：`text`、`title`、作者 username、`links`、附件名称。

空的 `query` 等同于没有查询条件；搜索接口的主查询参数必须是非空字符串。

### 3.4 限制和排序

- 所有分页读取接口的 `limit` 必须为 `1–5000`。
- 默认 `limit` 为 `100`。
- 消息按创建时间升序返回；同一时间按本地 ID 和稳定 ID 排序。
- 收藏夹和朋友圈按创建/更新时间降序返回；同一时间按稳定 ID 排序。
- `list_contacts()` 和 `list_official_accounts()` 的默认 `limit` 为 `1000`，允许范围也是 `1–5000`。
- `list_conversations()` 不使用分页，返回当前发现的全部会话。
- 收藏夹和朋友圈的查询会先扫描当前筛选范围，再排序并切出 `limit` 条；`limit` 控制返回量，不等于数据库扫描量。数据量较大时应尽量提供 `start`/`end` 或更窄的作者/类型条件。

### 3.5 常见错误

所有接口使用 `ValueError` 报告输入或快照问题。常见错误包括：

- `snapshot 必须是非符号链接目录`
- `缺少 contact/contact.db`
- `缺少 message/message_<数字>.db 或 biz_message_<数字>.db`
- `缺少 favorite/favorite.db`
- `缺少 sns/sns.db`
- `snapshot 存在活动 sidecar，请提供稳定的离线快照`
- `未知 conversation_id`
- `未知 author_id`
- `发言人名称不唯一，请使用 username`
- `start 必须早于 end`
- `无效或过期 cursor`

CLI 会把这些错误写到 stderr 并返回退出码 `2`；成功返回 `0`。

## 4. Python API

以下接口都是 `WeChatSource` 的公开方法。返回值均为普通 `dict`、`list` 和 JSON 可序列化的标量。

### 4.1 `status()`

```python
status() -> dict
```

返回连接器和快照能力，不读取消息正文。典型结构：

```json
{
  "schema_version": "wechat.local.v0",
  "source_id": "my-wechat",
  "snapshot": "/path/to/decrypted/current",
  "source_kind": "wechat_mac_decrypted_vault",
  "message_databases": ["message_0.db", "biz_message_0.db"],
  "conversation_count": 42,
  "resource_index": true,
  "favorite_index": true,
  "moments_index": true,
  "group_membership_index": true,
  "friendship_classification": true,
  "decoders": {"utf8": true, "zstandard": true},
  "snapshot_version": "<32 位十六进制字符串>",
  "capabilities": ["contacts", "conversations", "messages", "search", "filters", "export", "official_accounts", "favorites", "moments", "group_members", "relationships"],
  "limitations": ["no_media_body_decode", "no_network", "no_knowledge_store_write"]
}
```

字段说明：

- `resource_index`、`favorite_index`、`moments_index` 表示对应数据库文件是否存在；
- `decoders.zstandard` 表示当前 Python 环境是否安装 zstandard，不代表快照一定含压缩消息；
- `snapshot_version` 会随源文件列表、大小、inode 和修改时间变化；
- `capabilities` 表示连接器实现的能力，具体可选数据库是否存在由三个 `*_index` 字段判断。

### 4.2 `list_contacts()`

```python
list_contacts(
    query: str | None = None,
    *,
    kinds: list[str] | None = None,
    is_subscription: bool | None = None,
    is_friend: bool | None = None,
    limit: int = 1000,
) -> list[dict]
```

用途：列出联系人数据库和消息 `Name2Id` 中出现的 actor。`query` 匹配 display name 或 username。

参数：

- `kinds`：`person`、`group`；
- `is_subscription=True`：只列公众号；`False`：排除公众号；
- `is_friend=True`：只列本地标记为通讯录好友的个人；`False`：只列明确标记为群内陌生人的个人；`None`：不按好友状态筛选；
- `limit`：返回上限。

返回 actor 记录：

```json
{
  "id": "actor_<hash>",
  "username": "wxid_example",
  "display_name": "张三",
  "kind": "person",
  "account_kind": "person",
  "is_subscription": false,
  "in_contact_database": true,
  "is_friend": true,
  "friend_status": "friend",
  "is_self": false,
  "friendship_evidence": {"database": "contact/contact.db", "local_type": 1, "delete_flag": 0}
}
```

`display_name` 优先使用备注名，其次是昵称、alias、username。公众号一般通过 contact 表标记或 `gh_` username 识别。

好友状态是本地快照中的分类：`friend` 对应 `contact.local_type` 为 `1` 或 `5`，`non_friend` 对应 `3` 或 `6`，`deleted` 表示 `delete_flag` 非零，`self` 表示构造连接器时传入的 `account_username`，`unknown` 表示字段缺失或版本无法判断。群聊和公众号的 `is_friend` 为 `null`。`in_contact_database` 只表示存在联系人行；需要判断好友时使用 `is_friend` 和 `friend_status`。

### 4.3 `list_conversations()`

```python
list_conversations(
    query: str | None = None,
    *,
    kinds: list[str] | None = None,
    has_messages: bool | None = None,
) -> list[dict]
```

用途：列出消息库中能发现的会话，并补充 contact.db 中的群聊关系；返回后续 `read_items()`、`export_bundle()` 使用的 `conversation_id`。只有联系人库里存在但消息库没有记录的群会标记 `has_messages=false`。

参数：

- `query`：匹配会话 display name 或 username；
- `kinds`：`person`、`group`；
- `has_messages`：只保留当前消息库中有记录或没有记录的会话。

返回：

```json
{
  "id": "conversation_<hash>",
  "display_name": "After Noise",
  "kind": "group",
  "account_kind": "group",
  "is_official": false,
  "message_table": "Msg_<md5(username)>",
  "has_messages": true,
  "member_count": 12,
  "owner_actor_id": "actor_<hash>",
  "membership_source": "contact_db"
}
```

名称相同的群不会自动合并，也不会自动选择其中一个；调用方应保存返回的 `id`。

`member_count`、`owner_actor_id` 和 `membership_source` 在 `contact.db` 含有群成员表时可用；缺少表或没有对应关系时为 `null`。`membership_source` 的值为 `contact_db`、`message_observed` 或 `null`。

### 4.4 `list_group_members()`：群成员和好友状态

```python
list_group_members(
    conversation_id: str,
    *,
    query: str | None = None,
    is_friend: bool | None = None,
    is_owner: bool | None = None,
    limit: int = 5000,
) -> dict
```

`conversation_id` 必须指向群聊。接口优先读取 `contact/chat_room` 和 `contact/chatroom_member`，将成员 id 解析为 actor；如果快照没有这些表，则退回到该群消息中实际观察到的发言人，并返回 `complete: false`。

返回结构中的 `members` 每行包含 `member_id`、`actor_id`、`username`、`display_name`、`in_contact_database`、`is_friend`、`friend_status`、`is_self`、`is_owner`、`friendship_evidence` 和 `provenance`。`member_count` 是未过滤的成员总数，`total_in_scope` 是过滤后的数量；`friend_counts` 提供未过滤成员的状态计数。

`query` 匹配成员显示名或 username；`is_friend`、`is_owner` 与其它过滤条件使用 AND。`membership_source` 为 `contact_db`、`message_observed` 或 `unavailable`，`complete` 表示是否读取了完整成员表。

### 4.5 `list_contact_groups()`：联系人所在群

```python
list_contact_groups(
    actor_id: str,
    *,
    query: str | None = None,
    limit: int = 1000,
) -> list[dict]
```

`actor_id` 可以是稳定 `actor_id`、微信 username 或唯一显示名。返回该 actor 出现在成员表（或消息观察结果）中的群会话记录，每项额外包含 `is_owner`、`membership_source` 和 `complete`。

### 4.6 `list_common_groups()`：共同群

```python
list_common_groups(
    actor_ids: list[str],
    *,
    query: str | None = None,
    limit: int = 1000,
) -> list[dict]
```

`actor_ids` 至少包含两个不同联系人。接口对每个 actor 的群集合取交集；当某个群只在消息观察结果中出现时，结果保留 `complete: false`。

### 4.7 `list_relationships()`：关系边

```python
list_relationships(
    *,
    subject_id: str | None = None,
    object_id: str | None = None,
    relationship_types: list[str] | None = None,
    limit: int = 5000,
) -> list[dict]
```

返回统一的对象图边：`member_of` 表示 actor 是群成员，`owns` 表示 actor 是群主。每条边包含 `subject_id`（actor）、`object_id`（conversation）、嵌入的 `subject`/`object`、`membership_source`、`complete` 和 `provenance`。群主同时也是成员时会得到一条 `member_of` 和一条 `owns` 边。

### 4.8 `list_official_accounts()`

```python
list_official_accounts(
    query: str | None = None,
    *,
    limit: int = 1000,
) -> list[dict]
```

用途：列出识别为公众号的联系人。公众号识别优先使用 contact 表的订阅标记，同时兼容常见的 `gh_` username。

返回：

```json
{
  "id": "actor_<hash>",
  "username": "gh_example",
  "display_name": "示例公众号",
  "kind": "official_account",
  "account_kind": "official_account",
  "is_subscription": true,
  "has_messages": true,
  "conversation_id": "conversation_<hash>"
}
```

`conversation_id` 可能为 `null`，表示联系人存在但当前消息分片中没有可发现的会话表。公众号聊天记录仍可通过普通会话接口读取；消息搜索还支持 `official_only`。

### 4.9 `read_items()`：读取消息

```python
read_items(
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
) -> dict
```

`conversation_ids` 不能为空，且每个 ID 必须由 `list_conversations()` 返回。返回结构：

```json
{
  "schema_version": "wechat.local.v0",
  "source_id": "my-wechat",
  "snapshot": "<查询范围的快照令牌>",
  "items": [],
  "next_cursor": "<十六进制游标或 null>",
  "total_in_scope": 123
}
```

消息专用过滤：

| 参数 | 可选值/行为 |
|---|---|
| `author_usernames` | 精确 username、备注名或昵称。名称不唯一或无法解析时失败。 |
| `author_ids` | 已知 `actor_id`；未知 ID 失败。 |
| `kinds` | `text`、`image`、`voice`、`video`、`sticker`、`contact_card`、`location`、`link`、`file`、`quote`、`call`、`system`、`unsupported`。 |
| `directions` | `incoming`、`outgoing`、`unknown`。没有 `account_username` 或没有发言人映射时为 `unknown`。 |
| `qualities` | `complete`、`metadata_only`、`unsupported`、`decode_error`。 |
| `query` | 匹配正文、标题、链接和附件名。 |
| `has_links` | `True` 只保留至少一个链接，`False` 只保留没有链接的消息。 |
| `has_attachments` | `True` 只保留存在附件元数据的消息，`False` 只保留没有附件的消息。 |
| `official_only` | `True` 只保留公众号会话，`False` 排除公众号会话，`None` 不限制。 |

消息字段见第 5 节。

### 4.10 `search_items()`：搜索消息

```python
search_items(
    query: str,
    conversation_ids: list[str] | None = None,
    **same_filters_as_read_items,
) -> dict
```

`query` 必须是非空字符串。传入 `conversation_ids` 时只搜索指定会话；省略时搜索所有已发现会话。其余参数与 `read_items()` 相同，包含时间、作者、类型、方向、质量、链接、附件、分页和 `official_only`。

示例：

```python
result = source.search_items(
    "项目",
    conversation_ids=[conversation_id],
    author_usernames=["张三"],
    kinds=["text", "link"],
    start="2026-09-01T00:00:00Z",
    end="2026-10-01T00:00:00Z",
)
```

### 4.11 `list_favorites()`：读取收藏夹

```python
list_favorites(
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
) -> dict
```

要求 `favorite/favorite.db` 存在且含 `fav_db_item` 表。没有该文件时抛出 `ValueError("缺少 favorite/favorite.db")`。

收藏类型：

- `text`
- `image`
- `article`
- `contact_card`
- `video`
- `unknown`

收藏的时间使用 `update_time`（兼容常见字段别名），输出为 `created_at`。`author_usernames` 匹配收藏记录中的来源 username；如果数据库记录没有来源，`author_id` 和 `author_username` 为 `null`。

返回结构与消息分页结构相同，但外层 `resource_type` 为 `favorites`，每条记录的 `resource_type` 为 `favorite`。

### 4.12 `search_favorites()`：搜索收藏夹

```python
search_favorites(query: str, **same_filters_as_list_favorites) -> dict
```

`query` 必须非空。搜索收藏记录的正文、标题、作者 username、链接和附件名称。

### 4.13 `list_moments()`：读取朋友圈

```python
list_moments(
    query: str | None = None,
    *,
    start: str | None = None,
    end: str | None = None,
    author_usernames: list[str] | None = None,
    author_ids: list[str] | None = None,
    kinds: list[str] | None = None,
    has_links: bool | None = None,
    has_attachments: bool | None = None,
    is_pinned: bool | None = None,
    is_private: bool | None = None,
    has_location: bool | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict
```

要求 `sns/sns.db` 存在且含 `SnsTimeLine` 表。没有该文件时抛出 `ValueError("缺少 sns/sns.db")`。

朋友圈类型：

- `text`
- `image`
- `video`
- `link`
- `mixed`
- `unknown`

类型由已有 XML 中的正文、链接、媒体节点和 content style 推断。它是标准化检索标签，不承诺覆盖微信所有内部类型。

返回结构与消息分页结构相同，但外层 `resource_type` 为 `moments`，每条记录的 `resource_type` 为 `moment`。

朋友圈记录还提供 `is_pinned`、`is_private`、`visibility`、`location`、
`post_type` 和 `metadata`。`metadata.fields` 保存 XML 中所有标量标签，
`metadata.attributes` 保存所有属性；媒体附件也有自己的 `metadata`。当本地
XML 没有明确的可见期限枚举时，`visibility.policy` 为 `unknown`，不会推断
“3 天、1 个月、半年或全部可见”。

收藏和消息记录同样提供 `metadata`，因此新版本微信新增的 XML 标签无需先
修改接口才能查询；跨版本稳定且常用的字段才会另外提升为顶层字段。完整的
数据库表、字段和语义审计见 [`docs/database-schema-inventory.md`](database-schema-inventory.md)。

### 4.14 `search_moments()`：搜索朋友圈

```python
search_moments(query: str, **same_filters_as_list_moments) -> dict
```

`query` 必须非空。搜索朋友圈正文、标题、作者 username、链接和附件名称。

### 4.15 `search_all()`：统一搜索

```python
search_all(
    query: str,
    *,
    scopes: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 100,
) -> dict
```

`scopes` 可选值：`messages`、`favorites`、`moments`。省略时搜索三类资源。返回：

```json
{
  "schema_version": "wechat.local.v0",
  "source_id": "my-wechat",
  "query": "项目",
  "items": [],
  "total_in_scope": 12,
  "unavailable": {}
}
```

- `items` 混合三类记录，按 `created_at` 降序排列；
- `limit` 是混合结果的总上限；
- 当前接口不提供跨资源 cursor；需要完整读取时分别调用三个资源接口；
- 可选数据库缺失、表不存在或不可读时，该资源会进入 `unavailable`，消息范围的错误仍会直接抛出；
- `search_all()` 只支持统一的关键词和时间范围，不接受消息专用的 `directions`、`qualities` 或会话 ID。

### 4.16 `export_bundle()`：导出消息

```python
export_bundle(
    output_root: str | pathlib.Path,
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
    previous: str | pathlib.Path | None = None,
    official_only: bool | None = None,
) -> dict
```

导出不受 `read_items` 的默认 `limit` 影响，会完整扫描指定范围。导出目录不能位于 snapshot 内，也不能与 snapshot 重叠。如果目标已存在，会自动使用 `-001`、`-002` 等后缀，不覆盖旧导出。

消息导出包：

```text
export/
├── manifest.json
├── actors.jsonl
├── conversations.jsonl
├── relationships.jsonl
├── items.jsonl
└── changes.jsonl
```

`relationships.jsonl` 包含选中群聊范围内的 `member_of` 和 `owns` 关系边；成员 actor 即使没有在时间筛选内发言，也会写入 `actors.jsonl`。消息 manifest 的 `counts.relationships` 是关系边数量。

`previous` 必须指向同一 source、同一 schema 和同一筛选范围的旧导出目录。变化规则：

- 旧包没有的 ID：`{"op":"added","item":...}`；
- 相同 ID 但 `revision` 变化：`{"op":"updated","item":...}`；
- 当前范围缺少旧记录：只计入 `missing_from_previous`，不产生删除操作；
- 同一消息在多个分片出现时，导出会按稳定 ID 去重，并在 `duplicate_observations` 记录重复数量。

### 4.17 `export_favorites()` 和 `export_moments()`

```python
export_favorites(
    output_root: str | pathlib.Path,
    *,
    previous: str | pathlib.Path | None = None,
    **same_filters_as_list_favorites,
) -> dict

export_moments(
    output_root: str | pathlib.Path,
    *,
    previous: str | pathlib.Path | None = None,
    **same_filters_as_list_moments,
) -> dict
```

两者都会完整扫描符合条件的资源，不受查询接口默认 `limit` 影响。输出包结构为：

```text
export/
├── manifest.json
├── actors.jsonl
├── items.jsonl
└── changes.jsonl
```

收藏夹导出的 manifest `resource_type` 为 `favorites`，朋友圈导出的 manifest `resource_type` 为 `moments`。`previous` 对比规则与消息导出相同。

## 5. 记录字段规范

消息、收藏夹和朋友圈使用同一 schema 版本，并通过 `resource_type` 区分资源：

- 消息记录：`resource_type = "message"`
- 收藏记录：`resource_type = "favorite"`
- 朋友圈记录：`resource_type = "moment"`

### 5.1 通用字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | string | 当前为 `wechat.local.v0`。 |
| `resource_type` | string | `message`、`favorite` 或 `moment`。 |
| `id` | string | 来源隔离的稳定 ID。 |
| `source_id` | string | 构造连接器时传入的来源 ID。 |
| `author_id` | string/null | 稳定 actor ID；数据库没有可确认作者时为 `null`。 |
| `author_username` | string/null | 数据库中记录的作者 username；可能为空。 |
| `created_at` | string/null | UTC ISO 8601 时间。收藏夹使用收藏更新时间。 |
| `kind` | string | 资源类型标签。 |
| `text` | string/null | 已成功抽取的正文；媒体没有正文时为 `null` 或空值。 |
| `title` | string/null | 标题、文件名或卡片标题。 |
| `links` | string[] | 数据库/XML 中已存在的 URL；不会访问。 |
| `attachments` | object[] | 媒体、文件或附件索引元数据；不会输出本体。 |
| `quality` | object | 解析质量和失败原因。 |
| `provenance` | object | 数据库、表、本地 ID、原始类型和内容哈希。 |
| `revision` | string | 规范化字段的 SHA-256，用于导出变更比较。 |

### 5.2 消息字段

消息额外包含：

| 字段 | 说明 |
|---|---|
| `conversation_id` | 所属会话 ID。 |
| `direction` | `incoming`、`outgoing` 或 `unknown`。 |
| `relations` | 引用消息关系；引用目标可以包含 `target_server_id` 和 source 隔离的 `target_id`。 |

消息 `kind` 的主要含义：

| kind | 说明 |
|---|---|
| `text` | 普通文本。 |
| `image`、`voice`、`video`、`sticker` | 媒体消息；不猜测媒体正文。 |
| `link` | 文章或链接卡片。 |
| `file` | 文件卡片；附件中可能包含名称和大小。 |
| `contact_card` | 名片。 |
| `location` | 位置。 |
| `quote` | 引用/回复。 |
| `call` | 通话。 |
| `system` | 系统消息。 |
| `unsupported` | 已识别消息表但当前标准化器不承诺解析。 |

### 5.3 收藏夹字段

收藏记录额外包含：

| 字段 | 说明 |
|---|---|
| `source_chat` | 数据库中记录的来源聊天名称；可能为 `null`。 |
| `provenance.favorite_type` | 微信原始收藏类型整数。 |

### 5.4 朋友圈字段

朋友圈记录额外包含：

| 字段 | 说明 |
|---|---|
| `post_type` | XML 中已有的 content style/sub style；没有时为空字符串。 |
| `attachments` | XML 中已有的 media 节点，只保留类型、名称、URL/缩略图等元数据。 |

### 5.5 `quality`

```json
{"status": "complete", "reason": null}
```

`status` 的含义：

- `complete`：正文/结构化字段成功解析；
- `metadata_only`：有媒体或附件元数据，但没有正文或不读取本体；
- `unsupported`：消息类型存在，但当前标准化器不承诺解析；
- `decode_error`：UTF-8 或 zstd 内容解码失败；`reason` 会说明原因，正文不会伪装成乱码。

### 5.6 `attachments`

附件是元数据，不是文件本体。常见字段：

```json
{
  "kind": "file",
  "name": "example.pdf",
  "size_bytes": 123456,
  "availability": "metadata_only"
}
```

`url`、`thumb` 等字段只表示数据库已有的字符串；连接器不会跟随这些地址。

### 5.7 `provenance`

定位字段示例：

```json
{
  "database": "message_0.db",
  "table": "Msg_<hash>",
  "local_id": "42",
  "server_id": "9007199254740993",
  "local_type": 1,
  "raw_content_sha256": "<64 位十六进制字符串>"
}
```

不同资源的 provenance 字段会包含各自的原始类型或数据库表字段。下游程序应把它当作定位信息，不要依赖某个未在本版本说明中的数据库内部字段。

## 6. 分页和快照一致性

`read_items()`、`list_favorites()`、`list_moments()` 使用同一种不透明 cursor：

1. 第一次请求不传 `cursor`；
2. 响应中有 `next_cursor` 时，把它原样传给下一页；
3. 不要解析、修改或跨不同筛选条件复用 cursor；
4. 如果数据源文件发生变化，或 start/end/过滤条件变化，会收到 `无效或过期 cursor`；
5. 需要重新创建 `WeChatSource` 并从第一页开始。

游标内部绑定：

- `source_id`；
- 源文件版本；
- 资源类型；
- 时间范围；
- 全部过滤条件；
- 当前页位置。

它不是跨时间持久化同步 token。需要跨次增量处理时，使用 `export_*` 的 `previous`。

## 7. 导出 manifest

典型 manifest：

```json
{
  "schema_version": "wechat.local.v0",
  "source_id": "my-wechat",
  "source_kind": "wechat_mac_decrypted_vault",
  "resource_type": "favorites",
  "privacy": {
    "visibility": "private",
    "storage": "local",
    "trust": "untrusted"
  },
  "snapshot": "<32 位快照令牌>",
  "filters": {},
  "counts": {
    "items": 10,
    "added": 10,
    "updated": 0,
    "missing_from_previous": 0
  }
}
```

`items.jsonl` 每行是一条完整记录；`actors.jsonl` 每行是一个导出范围内实际出现的作者；消息包还包含 `conversations.jsonl`。所有 JSONL 使用 UTF-8，每行一个 JSON 对象。

## 8. CLI 完整规范

全局参数：

```text
--snapshot DIR             已解密快照目录，必需
--source-id ID             稳定来源 ID，必需
--account-username USER    当前账号，用于判断消息方向，可选
```

### `status`

```bash
wechat-local-interface --snapshot DIR --source-id ID status
```

输出 `status()` 的 JSON 对象。

### `rpc`

```bash
wechat-local-interface --snapshot DIR --source-id ID rpc [--file REQUESTS.ndjson]
```

从 `--file` 或 stdin 逐行读取 `wechat.local.protocol.v1` 请求，并逐行输出协议响应。请求、错误码和所有操作定义见 [`docs/protocol.md`](protocol.md)。

### `contacts`

```bash
wechat-local-interface --snapshot DIR --source-id ID contacts \
  [--query TEXT] [--kind person|group] [--subscription|--no-subscription] \
  [--friend|--no-friend] [--limit N]
```

### `conversations`

```bash
wechat-local-interface --snapshot DIR --source-id ID conversations \
  [--query TEXT] [--kind person|group] [--has-messages|--no-has-messages]
```

### `members`

```bash
wechat-local-interface --snapshot DIR --source-id ID members CONVERSATION_ID \
  [--query TEXT] [--friend|--no-friend] [--owner|--no-owner] [--limit N]
```

查询群成员、群主和好友状态。`--friend` 只保留 `friend_status=friend`，`--no-friend` 只保留明确的非好友；未知状态不会被强行归类。

### `contact-groups`、`common-groups`

```bash
wechat-local-interface --snapshot DIR --source-id ID contact-groups ACTOR_ID \
  [--query TEXT] [--limit N]
wechat-local-interface --snapshot DIR --source-id ID common-groups ACTOR_ID_1 ACTOR_ID_2 ... \
  [--query TEXT] [--limit N]
```

`ACTOR_ID` 可以是 `actor_id`、微信 username 或唯一显示名。

### `relations`

```bash
wechat-local-interface --snapshot DIR --source-id ID relations \
  [--subject ACTOR_ID] [--object CONVERSATION_ID] \
  [--type member_of|owns] [--limit N]
```

输出统一关系边 JSON 数组；`--type` 可重复。

### `official`

```bash
wechat-local-interface --snapshot DIR --source-id ID official \
  [--query TEXT] [--limit N]
```

### `search`

```bash
wechat-local-interface --snapshot DIR --source-id ID search QUERY [CONVERSATION_ID ...] \
  [--scope messages|favorites|moments|all] \
  [--start ISO] [--end ISO] [--author VALUE] [--kind KIND] \
  [--direction incoming|outgoing|unknown] \
  [--quality complete|metadata_only|unsupported|decode_error] \
  [--has-link|--no-has-link] [--has-attachment|--no-has-attachment] \
  [--official-only|--no-official-only] [--limit N] [--cursor CURSOR]
```

- 默认 `--scope messages`；`CONVERSATION_ID` 只对消息搜索生效；
- `--scope favorites` 或 `moments` 使用对应资源的搜索接口；
- `--scope all` 调用 `search_all()`，只使用 query、start、end、limit；
- 重复选项如 `--author`、`--kind` 可传多次。

### `items`

```bash
wechat-local-interface --snapshot DIR --source-id ID items CONVERSATION_ID [CONVERSATION_ID ...] \
  [--start ISO] [--end ISO] [--author VALUE] [--kind KIND] \
  [--direction VALUE] [--quality VALUE] [--query TEXT] \
  [--has-link|--no-has-link] [--has-attachment|--no-has-attachment] \
  [--official-only|--no-official-only] [--limit N] [--cursor CURSOR]
```

对应 `read_items()`。

### `export`

```bash
wechat-local-interface --snapshot DIR --source-id ID export CONVERSATION_ID [CONVERSATION_ID ...] \
  --output DIR [--previous DIR] [message filters...]
```

对应 `export_bundle()`，会写入新的私有导出目录。

### `favorites` 和 `moments`

```bash
wechat-local-interface --snapshot DIR --source-id ID favorites|moments \
  [--query TEXT] [--start ISO] [--end ISO] [--author VALUE] [--kind KIND] \
  [--has-link|--no-has-link] [--has-attachment|--no-has-attachment] \
  [--limit N] [--cursor CURSOR] [--output DIR] [--previous DIR]
```

不传 `--output` 时返回分页查询 JSON；传 `--output` 时完整扫描当前过滤范围并导出 JSONL 包。`--limit` 和 `--cursor` 只影响屏幕查询，不限制导出完整性。

## 9. 当前限制

- 只支持已解密的 Mac 4.x 数据布局；Windows 快照不在本接口范围内。
- 不包含密钥提取、解密、增量解密调度或微信客户端自动化。
- 不读取媒体正文，也不保证本地媒体文件仍然存在。
- 复杂合并转发、小程序、卡片、朋友圈互动和部分微信内部类型只做保守的元数据标准化。
- 不做摘要、Embedding、知识库写入或 HTTP 服务。
- 数据库字段会随微信版本变化；未知字段或表结构会明确失败或降级为 `unsupported`，不会静默猜测。

## 10. 版本兼容

`SCHEMA_VERSION` 当前为 `wechat.local.v0`。下游程序应：

1. 检查 `schema_version`；
2. 对未知字段保持兼容；
3. 不依赖数据库内部表名作为公共 API；
4. 使用 `resource_type` 区分消息、收藏和朋友圈；
5. 使用 `revision` 和 manifest 计数做导出更新判断。

## 4.20 扩展数据索引

除消息、收藏和朋友圈正文外，连接器还提供以下只读索引：

- `contacts.labels`：联系人标签；
- `sessions.list`：会话未读数、隐藏状态、草稿、摘要和最后消息；
- `favorites.tags`：收藏标签；
- `moments.interactions`：朋友圈评论、回复和互动通知；
- `events.list`：红包、转账、好友申请、撤回消息等特殊事件。

联系人记录的 `metadata` 包含头像、拼音、验证状态、群内状态等原始列；群会话
包含 `group_metadata`；消息记录包含 `storage`，用于查询 SQLite 中的排序、投递
和来源状态。二进制扩展字段只返回大小和 SHA-256，不返回原始二进制内容。

- `assets.list`：文件、图片、视频和头像的本地索引；
- `emoticons.list`：本地表情包包信息。

媒体接口只读 SQLite 索引，不读取媒体正文，也不会访问网络。
