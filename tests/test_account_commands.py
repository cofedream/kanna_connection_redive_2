"""账号查看/解绑指令的回归测试：不需要 bot、数据库或游戏账号。"""

import ast
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "account_commands", ROOT / "util" / "account_commands.py"
)
account_commands = importlib.util.module_from_spec(spec)
spec.loader.exec_module(account_commands)

# _unbind_account_text 依赖的模块级名字；测试里用桩替换数据库和缓存。
HELPERS = (
    "UNBIND_USAGE",
    "parse_platform",
    "format_platform_unknown",
    "format_not_bound",
    "format_unbind_done",
)


def load_login_functions(names):
    """只执行 login.py 中指认的函数，避开 Hoshino 的启动装饰器和数据库导入。"""
    tree = ast.parse((ROOT / "login.py").read_text(encoding="utf-8"))
    funcs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in names
    ]
    assert {node.name for node in funcs} == set(names), "login.py 里的函数被改名或删除了"
    namespace = {"Account": object}
    namespace.update(
        {name: getattr(account_commands, name) for name in HELPERS}
    )
    namespace["__name__"] = "test_account_commands"
    code = compile(ast.Module(body=funcs, type_ignores=[]), "login.py", "exec")
    exec(code, namespace)
    return namespace


class PlatformHelpersTests(unittest.TestCase):
    def test_platform_names_match_basedata(self):
        spec = importlib.util.spec_from_file_location("basedata", ROOT / "basedata.py")
        basedata = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(basedata)
        self.assertEqual(
            account_commands.PLATFORM_NAMES,
            {
                basedata.Platform.b_id.value: "B服",
                basedata.Platform.qu_id.value: "渠道服",
                basedata.Platform.tw_id.value: "台服",
            },
        )

    def test_parse_platform_aliases(self):
        for text, expected in [
            ("B服", 0),
            ("b服", 0),
            ("B站", 0),
            ("官服", 0),
            ("b", 0),
            ("0", 0),
            ("渠道服", 1),
            ("渠道", 1),
            ("qu", 1),
            ("1", 1),
            ("台服", 2),
            ("tw", 2),
            ("2", 2),
            (" 台服 ", 2),
        ]:
            with self.subTest(text=text):
                self.assertEqual(account_commands.parse_platform(text), expected)
        for text in ("日服", "", None):
            with self.subTest(text=text):
                self.assertIsNone(account_commands.parse_platform(text))


class AccountTextTests(unittest.TestCase):
    def account(self, platform, name, viewer_id, allow_others=0):
        return NS(
            platform=platform, name=name, viewer_id=viewer_id, allow_others=allow_others
        )

    def test_empty_list_points_to_help(self):
        text = account_commands.format_account_list([])
        self.assertIn("还没有绑定账号", text)
        self.assertIn("绑定账号帮助", text)

    def test_list_sorted_with_name_and_permission(self):
        accounts = [
            self.account(2, "台服号", 222, allow_others=2),
            self.account(0, "栞栞", 111),
        ]
        text = account_commands.format_account_list(accounts)
        self.assertIn("共 2 个", text)
        self.assertIn("1. B服：栞栞（游戏ID 111），触发权限：仅本人", text)
        self.assertIn("2. 台服：台服号（游戏ID 222），触发权限：任何人", text)

    def test_list_falls_back_for_missing_fields(self):
        text = account_commands.format_account_list([self.account(1, None, None)])
        self.assertIn("渠道服：未知昵称（游戏ID 未知ID）", text)
        self.assertIn("触发权限：仅本人", text)

    def test_unbind_texts(self):
        account = self.account(0, "栞栞", 111)
        done = account_commands.format_unbind_done(account)
        self.assertIn("已解绑：B服：栞栞（游戏ID 111）", done)
        self.assertIn("监控", done)

    def test_unknown_platform_and_not_bound_texts(self):
        self.assertIn("日服", account_commands.format_platform_unknown("日服"))
        self.assertIn("B服 / 渠道服 / 台服", account_commands.format_platform_unknown("日服"))
        self.assertIn("没有绑定 台服 账号", account_commands.format_not_bound(2))


class UnbindFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bound = NS(
            platform=0, name="栞栞", viewer_id=111, allow_others=0, account="uid"
        )
        self.sqla = NS(
            query_account=AsyncMock(return_value=[self.bound]),
            delete_account=AsyncMock(),
        )
        self.namespace = load_login_functions(
            {"_unbind_account_text", "_forget_client_cache"}
        )
        self.namespace["pcr_sqla"] = self.sqla
        self.cache = self.namespace["client_cache"] = {}
        self.unbind = self.namespace["_unbind_account_text"]

    async def test_empty_args_returns_usage(self):
        text = await self.unbind(100, "   ")
        self.assertEqual(text, account_commands.UNBIND_USAGE)
        self.sqla.query_account.assert_not_awaited()
        self.sqla.delete_account.assert_not_awaited()

    async def test_unknown_platform_does_not_touch_database(self):
        text = await self.unbind(100, "日服")
        self.assertIn("无法识别服务器", text)
        self.sqla.query_account.assert_not_awaited()
        self.sqla.delete_account.assert_not_awaited()

    async def test_not_bound_platform_does_not_delete(self):
        text = await self.unbind(100, "台服")
        self.assertIn("没有绑定 台服 账号", text)
        self.sqla.delete_account.assert_not_awaited()

    async def test_unbind_deletes_immediately_without_confirmation(self):
        self.cache[(111, 0)] = object()
        text = await self.unbind(100, "B服")
        self.assertIn("已解绑", text)
        self.sqla.delete_account.assert_awaited_once_with(100, 0)
        self.assertNotIn((111, 0), self.cache)

    async def test_platform_aliases_all_unbind(self):
        for alias in ("b服", "官服", "b", "0", "B服 多余参数"):
            with self.subTest(alias=alias):
                self.sqla.delete_account.reset_mock()
                text = await self.unbind(100, alias)
                self.assertIn("已解绑", text)
                self.sqla.delete_account.assert_awaited_once_with(100, 0)


if __name__ == "__main__":
    unittest.main()
