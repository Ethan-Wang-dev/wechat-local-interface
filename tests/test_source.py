import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from hashlib import md5
from pathlib import Path

from wechat_local_interface.cli import main
from wechat_local_interface.source import WeChatSource


class WeChatSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "snapshot"
        (self.root / "contact").mkdir(parents=True)
        (self.root / "message").mkdir()
        self.username = "100@chatroom"
        self.table = "Msg_" + md5(self.username.encode()).hexdigest()
        with sqlite3.connect(self.root / "contact/contact.db") as con:
            con.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
            con.executemany("INSERT INTO contact VALUES(?,?,?)", [
                (self.username, "测试群", ""), ("person-a", "张三", "同事"),
                ("my-account", "我", ""), ("private-unrelated", "不相关联系人", ""),
            ])
        self.db = self.root / "message/message_0.db"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER)")
            con.executemany("INSERT INTO Name2Id VALUES(?,0)", [("person-a",), ("my-account",)])
            con.execute(f'CREATE TABLE "{self.table}"(local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER)')
        self.insert(1, "第一条 https://example.com/a", server_id=9007199254740993)
        self.insert(2, "第二条", timestamp=1700000000, sender=2)

    def insert(self, local_id, content, timestamp=1700000000, kind=1, sender=1, server_id=None, db=None, compression=0):
        with sqlite3.connect(db or self.db) as con:
            con.execute(f'INSERT INTO "{self.table}" VALUES(?,?,?,?,?,?,?)',
                        (local_id, server_id or 100 + local_id, kind, sender, timestamp, content, compression))

    def source(self, **kwargs):
        return WeChatSource(self.root, "test-account", **kwargs)

    def chat(self, source):
        return source.list_conversations()[0]["id"]

    def test_normalization_namespace_provenance_and_direction(self):
        source = self.source(account_username="my-account")
        rows = source.read_items([self.chat(source)])['items']
        self.assertEqual(2, len(rows))
        first = rows[0]
        self.assertEqual("9007199254740993", first['provenance']['server_id'])
        self.assertEqual("同事", source.actors[first['author_id']]['display_name'])
        self.assertEqual("incoming", first['direction'])
        self.assertEqual("outgoing", rows[1]['direction'])
        self.assertEqual(["https://example.com/a"], first['links'])
        self.assertNotIn("person-a", json.dumps(rows))
        other = WeChatSource(self.root, "another-account")
        self.assertNotEqual(first['id'], other.read_items([self.chat(other)])['items'][0]['id'])
        self.assertEqual(first['id'], self.source().read_items([self.chat(self.source())])['items'][0]['id'])

    def test_keyset_paging_and_scope_and_snapshot_boundaries(self):
        source = self.source()
        chat = self.chat(source)
        first = source.read_items([chat], limit=1)
        second = source.read_items([chat], limit=1, cursor=first['next_cursor'])
        self.assertNotEqual(first['items'][0]['id'], second['items'][0]['id'])
        self.assertIsNone(second['next_cursor'])
        with self.assertRaises(ValueError):
            source.read_items([chat], start="2023-11-01T00:00:00Z", cursor=first['next_cursor'])
        with self.assertRaises(ValueError):
            source.read_items([chat], cursor="garbage")
        self.insert(3, "新消息")
        with self.assertRaises(ValueError):
            source.read_items([chat], cursor=first['next_cursor'])

    def test_composable_author_kind_direction_and_content_filters(self):
        self.insert(3, "第三条 https://example.com/third", sender=1)
        self.insert(4, '<msg><appmsg><title>我的文件</title><type>6</type></appmsg></msg>', kind=(6 << 32) | 49, sender=2)
        source = self.source(account_username="my-account")
        chat = self.chat(source)
        rows = source.read_items(
            [chat],
            author_usernames=["同事"],
            kinds=["text"],
            directions=["incoming"],
            query="第一",
        )['items']
        self.assertEqual(["第一条 https://example.com/a"], [row['text'] for row in rows])
        links = source.read_items([chat], has_links=True)['items']
        self.assertEqual({"第一条 https://example.com/a", "第三条 https://example.com/third"}, {row['text'] for row in links})
        files = source.read_items([chat], kinds=["file"], has_attachments=True)['items']
        self.assertEqual(["我的文件"], [row['title'] for row in files])
        author_id = source.actor_id("person-a")
        self.assertEqual(2, source.read_items([chat], author_ids=[author_id])['total_in_scope'])
        with self.assertRaises(ValueError):
            source.read_items([chat], author_usernames=["不存在的人"])
        with self.assertRaises(ValueError):
            source.read_items([chat], kinds=["not-a-kind"])

    def test_contacts_conversations_and_global_search(self):
        source = self.source()
        contacts = source.list_contacts(query="同事", kinds=["person"])
        self.assertEqual(["同事"], [row['display_name'] for row in contacts])
        self.assertEqual(source.actor_id("person-a"), contacts[0]['id'])
        self.assertEqual(1, len(source.list_conversations(kinds=["group"])))
        self.assertEqual([], source.list_conversations(has_messages=False))
        found = source.search_items("example.com/a")
        self.assertEqual(["第一条 https://example.com/a"], [row['text'] for row in found['items']])

    def test_filter_scope_is_bound_to_cursor_and_export(self):
        source = self.source(account_username="my-account")
        chat = self.chat(source)
        first = source.read_items([chat], kinds=["text"], limit=1)
        with self.assertRaises(ValueError):
            source.read_items([chat], kinds=["link"], cursor=first['next_cursor'])
        bundle = source.export_bundle(
            self.base / 'filtered',
            [chat],
            author_usernames=["my-account"],
            directions=["outgoing"],
            query="第二",
        )
        rows = self.load(bundle, 'items.jsonl')
        self.assertEqual(["第二条"], [row['text'] for row in rows])
        self.assertEqual([source.actor_id("my-account")], bundle['manifest']['filters']['author_ids'])
        self.assertEqual(["outgoing"], bundle['manifest']['filters']['directions'])

    def test_shards_duplicates_and_missing_sender(self):
        extra = self.root / "message/message_1.db"
        with sqlite3.connect(extra) as con:
            con.execute(f'CREATE TABLE "{self.table}"(local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER)')
        self.insert(9, "第一条 https://example.com/a", db=extra, server_id=9007199254740993)
        self.insert(10, "无发送者的消息", db=extra, server_id=999)
        source = self.source()
        # Pages expose observations; complete export deduplicates stable message IDs.
        bundle = source.export_bundle(self.base / "output", [self.chat(source)])
        items = self.load(bundle, "items.jsonl")
        self.assertEqual(3, len(items))
        self.assertEqual(1, bundle['manifest']['counts']['duplicate_observations'])
        self.assertIsNone(next(x for x in items if x['text'] == "无发送者的消息")['author_id'])

    def test_xml_link_file_quote_and_no_fetched_content(self):
        self.insert(3, '<msg><appmsg><title>文章</title><des>简介</des><type>5</type><url>https://example.com/article</url></appmsg></msg>', kind=(5 << 32) | 49)
        self.insert(4, '<msg><appmsg><title>工具.zip</title><type>6</type><appattach><totallen>6811</totallen><fileext>zip</fileext></appattach></appmsg></msg>', kind=(6 << 32) | 49)
        self.insert(5, '<msg><appmsg><title>回应</title><type>57</type><refermsg><svrid>9007199254740993</svrid><content>第一条</content></refermsg></appmsg></msg>', kind=(57 << 32) | 49)
        self.insert(6, b'\xff\xfe')
        self.insert(7, '<msg><appmsg><type>33</type><title>小程序</title></appmsg></msg>', kind=(33 << 32) | 49)
        self.insert(8, "", kind=34)
        source = self.source()
        bundle = source.export_bundle(self.base / "output", [self.chat(source)])
        rows = self.load(bundle, 'items.jsonl')
        by_kind = {x['kind']: x for x in rows}
        self.assertEqual("文章", by_kind['link']['title'])
        self.assertEqual("简介", by_kind['link']['text'])
        self.assertEqual('metadata_only', by_kind['file']['quality']['status'])
        self.assertEqual('工具.zip', by_kind['file']['attachments'][0]['name'])
        self.assertEqual(6811, by_kind['file']['attachments'][0]['size_bytes'])
        first = next(x for x in rows if x['provenance']['local_id'] == '1')
        self.assertEqual(first['id'], by_kind['quote']['relations'][0]['target_id'])
        self.assertEqual('decode_error', next(x for x in rows if x['provenance']['local_id'] == '6')['quality']['status'])
        self.assertEqual('unsupported', by_kind['unsupported']['quality']['status'])
        self.assertIsNone(by_kind['voice']['text'])

    def test_zstd_and_resource_metadata(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest("install the wechat optional dependency for compression test")
        self.insert(3, zstandard.ZstdCompressor().compress('压缩的正文'.encode()), compression=4)
        source = self.source()
        rows = source.read_items([self.chat(source)])['items']
        self.assertEqual('压缩的正文', rows[2]['text'])
        self.insert(4, zstandard.ZstdCompressor().compress(b'<msg><appmsg><type>6</type></appmsg></msg>'), kind=(6 << 32) | 49, compression=4)
        with sqlite3.connect(self.root / 'message/message_resource.db') as con:
            con.execute('CREATE TABLE MessageResourceInfo(message_id INTEGER, message_svr_id INTEGER, message_local_id INTEGER, chat_id INTEGER)')
            con.execute('CREATE TABLE ChatName2Id(user_name TEXT PRIMARY KEY)')
            con.execute('INSERT INTO ChatName2Id VALUES(?)', (self.username,))
            con.execute('INSERT INTO MessageResourceInfo VALUES(1,104,4,1)')
            con.execute('CREATE TABLE MessageResourceDetail(message_id INTEGER, size INTEGER, packed_info BLOB)')
            name = '附件.zip'.encode()
            con.execute('INSERT INTO MessageResourceDetail VALUES(1,100,?)', (b'\n'+bytes([len(name)])+name,))
        source = self.source()
        file = source.read_items([self.chat(source)])['items'][3]
        self.assertEqual('附件.zip', file['attachments'][0]['name'])

    def test_bundle_repeat_update_late_message_and_missing_not_deletion(self):
        source = self.source()
        chat = self.chat(source)
        first = source.export_bundle(self.base / 'output', [chat])
        self.assertEqual(2, first['manifest']['counts']['added'])
        repeat = source.export_bundle(self.base / 'output', [chat], previous=Path(first['path']))
        self.assertEqual([], self.load(repeat, 'changes.jsonl'))
        self.assertNotEqual(first['path'], repeat['path'])
        self.insert(3, '迟到的旧消息', timestamp=1690000000)
        with sqlite3.connect(self.db) as con:
            con.execute(f'UPDATE "{self.table}" SET message_content=? WHERE local_id=1', ('更新后的正文',))
            con.execute(f'DELETE FROM "{self.table}" WHERE local_id=2')
        source = self.source()
        changed = source.export_bundle(self.base / 'output', [chat], previous=Path(first['path']))
        self.assertEqual(1, changed['manifest']['counts']['added'])
        self.assertEqual(1, changed['manifest']['counts']['updated'])
        self.assertEqual(1, changed['manifest']['counts']['missing_from_previous'])
        self.assertEqual({'added', 'updated'}, {x['op'] for x in self.load(changed, 'changes.jsonl')})
        with self.assertRaises(ValueError):
            source.export_bundle(self.base / 'output', [chat], start='2023-01-01T00:00:00Z', previous=Path(first['path']))

    def test_complete_export_exceeds_page_limit_and_preserves_empty_conversation(self):
        with sqlite3.connect(self.db) as con:
            con.executemany(f'INSERT INTO "{self.table}" VALUES(?,?,?,?,?,?,?)',
                            [(i, i + 1000, 1, 1, 1700000000, '正文', 0) for i in range(3, 5004)])
        source = self.source()
        bundle = source.export_bundle(self.base / 'large', [self.chat(source)])
        self.assertEqual(5003, len(self.load(bundle, 'items.jsonl')))
        self.assertEqual(5003, bundle['manifest']['counts']['items'])
        empty = source.export_bundle(self.base / 'empty', [self.chat(source)], start='2025-01-01T00:00:00Z')
        self.assertEqual(0, empty['manifest']['counts']['items'])
        self.assertEqual(1, len(self.load(empty, 'conversations.jsonl')))

    def test_resource_local_id_does_not_match_another_conversation(self):
        self.insert(4, '<msg><appmsg><type>6</type></appmsg></msg>', kind=(6 << 32) | 49)
        with sqlite3.connect(self.root / 'message/message_resource.db') as con:
            con.execute('CREATE TABLE MessageResourceInfo(message_id INTEGER, message_svr_id INTEGER, message_local_id INTEGER, chat_id INTEGER)')
            con.execute('CREATE TABLE MessageResourceDetail(message_id INTEGER, size INTEGER, packed_info BLOB)')
            con.execute('INSERT INTO MessageResourceInfo VALUES(1,999,4,2)')
            con.execute('INSERT INTO MessageResourceDetail VALUES(1,100,?)', (b'private-other-chat.zip',))
        source = self.source()
        item = next(x for x in source.read_items([self.chat(source)])['items'] if x['kind'] == 'file')
        self.assertNotIn('private-other-chat.zip', json.dumps(item))

    def test_invalid_cursor_and_changed_catalog_fail_cleanly(self):
        source = self.source()
        chat = self.chat(source)
        with self.assertRaises(ValueError):
            source.read_items([chat], cursor=b'[]'.hex())
        token = source.read_items([chat])['snapshot']
        cursor = json.dumps({'snapshot': token, 'position': -1}).encode().hex()
        with self.assertRaises(ValueError):
            source.read_items([chat], cursor=cursor)
        with sqlite3.connect(self.root / 'contact/contact.db') as con:
            con.execute('UPDATE contact SET remark=? WHERE username=?', ('新群名', self.username))
        with self.assertRaises(ValueError):
            source.read_items([chat])
        with self.assertRaises(ValueError):
            self.source().read_items([chat], start='2024-01-01T00:00:00Z', end='2023-01-01T00:00:00Z')

    def test_source_is_read_only_and_exports_are_private_and_scoped(self):
        before = self.db.read_bytes()
        source = self.source()
        with source._connect(self.db) as con:
            with self.assertRaises(sqlite3.DatabaseError):
                con.execute(f'DELETE FROM "{self.table}"')
        bundle = source.export_bundle(self.base / 'output', [self.chat(source)])
        folder = Path(bundle['path'])
        self.assertEqual(0o700, folder.stat().st_mode & 0o777)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in folder.iterdir()))
        actors = self.load(bundle, 'actors.jsonl')
        self.assertNotIn('不相关联系人', json.dumps(actors, ensure_ascii=False))
        self.assertEqual(before, self.db.read_bytes())
        with self.assertRaises(ValueError):
            source.export_bundle(self.root / 'exports', [self.chat(source)])

    def test_reject_active_sidecar_symlink_missing_schema_and_unknown_scope(self):
        self.db.with_name(self.db.name + '-wal').write_bytes(b'active')
        with self.assertRaises(ValueError):
            self.source()
        self.db.with_name(self.db.name + '-wal').unlink()
        source = self.source()
        with self.assertRaises(ValueError):
            source.read_items(['not-a-conversation'])
        with self.assertRaises(ValueError):
            source.read_items([])
        with self.assertRaises(ValueError):
            source.read_items([self.chat(source)], start='2026-09-26')
        link = self.base / 'linked'
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            WeChatSource(link, 'test-account')
        with sqlite3.connect(self.db) as con:
            con.execute(f'ALTER TABLE "{self.table}" RENAME COLUMN create_time TO unknown_time')
        with self.assertRaises(ValueError):
            self.source()

    def test_cli_never_initializes_the_knowledge_store(self):
        home = self.base / 'mousia-home'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(['--snapshot', str(self.root), '--source-id', 'test-account', 'conversations'])
        self.assertEqual(0, code)
        self.assertFalse(home.exists())
        self.assertEqual('测试群', json.loads(out.getvalue())[0]['display_name'])

    def test_cli_accepts_common_message_filters(self):
        source = self.source()
        chat = self.chat(source)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                '--snapshot', str(self.root), '--source-id', 'test-account',
                'items', chat, '--author', '同事', '--kind', 'text', '--query', '第一',
            ])
        self.assertEqual(0, code)
        self.assertEqual(["第一条 https://example.com/a"], [x['text'] for x in json.loads(out.getvalue())['items']])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main([
                '--snapshot', str(self.root), '--source-id', 'test-account',
                'search', '第一条', '--author', '同事',
            ])
        self.assertEqual(0, code)
        self.assertEqual(["第一条 https://example.com/a"], [x['text'] for x in json.loads(out.getvalue())['items']])

    @staticmethod
    def load(bundle, filename):
        return [json.loads(line) for line in (Path(bundle['path']) / filename).read_text().splitlines()]


