# Agent 集成指南

本文是给调用本接口的 Agent、插件和 SDK 作者看的规范。若本文与代码注释
冲突，以 `schemas/wechat.local.operations.v1.json` 和运行时返回的
`schema_version` 为准；SQLite 表名只用于审计，不能作为公共 API。

## 一、连接器生命周期

1. Agent 接收一个**已经解密且稳定**的 Mac 快照目录。
2. 用 `WeChatSource(snapshot, source_id, account_username?)` 创建一个只读连接器，
   或启动 `rpc` 子进程。
3. 先调用 `status`，根据 `*_index` 判断可选数据库是否存在。
4. 每次分页请求保存返回的 `snapshot` 和 `next_cursor`。cursor 只对同一个
   source、资源、过滤器和快照有效；任何数据库变化都必须重新创建连接器。
5. Agent 不应自行打开 SQLite，不应把 `id` 当作数据库整数，也不应把
   `metadata` 中的未知字段当成稳定字段。

连接器只读 SQLite，拒绝符号链接和活动 `-wal/-shm/-journal` sidecar；它不提取
密钥、不解密、不连接微信、不访问 URL、不写知识库，也不读取媒体正文。

## 一点五、Skill 读取前刷新

任何依赖微信本地数据的上层 Skill 都必须在上述连接器生命周期之前先执行一次
增量刷新：

```bash
python3 {{YICHEN_SKILL_DIR}}/scripts/decrypt_all_dbs.py --mode incremental
```

刷新完成后重新确认快照稳定，再创建 `WeChatSource` 或启动 RPC。刷新失败、快照
没有变化、或者存在活动 sidecar，都必须把状态传给用户；不能静默复用旧快照。
只处理已生成文件的渲染 Skill，以及用户明确提供的离线快照，可以跳过刷新，但要
记录 `refresh_before_read: false` 或跳过原因。增量刷新只更新本地解密数据，不保证
微信服务器同步完成，也不能恢复已删除或已撤回的原文。

## 二、统一对象规则

所有结果使用 UTF-8 JSON。字符串 ID 是 source-scoped opaque ID，例如
`actor_<hash>`、`conversation_<hash>`、`moment_<hash>`。同一 username 在不同
`source_id` 下的 ID 不相同。

### 记录质量

- `complete`：已取得可解析正文；
- `metadata_only`：只有 SQLite/XML/资源索引，正文或媒体本体未读取；
- `unsupported`：已发现记录，但当前标准化器不认识其类型；
- `decode_error`：压缩或编码解码失败。

### XML 元数据

消息、收藏和朋友圈都有 `metadata`：

```json
{"fields":{"isTop":"1"},"attributes":{"location":{"city":"上海"}}}
```

`fields` 保存 XML 标量叶节点，重复标签变成数组；`attributes` 按标签保存属性。
媒体附件有自己的 `metadata`。新版本微信新增字段首先从这里可见，再根据跨版本
稳定性提升为顶层字段。

### 不透明二进制

头像、扩展 buffer、资源 packed info 等二进制值不会直接放进 JSON，而会变成：

```json
{"encoding":"binary","size":1234,"sha256":"..."}
```

这能让 Agent 判断是否发生变化，同时避免把不可移植的二进制误当成文本。

## 三、操作完整清单

