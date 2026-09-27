# WeChat Local Interface 数据接口 v0

## 用户旅程与验收

用户已经通过自己选择的外部工具获得本机微信明文数据库。用户显式提供目录和逻辑数据源 ID，检查状态、列出会话、选择会话与时间范围，导出可迁移的数据包。下游插件读取数据包，无需解密工具、Mousia Store 或模型服务。

验收标准：

- 源库只读，拒绝加密库、符号链接、活动 WAL/SHM/journal、未知消息字段。
- 结构化输出包含联系人、会话、消息、链接/引用/附件元数据、时间和定位信息；图片/语音/文件不猜测正文。
- 压缩消息正确解码；失败时保留定位、内容哈希和质量原因，不把乱码当正文。
- ID 以显式 source_id 隔离账号，稳定且不暴露内部微信账号；大整数转换为字符串。
- 分页游标绑定数据版本和筛选范围；源库变化后要求重新开始。
- 对同一范围再次导出，可与前次数据包比较并输出新增/更新，包括迟到消息；缺失不推断为删除。
- 导出为私有目录和 JSONL，不写入知识库，不调用外部 Skill、网络或模型，不发布 HTTP 接口。

## 边界

连接器支持已解密的 Mac 4.x contact/contact.db、message/message_<数字>.db、biz_message_<数字>.db，以及存在时的 favorite/favorite.db 和 sns/sns.db。读取必要的消息资源索引来补充附件文件名和大小。收藏夹和朋友圈只解析数据库中已有的文字、标题、链接和媒体元数据，不读取媒体正文，不访问链接，也不负责 Windows 格式、多账号自动发现、调度或解密。

数据库布局和字段名是输入格式事实；代码独立实现，不导入、复制或调用 yichen 的脚本。外部解密工具由用户独立管理。真实库及导出数据不进入项目/Git。

## 接口

`WeChatSource(snapshot, source_id, account_username=None)` 提供：

- `status()`：结构、数据版本、可用解码器和限制。
- `list_contacts(query=None, kinds=None, is_subscription=None, limit=1000)`：查询联系人和已在消息库中出现的发言人，返回稳定 actor_id；可按名称、person/group 和公众号标记过滤。
- `list_conversations(query=None, kinds=None, has_messages=None)`：返回精确 conversation_id；不自动挑选同名群。
- `read_items(conversation_ids, start=None, end=None, author_usernames=None, author_ids=None, kinds=None, directions=None, qualities=None, query=None, has_links=None, has_attachments=None, limit=100, cursor=None)`：结构化消息分页；ISO 8601 时间需包含时区，范围为 `[start, end)`。
- `search_items(query, conversation_ids=None, ...)`：在指定会话或全部已发现会话中搜索消息；其余过滤参数与 `read_items` 相同。
- `list_official_accounts(query=None, limit=1000)`：列出联系人库中的公众号，返回 actor_id、username、对应会话和是否有消息。
- `list_favorites(query=None, start=None, end=None, author_usernames=None, author_ids=None, kinds=None, has_links=None, has_attachments=None, limit=100, cursor=None)`：读取收藏夹，支持文本、图片、文章、名片和视频号等类型过滤。
- `search_favorites(query, ...)`：在收藏标题、正文、链接和附件名中搜索。
- `list_moments(query=None, start=None, end=None, author_usernames=None, author_ids=None, kinds=None, has_links=None, has_attachments=None, limit=100, cursor=None)`：读取朋友圈，支持作者、时间、类型、链接和媒体元数据过滤。
- `search_moments(query, ...)`：在朋友圈正文、标题、链接和附件名中搜索。
- `search_all(query, scopes=None, start=None, end=None, limit=100)`：统一搜索消息、收藏夹和朋友圈；缺少对应可选数据库时在 `unavailable` 中说明。
- `export_bundle(output_root, conversation_ids, start=None, end=None, author_usernames=None, author_ids=None, kinds=None, directions=None, qualities=None, query=None, has_links=None, has_attachments=None, previous=None)`：按同一组过滤条件导出完整 JSONL 数据包和对比变化。
- `export_favorites(output_root, ...)` / `export_moments(output_root, ...)`：把过滤后的收藏夹或朋友圈导出为私有 JSONL 包，支持 `previous` 对比新增和更新。

