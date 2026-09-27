# WeChat Local Interface

一个只读的本地微信数据接口，面向已经由用户自行解密的 Mac 微信数据库快照。

它把微信本地 SQLite 数据转换成稳定、可过滤、可分页、可导出的结构化记录，供 Mousia、知识管理工具或其他本地应用继续处理。

## 支持的数据

- 联系人和会话
- 公众号联系人与公众号会话
- 聊天消息，包括 `message_*.db` 和 `biz_message_*.db`
- 收藏夹：文本、图片、文章、名片、视频号等
- 朋友圈：正文、标题、链接和媒体索引元数据
- 消息资源索引中的文件名和大小元数据

## 设计边界

- 只读取已经解密的快照，不负责解密。
- 源数据库使用 SQLite 只读、immutable 模式打开，不修改微信数据。
- 拒绝符号链接和带活动 WAL/SHM/journal 的不稳定快照；请先生成稳定的离线快照。
- 不访问消息中的 URL，不下载媒体，不把媒体索引误认为媒体正文。
- 不调用模型、网络服务或知识库；导出结果默认是本地私有 JSONL 文件。
- 记录保留 `mousia.wechat.v0` 数据契约，便于直接接入 Mousia。

## 安装

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[zstandard]'
```

`zstandard` 只在快照含压缩消息时需要。

## CLI

```bash
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat status
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat contacts --query "张三"
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat official --query "公众号"
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat conversations
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat search "项目" --kind link
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat search "关键词" --scope all
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat favorites --query "文章"
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat moments --author "张三" --start 2026-01-01T00:00:00Z
wechat-local-interface --snapshot ./wechat-snapshot --source-id my-wechat export CONVERSATION_ID --output ./wechat-export
```

`favorites` 和 `moments` 也支持 `--author`、`--kind`、`--query`、`--start`、`--end`、`--has-link`、`--has-attachment`、游标和 `--output` 导出。消息支持 `--official-only`，可以只查公众号消息。

## Python API

```python
from wechat_local_interface import WeChatSource

source = WeChatSource(
    "/path/to/decrypted/current",
    source_id="my-wechat",
    account_username="my-account",
)

accounts = source.list_official_accounts()
favorites = source.search_favorites("文章", kinds=["article"])
moments = source.list_moments(author_usernames=["张三"], has_links=True)
results = source.search_all("项目", scopes=["messages", "favorites", "moments"])
```

完整字段、过滤、游标和 JSONL 导出说明见 [`docs/interface.md`](docs/interface.md)。

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 -m compileall -q src
```

## License

MIT License。这个项目只提供本地数据读取和标准化接口，不包含任何微信解密实现。
