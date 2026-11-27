"""账号默认标记、多账号 upsert/删除以及 BOX 缓存隔离的 DAL 测试。

使用临时 sqlite 文件，不碰真实数据库。
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from kanna_connection_redive_2.database.dal import SQALA  # noqa: E402
from kanna_connection_redive_2.database.models import (  # noqa: E402
    PlayerUnit,
    RefreshAccount,
)


class DalTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "test.db")
        self.sqla = SQALA(self.db_path)
        await self.sqla.create_all()

    async def asyncTearDown(self):
        await self.sqla.engine.dispose()
        self.tmpdir.cleanup()

    async def account_names(self, user_id):
        return [
            (item.name, item.is_default)
            for item in await self.sqla.query_account(user_id)
        ]


class AccountDefaultTests(DalTestCase):
    async def test_first_account_becomes_default(self):
        account = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        self.assertEqual(account.is_default, 1)
        self.assertIs(
            (await self.sqla.query_default_account(100)).id, account.id
        )

    async def test_multi_account_same_platform(self):
        first = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        second = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 222, "account": "u2", "name": "B"}
        )
        self.assertEqual(await self.account_names(100), [("A", 1), ("B", 0)])
        self.assertNotEqual(first.id, second.id)

    async def test_bind_default_flag_switches_default(self):
        await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        await self.sqla.add_account(
            100,
            {
                "platform": 1,
                "viewer_id": 222,
                "account": "u2",
                "name": "B",
                "is_default": 1,
            },
        )
        self.assertEqual(await self.account_names(100), [("A", 0), ("B", 1)])
        self.assertEqual((await self.sqla.query_default_account(100)).name, "B")

    async def test_rebind_same_viewer_updates_in_place(self):
        await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        await self.sqla.add_account(
            100,
            {
                "platform": 0,
                "viewer_id": 111,
                "account": "u1",
                "password": "new-key",
                "name": "A2",
            },
        )
        accounts = await self.sqla.query_account(100)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].name, "A2")
        self.assertEqual(accounts[0].password, "new-key")

    async def test_update_by_explicit_id_keeps_default(self):
        first = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        await self.sqla.add_account(
            100, {"platform": 1, "viewer_id": 222, "account": "u2", "name": "B"}
        )
        await self.sqla.add_account(100, {"id": first.id, "name": "A3"})
        self.assertEqual(await self.account_names(100), [("A3", 1), ("B", 0)])

    async def test_set_default_account(self):
        first = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        second = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 222, "account": "u2", "name": "B"}
        )
        await self.sqla.set_default_account(100, second.id)
        self.assertEqual(await self.account_names(100), [("A", 0), ("B", 1)])
        self.assertEqual((await self.sqla.query_default_account(100)).id, second.id)
        # 别人的账号不允许被切换
        self.assertIsNone(await self.sqla.set_default_account(101, first.id))

    async def test_delete_default_promotes_next_account(self):
        first = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        second = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 222, "account": "u2", "name": "B"}
        )
        deleted = await self.sqla.delete_account(100, first.id)
        self.assertEqual(deleted.id, first.id)
        self.assertEqual(await self.account_names(100), [("B", 1)])
        self.assertEqual((await self.sqla.query_default_account(100)).id, second.id)

    async def test_delete_other_users_account_is_ignored(self):
        first = await self.sqla.add_account(
            100, {"platform": 0, "viewer_id": 111, "account": "u1", "name": "A"}
        )
        self.assertIsNone(await self.sqla.delete_account(101, first.id))
        self.assertEqual(len(await self.sqla.query_account(100)), 1)

    async def test_delete_last_account_cleans_refresh_credentials(self):
        await self.sqla.add_account(
            100,
            {
                "platform": 0,
                "viewer_id": 111,
                "account": "u1",
                "name": "A",
                "refresh": "bili-account",
            },
        )
        await self.sqla.add_refresh(
            RefreshAccount(account="bili-account", password="pw")
        )
        account = (await self.sqla.query_account(100))[0]
        await self.sqla.delete_account(100, account.id)
        self.assertIsNone(await self.sqla.query_refresh("bili-account"))


class LegacyMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "legacy.db")

    async def asyncTearDown(self):
        self.tmpdir.cleanup()

    def _create_legacy_table(self):
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "CREATE TABLE account ("
            "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, "
            "platform INTEGER NOT NULL, viewer_id INTEGER, allow_others INTEGER, "
            "account VARCHAR, password VARCHAR, name VARCHAR, refresh VARCHAR)"
        )
        connection.execute(
            "INSERT INTO account (user_id, platform, viewer_id, name) VALUES (1, 0, 111, 'A')"
        )
        connection.execute(
            "INSERT INTO account (user_id, platform, viewer_id, name) VALUES (1, 1, 222, 'B')"
        )
        connection.execute(
            "INSERT INTO account (user_id, platform, viewer_id, name) VALUES (2, 0, 333, 'C')"
        )
        connection.commit()
        connection.close()

    async def test_create_all_backfills_default_accounts(self):
        self._create_legacy_table()
        sqla = SQALA(self.db_path)
        await sqla.create_all()
        try:
            first_user = await sqla.query_account(1)
            self.assertEqual(
                [(item.name, item.is_default) for item in first_user],
                [("A", 1), ("B", 0)],
            )
            self.assertEqual((await sqla.query_default_account(2)).name, "C")
            # 再跑一次迁移不应该改变结果
            await sqla.create_all()
            self.assertEqual(
                [
                    (item.name, item.is_default)
                    for item in await sqla.query_account(1)
                ],
                [("A", 1), ("B", 0)],
            )
        finally:
            await sqla.engine.dispose()


def unit(unit_id, pcrid, user_id=100):
    return PlayerUnit(
        user_id=user_id,
        pcrid=pcrid,
        unit_id=unit_id,
        name="玩家",
        rarity=5,
        love_level=8,
        level=200,
        rank=20,
        equip_1="1",
        equip_2="1",
        equip_3="1",
        equip_4="1",
        equip_5="1",
        equip_6="1",
    )


class PlayerUnitScopeTests(DalTestCase):
    async def test_box_cache_isolated_per_viewer(self):
        await self.sqla.refresh_player_units([unit(1001, 111)], 100, 111)
        await self.sqla.refresh_player_units([unit(1002, 222)], 100, 222)
        first = await self.sqla.get_player_units(100, pcrid=111)
        second = await self.sqla.get_player_units(100, pcrid=222)
        self.assertEqual([item.unit_id for item in first], [1001])
        self.assertEqual([item.unit_id for item in second], [1002])
        self.assertEqual(len(await self.sqla.get_player_units(100)), 2)

    async def test_refresh_only_replaces_same_viewer(self):
        await self.sqla.refresh_player_units([unit(1001, 111)], 100, 111)
        await self.sqla.refresh_player_units([unit(1002, 222)], 100, 222)
        await self.sqla.refresh_player_units([unit(1003, 111)], 100, 111)
        self.assertEqual(
            [item.unit_id for item in await self.sqla.get_player_units(100, pcrid=111)],
            [1003],
        )
        self.assertEqual(
            [item.unit_id for item in await self.sqla.get_player_units(100, pcrid=222)],
            [1002],
        )


if __name__ == "__main__":
    unittest.main()