| 操作 | 关键参数 | 返回 | 语义 |
|---|---|---|---|
| `status` | 无 | status | 能力、索引存在性、快照版本 |
| `contacts.list` | `query`, `kinds`, `is_subscription`, `is_friend`, `limit` | actor[] | 联系人、群聊、好友关系和公众号资料 |
| `contacts.labels` | `query`, `limit` | label[] | 联系人标签表 |
| `conversations.list` | `query`, `kinds`, `has_messages` | conversation[] | 会话、群关系、会话状态、群详情 |
| `sessions.list` | `query`, `unread_only`, `limit` | session[] | 未读、隐藏、草稿、摘要、最后消息 |
| `official.list` | `query`, `limit` | actor[] | 公众号联系人及 business_info |
| `groups.members` | `conversation_id`, `query`, `is_friend`, `is_owner`, `limit` | envelope | 群成员和本地联系人关系 |
| `contacts.groups` | `actor_id`, `query`, `limit` | conversation[] | 某联系人所在群 |
| `groups.common` | `actor_ids`, `query`, `limit` | conversation[] | 多人共同群 |
| `relationships.list` | subject/object/type/limit | relationship[] | `member_of`、`owns` |
| `messages.read` | conversation/filter/page 参数 | message page | 消息分页 |
| `messages.search` | `query` 加消息过滤器 | message page | 消息全文/字段搜索 |
| `messages.export` | conversation/filter/output | manifest | 完整 JSONL 导出 |
| `favorites.list/search/export` | collection filters | page/manifest | 收藏正文、附件和 XML 元数据 |
| `favorites.tags` | `query`, `limit` | tag[] | 收藏标签 |
| `moments.list/search/export` | collection filters | page/manifest | 朋友圈正文、媒体、置顶和可见性标记 |
| `moments.interactions` | `feed_id`, `author_usernames`, `unread_only`, `limit` | interaction[] | 评论、回复、互动通知 |
| `events.list` | `kind`, `limit` | event[] | 红包、转账、好友申请、撤回事件 |
| `assets.list` | `kind`, `limit` | asset[] | 文件、图片、视频、头像索引 |
| `emoticons.list` | `limit` | package[] | 表情包包信息 |
| `search.all` | `query`, `scopes`, 时间、limit | unified page | 消息、收藏、朋友圈统一搜索 |

未知参数会被协议拒绝。Python API 也会对分页、时间和枚举值做校验。

## 四、重点对象的语义

### 联系人和公众号

`actor.metadata` 保留头像 URL/MD5、拼音、验证状态、群内状态、通知设置等
contact 列。`actor.business_info` 来自公众号业务资料；它为空不代表联系人
不是公众号，Agent 还应检查 `is_subscription` 和 username 前缀 `gh_`。

### 会话和群聊

`conversation.session` 来自 `SessionTable`，包含未读数、草稿、摘要和最后消息；
`conversation.group_metadata` 来自群详情表，包含公告、公告编辑者、发布时间和
群状态。群成员列表标明 `membership_source` 和 `complete`：`contact_db` 表示
缓存成员表，`message_observed` 只表示消息中观察到的发言人，不能当作完整成员名单。

### 消息

`message.storage` 保存 `sort_seq`、投递/下载状态、`server_seq`、来源和 packed
索引。它们用于同步和诊断，不应替代 `created_at`、`direction` 或 `kind`。
`metadata` 是 XML 载荷字段；`relations` 用于引用/回复关系。复杂转发、小程序和
部分卡片可能只有元数据。

### 朋友圈

`is_pinned` 只在 XML `isTop=1` 时为 true；`SnsTopItem_1` 只是本地索引，不单独
证明当前账号可见。`visibility.policy` 没有明确枚举时为 `unknown`，不能推断
“3 天、1 个月、半年或全部可见”。

### 特殊事件与撤回

`events.list(kind="revoked_message")` 返回的是**撤回事件**，不是恢复后的原消息。
事件可能包含服务器消息 ID、发送人、时间和撤回载荷；只有对应消息仍留在消息表时，
Agent 才能用 `server_id` 尝试关联原消息。当前快照没有撤回事件时返回空数组，不能
据此推断历史上从未撤回。

### 媒体和表情

`assets.list` 与 `emoticons.list` 只返回本地索引和元数据，不保证文件仍存在，
也不读取或上传图片、视频、语音和表情正文。

## 五、推荐调用流程

```text
status
  ├─ conversations.list / sessions.list
  ├─ contacts.list / contacts.labels
  ├─ messages.read 或 messages.search
  ├─ favorites.list / moments.list
  ├─ moments.interactions / events.list
  └─ assets.list（需要资源索引时）
```

做增量同步时，以导出 manifest 的 `revision`、`snapshot` 和计数为边界；不要用
数据库 rowid 或最后一条消息时间单独判断新增。做分析时，先根据 `quality.status`
区分完整正文和元数据，再决定是否交给模型。

## 六、错误处理

`invalid_argument` 表示调用参数问题；`source_changed` 表示快照发生变化；
`internal_error` 不提供数据库路径或 traceback。Agent 应在 `source_changed` 时
丢弃旧 cursor，重新建立稳定快照和连接器，而不是无限重试同一个 cursor。

机器可读参数以 [operations schema](../schemas/wechat.local.operations.v1.json)
为准；跨语言请求使用 [protocol guide](protocol.md)。
