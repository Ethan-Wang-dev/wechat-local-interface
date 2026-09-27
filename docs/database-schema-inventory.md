# 微信本地库字段审计

这个项目不是把某一版微信的几个字段写死后就结束。微信 4.x 的 SQLite
表、消息分片和 XML 载荷会随版本变化，因此仓库提供了只读审计工具：

```bash
python3 tools/inspect_snapshot_schema.py /path/to/decrypted/current \
  --sample-limit 500 \
  --output schema-inventory.json
```

工具输出数据库、表、列、行数，以及 XML 列中出现过的标签和属性名称；
不会输出正文、媒体内容或 XML 原文。遇到 SQLite 虚拟表、二进制列或无效
UTF-8 时会跳过值，只保留结构信息。

## 当前快照中确认的主要数据域

| 数据库 | 主要表/字段 | 接口资源 | 说明 |
| --- | --- | --- | --- |
| `contact/contact.db` | `contact`、`chat_room`、`chatroom_member`、`biz_info` | `contacts`、`conversations`、`group_members` | 联系人属性、群主、群成员、公众号标记和关系字段 |
| `message/message_*.db` | `Msg_*`、`Name2Id` | `messages` | 每个会话一张消息表；`local_type` 拆出基础类型和子类型 |
| `message/message_resource.db` | `MessageResourceInfo`、`MessageResourceDetail` | `messages.attachments` | 文件名、大小、资源索引等本地元数据 |
| `favorite/favorite.db` | `fav_db_item` | `favorites` | 收藏类型、来源、更新时间和 XML 内容 |
| `sns/sns.db` | `SnsTimeLine`、`SnsTopItem_1`、`SnsMessage_tmp3` | `moments` | 朋友圈正文、媒体、互动和置顶标记 |
| `session/session.db` | `SessionTable` | 会话目录辅助信息 | 未把它误当作消息正文 |
| `general/general.db` | `redEnvelopeTable`、`transferTable`、`FMessageTable`、`revokemessage` | `events.list` | 红包、转账、好友申请和撤回消息等特殊事件 |
| `hardlink/hardlink.db`、`head_image/head_image.db` | 文件、图片、视频和头像索引 | 元数据辅助 | 只读索引，不下载或解密媒体正文 |

## XML 字段策略

消息、收藏和朋友圈记录现在都包含 `metadata`：

```json
{
  "metadata": {
    "fields": {"contentDesc": "...", "isTop": "1"},
    "attributes": {"location": {"city": "..."}}
  }
}
```

字段值是字符串或字符串数组，重复标签不会丢失；属性按标签分组。常用
语义同时提升为稳定字段，便于跨语言调用：

- 朋友圈：`is_pinned`（来自 `isTop`）、`is_private`、`visibility`、
  `location`、`post_type`、`attachments[].metadata`；
- 收藏：`kind`、`source_chat`、`metadata`、`attachments[].metadata`；
- 消息：`kind`、`relations`、`metadata`，并保留文件资源索引中的附件字段。
- 联系人：`metadata` 保留头像、拼音、验证状态、群内状态等列；`contacts.labels`
  提供联系人标签；`sessions.list` 提供未读数、草稿和最后消息状态。
- 收藏标签：`favorites.tags`；朋友圈互动：`moments.interactions`；特殊事件：
  `events.list`。

`revokemessage` 表表示撤回事件，不等于原消息恢复。事件的 `content` 可能只是
撤回通知载荷；只有对应消息仍存在于消息分片时，才可以用服务器消息 ID 尝试关联
原文。当前快照该表为空。

`visibility.policy` 在本地数据库没有明确的“3 天/1 个月/半年/全部”
枚举时返回 `unknown`，不会根据时间范围猜测。`SnsTopItem_1` 是本地顶层
索引，不能单独证明某条记录对当前账号可见；只有 XML 的 `isTop=1` 才提升
为 `is_pinned=true`。

## 查询示例

```python
source.list_moments(is_pinned=True)
source.list_moments(is_private=False, has_location=True)
source.search_moments("关键词", author_usernames=["某联系人"])
```

新增字段会先出现在 `metadata`，再根据跨版本稳定性提升为顶层字段。这样
下游程序可以使用统一 JSON，不需要绑定某个 SQLite 版本或 Python 类型。
