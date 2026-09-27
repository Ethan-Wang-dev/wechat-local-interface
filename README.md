# WeChat Local Interface

WeChat Local Interface 是一个面向 macOS 微信明文数据库的本地、只读查询接口。它把用户已经解密的微信 SQLite 快照转换为稳定的结构化记录，供 Mousia、知识管理工具或其他本地程序检索和导出。

这个项目只做数据读取和标准化：不负责解密，不连接微信进程，不调用网络或模型，也不把数据写入知识库。

## 获取已解密快照

本项目需要一个已经解密的微信数据库快照。可以使用外部的 [`yichen-wechat-local-vault` Skill](https://github.com/mcncarl/yichen-skills/tree/main/yichen-wechat-local-vault) 完成 Mac 微信数据库的密钥提取、全量解密和增量刷新，再把生成的稳定快照目录传给本项目的 `--snapshot` 参数。

该 Skill 与本项目是两个独立项目：本项目不复制、调用或重新实现它的解密代码。使用时请同时遵守该 Skill 自身的许可证、使用说明以及适用的法律和平台规则。

## 能读取什么

| 数据 | 数据库 | 能力 |
|---|---|---|
| 联系人 | `contact/contact.db` | 联系人、群聊、公众号、好友状态识别和稳定 `actor_id` |
| 群聊关系 | `contact/chat_room`、`contact/chatroom_member` | 群成员、群主、好友/陌生人分类、共同群和关系边 |
| 聊天记录 | `message/message_*.db`、`message/biz_message_*.db` | 文本、图片、语音、视频、链接、文件、引用、系统消息 |
| 消息资源索引 | `message/message_resource.db` | 已有索引中的文件名和大小，不读取文件本体 |
| 收藏夹 | `favorite/favorite.db`（可选） | 文本、图片、文章、名片、视频号，支持搜索和导出 |
| 朋友圈 | `sns/sns.db`（可选） | 正文、标题、链接和媒体元数据，支持搜索和导出 |

收藏夹和朋友圈数据库不存在时，联系人和消息接口仍可用；调用对应资源接口时会返回明确的缺失数据库错误。

## 设计边界

- 只接受已经解密的 Mac 微信离线快照，不包含任何解密实现。
- 源数据库以 SQLite `mode=ro&immutable=1` 打开，并拒绝符号链接和活动 `-wal`、`-shm`、`-journal` 文件。
- 不访问消息中的 URL，不下载图片、语音、视频或文件，不把媒体索引当成媒体正文。
- 不调用模型、网络服务、外部 Skill 或知识库。
- 导出目录默认使用 `0700`，导出文件使用 `0600`。
- 记录使用 `mousia.wechat.v0` 数据契约，方便直接接入 Mousia。

## 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[zstandard]'
```

`zstandard` 是可选依赖。快照包含 zstd 压缩消息时需要安装；普通文本快照不需要。

## CLI 快速开始

所有命令都需要快照目录和逻辑来源 ID：

```bash
wechat-local-interface \
  --snapshot ./wechat-snapshot \
  --source-id my-wechat \
  status
```

常用命令：

```bash
# 联系人、会话和公众号
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat contacts --query "张三"
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat conversations --kind group
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat official --query "公众号"

# 群成员和对象关系（先从 conversations 取得 conversation_id）
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat members CONVERSATION_ID --friend
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat contact-groups ACTOR_ID
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat common-groups ACTOR_ID_1 ACTOR_ID_2
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat relations --type owns

# 消息检索
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat search "项目" --kind link
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat search "项目" --official-only

# 收藏夹和朋友圈
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat favorites --query "文章"
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat moments --author "张三" --has-link

# 跨资源搜索
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat search "关键词" --scope all

# 消息导出
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat \
  export CONVERSATION_ID --output ./wechat-export

# 收藏夹或朋友圈查询结果直接导出
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat \
  favorites --kind article --output ./favorite-export
```

完整 CLI 参数和 Python 接口规范见 [`docs/interface.md`](docs/interface.md)。

## 语言无关协议

如果 Mousia 或其他程序不是 Python，使用统一的 JSON/NDJSON 协议，不需要调用 Python 类：

```bash
wechat-local-interface \
  --snapshot ./wechat-snapshot \
  --source-id my-wechat \
  rpc < requests.ndjson > responses.ndjson
```

每行请求使用 `protocol_version`、`request_id`、`operation`、`params` 四个字段；每行响应使用 `ok`、`data`/`error` 和 `meta`。完整的信封、错误码、操作名、记录字段和机器可读 JSON Schema 见 [`docs/protocol.md`](docs/protocol.md) 以及 [`schemas/`](schemas)。

## Python API 快速开始

```python
from wechat_local_interface import WeChatSource

source = WeChatSource(
    snapshot="/path/to/decrypted/current",
    source_id="my-wechat",
    account_username="my-account",  # 可选，用于判断收发方向
)

accounts = source.list_official_accounts()
groups = source.list_conversations(kinds=["group"])
group_members = source.list_group_members(groups[0]["id"], is_friend=True) if groups else None
contact_groups = source.list_contact_groups("actor_<hash>")
favorites = source.search_favorites("文章", kinds=["article"])
moments = source.list_moments(author_usernames=["张三"], has_links=True)
results = source.search_all(
    "项目",
    scopes=["messages", "favorites", "moments"],
)
```

## 输出特点

每条记录都有来源隔离的稳定 ID、UTC 时间、资源类型、质量标记和数据库定位信息。联系人、群聊、群成员和群主使用同一套 `actor_id`/`conversation_id`，可以从联系人反查所在群，也可以导出 `member_of`、`owns` 关系边。消息、收藏夹和朋友圈可以分别分页，也可以通过 `search_all()` 获取统一搜索结果。导出包包含 JSONL 文件和 `manifest.json`，可以在不依赖微信数据库的情况下交给下游插件处理。

群成员的 `friend_status` 来自微信 `contact.local_type`：`friend` 表示通讯录好友，`non_friend` 表示群内陌生人，`unknown` 表示快照缺少可判断字段；`self` 表示当前账号。`in_contact_database` 只表示数据库存在对应联系人行，不能单独当作好友结论。缺少 `chat_room`/`chatroom_member` 时会降级为消息中观察到的发言人，并标记 `complete: false`。

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 -m compileall -q src
```

## License

MIT License。项目不包含微信解密代码，也不包含任何真实微信数据库或用户数据。