class WeChatSecondaryResourceTests(unittest.TestCase):
    """Exercise the optional favorite, moments, and official-account indexes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "snapshot"
        for name in ("contact", "message", "favorite", "sns"):
            (self.root / name).mkdir(parents=True)
        with sqlite3.connect(self.root / "contact/contact.db") as con:
            con.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
            con.executemany("INSERT INTO contact VALUES(?,?,?)", [
                ("gh_official", "产品公众号", ""), ("person-a", "张三", "同事"),
            ])
        table = "Msg_" + md5("gh_official".encode()).hexdigest()
        with sqlite3.connect(self.root / "message/message_0.db") as con:
            con.execute("CREATE TABLE Name2Id(user_name TEXT)")
            con.execute("INSERT INTO Name2Id VALUES(?)", ("gh_official",))
            con.execute(f'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY, server_id INTEGER, local_type INTEGER, real_sender_id INTEGER, create_time INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER)')
            con.execute(f'INSERT INTO "{table}" VALUES(?,?,?,?,?,?,?)', (1, 10, 1, 0, 1700000000, "公众号消息", 0))
        with sqlite3.connect(self.root / "favorite/favorite.db") as con:
            con.execute("CREATE TABLE fav_db_item(local_id INTEGER, type INTEGER, update_time INTEGER, content TEXT, fromusr TEXT, realchatname TEXT)")
            con.executemany("INSERT INTO fav_db_item VALUES(?,?,?,?,?,?)", [
                (1, 1, 1700000000, "<favitem><desc>要记住的收藏</desc></favitem>", "person-a", "群聊"),
                (2, 5, 1700000100, "<favitem><item><pagetitle>一篇文章</pagetitle><desc>收藏文章正文</desc><url>https://example.com/article</url></item></favitem>", "person-a", "群聊"),
            ])
        with sqlite3.connect(self.root / "sns/sns.db") as con:
            con.execute("CREATE TABLE SnsTimeLine(tid INTEGER, user_name TEXT, content TEXT)")
            con.executemany("INSERT INTO SnsTimeLine VALUES(?,?,?)", [
                (11, "person-a", "<TimelineObject><username>person-a</username><createTime>1700000200</createTime><contentDesc>朋友圈干货</contentDesc></TimelineObject>"),
                (12, "person-a", "<TimelineObject><username>person-a</username><createTime>1700000300</createTime><contentDesc>带链接</contentDesc><ContentObject><contentUrl>https://example.com/moment</contentUrl></ContentObject></TimelineObject>"),
            ])

    def source(self):
        return WeChatSource(self.root, "secondary")

    def test_official_accounts_and_optional_capabilities(self):
        source = self.source()
        self.assertEqual(["产品公众号"], [row["display_name"] for row in source.list_official_accounts()])
        conversation = source.list_conversations()[0]
        self.assertTrue(conversation["is_official"])
        self.assertTrue(source.status()["favorite_index"])
        self.assertTrue(source.status()["moments_index"])

    def test_favorites_filters_paging_and_export(self):
        source = self.source()
        found = source.search_favorites("文章", kinds=["article"], has_links=True)
        self.assertEqual(["一篇文章"], [row["title"] for row in found["items"]])
        first = source.list_favorites(limit=1)
        second = source.list_favorites(limit=1, cursor=first["next_cursor"])
        self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])
        bundle = source.export_favorites(self.base / "favorites")
        self.assertEqual(2, bundle["manifest"]["counts"]["items"])
        self.assertTrue((Path(bundle["path"]) / "items.jsonl").is_file())

    def test_moments_filters_and_unified_search(self):
        source = self.source()
        found = source.search_moments("干货", author_usernames=["同事"])
        self.assertEqual(["朋友圈干货"], [row["text"] for row in found["items"]])
        linked = source.list_moments(has_links=True)
        self.assertEqual(["带链接"], [row["text"] for row in linked["items"]])
        all_found = source.search_all("干货")
        self.assertEqual(["朋友圈干货"], [row["text"] for row in all_found["items"]])

    def test_cli_secondary_commands_and_export(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--snapshot", str(self.root), "--source-id", "secondary", "official"])
        self.assertEqual(0, code)
        self.assertEqual("产品公众号", json.loads(out.getvalue())[0]["display_name"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--snapshot", str(self.root), "--source-id", "secondary", "moments", "--query", "干货"])
        self.assertEqual(0, code)
        self.assertEqual(["朋友圈干货"], [row["text"] for row in json.loads(out.getvalue())["items"]])
        output_dir = self.base / "favorite-export"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["--snapshot", str(self.root), "--source-id", "secondary", "favorites", "--output", str(output_dir)])
        self.assertEqual(0, code)
        self.assertTrue((Path(json.loads(out.getvalue())["path"]) / "manifest.json").is_file())
