"""监控登记在「开机」与「退出」路径上的接线回归测试：不需要 bot 或游戏账号。"""

import ast
import asyncio
import contextlib
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]


def load_methods(path, class_name, methods, namespace):
    """只执行指认的类方法，避开 Hoshino 的启动装饰器、网络客户端和数据库。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    cls.bases = []
    cls.body = [n for n in cls.body if getattr(n, "name", None) in methods]
    isolated = ast.Module(body=[cls], type_ignores=[])
    namespace["__name__"] = "test_monitor_lifecycle"
    exec(compile(isolated, str(path), "exec"), namespace)
    return namespace[class_name]


def registry_stub():
    return NS(
        KIND_CLAN="clan",
        register=Mock(),
        unregister=Mock(),
        heartbeat=Mock(),
    )


class InitRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_registers_monitor_for_restart_notice(self):
        registry = registry_stub()
        top = NS(
            clan_battle_id=5,
            lap_num=2,
            damage_history=[],
            period_rank=11,
            user_clan=NS(clan_name="公会"),
        )

        class EntryTracker:
            def __init__(self, *args, **kwargs):
                pass

            def settle(self, history):
                pass

            def dump(self):
                return "{}"

        async def home_index():
            return NS(user_clan=NS(clan_id=7))

        async def noop(*args, **kwargs):
            return None

        cls = load_methods(
            ROOT / "clanbattle/model.py",
            "ClanBattle",
            {"init"},
            {
                "contextlib": contextlib,
                "time": time,
                "BaseClient": object,
                "Dict": dict,
                "EntryTracker": EntryTracker,
                "pcr_sqla": NS(
                    get_battle_entries=noop, save_battle_entries=noop
                ),
                "clan_boss_info": NS(lap2stage=lambda lap: "B"),
                "monitor_registry": registry,
            },
        )
        instance = cls.__new__(cls)
        instance.group_id = 100
        instance.loop_num = 0
        instance.entry_lock = asyncio.Lock()
        instance.damage_correction = NS(reset=lambda top: None)
        instance.account_name = "玩家A"

        async def get_coin():
            return 0

        async def get_clanbattle_top():
            return top

        async def all_member():
            return {}

        instance.get_coin = get_coin
        instance.get_clanbattle_top = get_clanbattle_top
        instance.refresh_latest_time = lambda top: None
        instance.all_member = all_member

        await instance.init(NS(home_index=home_index), 22, 11)

        registry.register.assert_called_once_with(
            "clan", 100, bot_id=11, user_id=22, loop_num=1, account_name="玩家A"
        )


class HandleExitTests(unittest.IsolatedAsyncioTestCase):
    def make_handle(self, *, loop_num=3, current_loop=3, error_count=0, client_ok=True):
        registry = registry_stub()
        sent = []
        printed = []

        async def anywhere_send(message, group_id, bot_id=None):
            sent.append((message, group_id, bot_id))
            return True

        async def check_client(client):
            return client_ok

        handle_cls = load_methods(
            ROOT / "clanbattle/model.py",
            "ClanbattleHandle",
            {"__aexit__"},
            {
                "asyncio": asyncio,
                "time": time,
                "monitor_registry": registry,
                "check_client": check_client,
                "anywhere_send": anywhere_send,
                "run_group": {},
                "logger": NS(error=Mock()),
                "format_exc": lambda: "traceback",
                "print": printed.append,
            },
        )
        clan_info = NS(
            group_id=100,
            bot_id=11,
            user_id=22,
            client=NS(),
            loop_num=current_loop,
            loop_check=1,
            dao_update_time=0,
            error_count=error_count,
        )
        handle = handle_cls.__new__(handle_cls)
        handle.clan_info = clan_info
        handle.loop_num = loop_num
        return handle, registry, sent

    async def test_cancelled_during_shutdown_is_not_swallowed(self):
        handle, registry, sent = self.make_handle()
        result = await handle.__aexit__(
            asyncio.CancelledError, asyncio.CancelledError(), None
        )
        # 返回 False 才会让 CancelledError 继续向上抛，不把它当成可恢复错误。
        self.assertFalse(result)
        registry.unregister.assert_not_called()
        self.assertEqual(sent, [])

    async def test_manual_stop_unregisters_and_announces(self):
        handle, registry, sent = self.make_handle(loop_num=3, current_loop=4)
        await handle.__aexit__(RuntimeError, RuntimeError("boom"), None)
        registry.unregister.assert_called_once_with("clan", 100, 3)
        self.assertEqual(sent[0][0], "#编号HN0003监控已关闭")

    async def test_kicked_account_unregisters_and_announces(self):
        handle, registry, sent = self.make_handle(client_ok=False)
        await handle.__aexit__(RuntimeError, RuntimeError("boom"), None)
        registry.unregister.assert_called_once_with("clan", 100, 3)
        self.assertEqual(sent[0][0], "当前账号被顶号，出刀监控已退出")

    async def test_retry_exhausted_unregisters_and_announces(self):
        handle, registry, sent = self.make_handle(error_count=4)
        await handle.__aexit__(RuntimeError, RuntimeError("boom"), None)
        registry.unregister.assert_called_once_with("clan", 100, 3)
        self.assertIn("超过最大重试次数", sent[0][0])

    async def test_recoverable_error_keeps_registration(self):
        handle, registry, sent = self.make_handle()
        result = await handle.__aexit__(RuntimeError, RuntimeError("boom"), None)
        self.assertTrue(result)
        registry.unregister.assert_not_called()
        self.assertEqual(sent, [])
        # 可恢复错误会继续轮询，登记必须保留（否则下次重启会误报退出）。
        self.assertEqual(handle.clan_info.error_count, 1)


if __name__ == "__main__":
    unittest.main()