常用过滤条件：

- `author_usernames`：精确 username、联系人备注名或昵称；名称不唯一时拒绝猜测。
- `author_ids`：已返回的稳定 `actor_id`，适合缓存和跨次调用。
- `kinds`：`text`、`image`、`voice`、`video`、`sticker`、`link`、`file`、`quote`、`call`、`system`、`unsupported` 等。
- `directions`：`incoming`、`outgoing`、`unknown`；需要传入 `account_username` 才能区分收发。
- `qualities`：`complete`、`metadata_only`、`unsupported`、`decode_error`。
- `query`：不区分大小写匹配正文、标题、链接和附件名。
- `has_links` / `has_attachments`：只保留是否包含链接或附件元数据的消息。

多个条件同时提供时使用 AND；同一条件中的多个值使用 OR。游标和导出增量范围会绑定完整过滤条件。

CLI：`wechat-local-interface --snapshot DIR --source-id ID status|contacts|conversations|official|search|items|export|favorites|moments`。`official` 列出公众号；`favorites` 和 `moments` 支持 `--author`、`--kind`、`--query`、`--start`、`--end`、`--has-link/--no-has-link`、`--has-attachment/--no-has-attachment`，传 `--output` 时直接导出 JSONL 包。消息 `search`、`items` 和 `export` 还支持 `--direction`、`--quality` 和 `--official-only`；`search --scope favorites|moments|all` 可切换统一搜索范围。它是独立连接器入口，不负责通用插件宿主。

## 数据包与字段

`manifest.json`、`actors.jsonl`、`conversations.jsonl`、`items.jsonl`、`changes.jsonl`。`manifest.json` 同时记录规范化后的过滤条件，便于下游复现范围。

消息、收藏夹和朋友圈记录共用 `schema_version: "mousia.wechat.v0"`，并通过 `resource_type` 区分 `message`、`favorite`、`moment`：

| 字段 | 含义 |
|---|---|
| id / source_id / conversation_id | 稳定消息 ID、用户指定数据源、会话引用 |
| author_id / direction | 联系人引用；未确认本人时方向为 unknown |
| kind / created_at | 文本、图片、语音、视频、链接、文件、引用、系统或 unsupported；UTC 时间 |
| text / title / links | 已成功抽取的正文、标题、URL；只记录 URL，不访问 |
| attachments | 类型、文件名、大小等元数据；availability 为 metadata_only，不承诺文件本体存在 |
| relations | 引用消息关系，能匹配本次范围时包含 target_id，其他情况保持 unresolved |
| quality | complete / metadata_only / unsupported / decode_error 及原因 |
| provenance | 相对数据库、表、local_id/server_id 字符串、原始类型、原始内容 SHA-256 |
| revision | 规范化消息的内容哈希（不包含导出时间） |

收藏夹和朋友圈沿用 `id`、`source_id`、`author_id`、`author_username`、`created_at`、`kind`、`text`、`title`、`links`、`attachments`、`quality`、`provenance`、`revision` 字段；朋友圈额外保留 `post_type`，收藏夹额外保留 `source_chat`。`attachments` 只表示数据库索引里的媒体或附件元数据，`availability` 为 `metadata_only`，不代表本体会随数据包输出。

来源对象统一标记 private、local、untrusted。ID 去标识化不等于正文匿名化。保留确定性的原始定位，不复制原始 XML/二进制；复杂 XML（合并转发、卡片、小程序等）不宣称完整解析。

分页游标只用于同一快照；跨快照同步由前次包对比完成。`changes.jsonl` 为带完整 item 的 upsert，op 为 added 或 updated；未变化项不输出。缺失项只在 manifest 统计，不产生删除操作。首版每次重新扫描选定范围，并不声称源库层面的 CDC。
