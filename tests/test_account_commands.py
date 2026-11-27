"""账号查看/切换默认/解绑指令的回归测试：不需要 bot、数据库或游戏账号。"""

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


def load_login_functions(names, extra_namespace=None):
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
        {
            name: getattr(account_commands, name)
            for name in dir(account_commands)
            if not name.startswith("_")
        }
    )
    namespace["__name__"] = "test_account_commands"
    namespace.update(extra_namespace or {})
    code = compile(ast.Module(body=funcs, type_ignores=[]), "login.py", "exec")
    exec(code, namespace)
    return namespace


def account(account_id, platform=0, name="栞栞", viewer_id=111, allow_others=0, default=0):
    return NS(
        id=account_id,
        user_id=100,
        platform=platform,
        name=name,
        viewer_id=viewer_id,
        allow_others=allow_others,
        is_default=default,
    )


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


class AccountOrderTests(unittest.TestCase):
    def test_default_account_marked_or_first(self):
        marked = account(3, platform=1, default=1)
        accounts = [account(1), account(2), marked]
        self.assertIs(account_commands.pick_default_account(accounts), marked)
        unmarked = [account(2), account(1)]
        self.assertIs(account_commands.pick_default_account(unmarked), unmarked[1])
        self.assertIsNone(account_commands.pick_default_account([]))

    def test_sort_accounts_by_platform_then_id(self):
        accounts = [account(5, platform=2), account(3, platform=0), account(1, platform=0)]
        sorted_accounts = account_commands.sort_accounts(accounts)
        self.assertEqual([item.id for item in sorted_accounts], [1, 3, 5])

    def test_pick_preferred_account_by_platform(self):
        b_old = account(1, platform=0, default=0)
        b_default = account(2, platform=0, default=1)
        qu = account(3, platform=1)
        accounts = [b_old, b_default, qu]
        self.assertIs(
            account_commands.pick_preferred_account(accounts, 0), b_default
        )
        self.assertIs(account_commands.pick_preferred_account(accounts, 1), qu)
        self.assertIsNone(account_commands.pick_preferred_account(accounts, 2))
        self.assertIs(
            account_commands.pick_preferred_account(accounts, 0, viewer_id=111),
            b_old,
        )

    def test_pick_preferred_accounts_dedupes_per_platform(self):
        b_old = account(1, platform=0, default=0)
        b_default = account(2, platform=0, default=1)
        qu = account(3, platform=1)
        picked = account_commands.pick_preferred_accounts([b_old, b_default, qu])
        self.assertEqual([item.platform for item in picked], [0, 1])
        self.assertIs(picked[0], b_default)

    def test_group_default_accounts(self):
        first = account(1, platform=0, default=1)
        first.user_id = 1
        second = account(2, platform=0, default=0)
        second.user_id = 1
        other = account(3, platform=1, default=0)
        other.user_id = 2
        grouped = account_commands.group_default_accounts([first, second, other])
        self.assertIs(grouped[1], first)
        self.assertIs(grouped[2], other)


class DefaultFlagTests(unittest.TestCase):
    def test_split_default_flag(self):
        for token in ("默认", "[默认]", "设为默认", "作为默认"):
            with self.subTest(token=token):
                tokens, flag = account_commands.split_default_flag(
                    ["B绑定账号", "uid", "key", token]
                )
                self.assertTrue(flag)
                self.assertEqual(tokens, ["B绑定账号", "uid", "key"])
        tokens, flag = account_commands.split_default_flag(["绑定账号", "a", "b"])
        self.assertFalse(flag)
        self.assertEqual(tokens, ["绑定账号", "a", "b"])


class AccountTextTests(unittest.TestCase):
    def test_empty_list_points_to_help(self):
        text = account_commands.format_account_list([])
        self.assertIn("还没有绑定账号", text)
        self.assertIn("绑定账号帮助", text)

    def test_list_shows_default_marker_and_permission(self):
        accounts = [
            account(2, platform=2, name="台服号", viewer_id=222, allow_others=2),
            account(1, platform=0, name="栞栞", viewer_id=111, default=1),
        ]
        text = account_commands.format_account_list(accounts)
        self.assertIn("共 2 个", text)
        self.assertIn(
            "1. B服：栞栞（游戏ID 111），触发权限：仅本人 [默认]", text
        )
        self.assertIn(
            "2. 台服：台服号（游戏ID 222），触发权限：任何人", text
        )
        self.assertIn("切换默认账号", text)

    def test_list_falls_back_for_missing_fields(self):
        entry = NS(
            platform=1,
            name=None,
            viewer_id=None,
            allow_others=None,
            is_default=0,
        )
        text = account_commands.format_account_list([entry])
        self.assertIn("渠道服：未知昵称（游戏ID 未知ID）", text)
        self.assertIn("触发权限：仅本人", text)

    def test_bind_success_text(self):
        new_default = account(1, default=1)
        text = account_commands.format_bind_success(new_default, new_default)
        self.assertIn("绑定成功", text)
        self.assertIn("已设为默认账号", text)

        extra = account(2, default=0)
        text = account_commands.format_bind_success(extra, new_default)
        self.assertIn("绑定成功", text)
        self.assertIn("当前默认账号", text)
        self.assertIn("切换默认账号", text)

    def test_unbind_and_failure_texts(self):
        entry = account(1)
        done = account_commands.format_unbind_done(entry)
        self.assertIn("已解绑：B服：栞栞（游戏ID 111）", done)
        self.assertIn("监控", done)
        promoted = account(2, name="新默认", default=1)
        done = account_commands.format_unbind_done(entry, promoted)
        self.assertIn("已将 B服：新默认", done)

        self.assertIn("日服", account_commands.format_platform_unknown("日服"))
        self.assertIn(
            "B服 / 渠道服 / 台服", account_commands.format_platform_unknown("日服")
        )
        self.assertIn("没有绑定 台服 账号", account_commands.format_not_bound(2))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.b1 = account(1, platform=0, name="B1")
        self.b2 = account(2, platform=0, name="B2")
        self.qu = account(3, platform=1, name="QU")
        self.accounts = [self.b1, self.b2, self.qu]

    def test_selector_by_global_index(self):
        target, error = account_commands.resolve_account_selector("2", self.accounts)
        self.assertIsNone(error)
        self.assertIs(target, self.b2)

    def test_selector_by_platform_with_single_account(self):
        target, error = account_commands.resolve_account_selector("渠道服", self.accounts)
        self.assertIsNone(error)
        self.assertIs(target, self.qu)

    def test_selector_rejects_out_of_range_and_unknown(self):
        _, error = account_commands.resolve_account_selector("9", self.accounts)
        self.assertIn("超出范围", error)
        _, error = account_commands.resolve_account_selector("日服", self.accounts)
        self.assertIn("无法识别账号", error)
        _, error = account_commands.resolve_account_selector("", self.accounts)
        self.assertIn("请指定", error)

    def test_selector_ambiguous_platform_lists_options(self):
        _, error = account_commands.resolve_account_selector("B服", self.accounts)
        self.assertIn("B服绑定了 2 个账号", error)
        self.assertIn("1. B服：B1", error)
        self.assertIn("2. B服：B2", error)


class MonitorSelectorTests(unittest.TestCase):
    def setUp(self):
        self.b1 = account(1, platform=0, name="B1", default=1)
        self.b2 = account(2, platform=0, name="B2")
        self.qu = account(3, platform=1, name="QU")
        self.accounts = [self.b1, self.b2, self.qu]

    def test_empty_uses_default_account(self):
        target, error = account_commands.resolve_monitor_account(
            "", self.accounts, self.b1
        )
        self.assertIsNone(error)
        self.assertIs(target, self.b1)

    def test_default_keywords_use_default_account(self):
        for token in ("默认", "[默认]", "当前"):
            with self.subTest(token=token):
                target, error = account_commands.resolve_monitor_account(
                    token, self.accounts, self.b2
                )
                self.assertIsNone(error)
                self.assertIs(target, self.b2)

    def test_index_selects_account(self):
        target, error = account_commands.resolve_monitor_account(
            "2", self.accounts, self.b1
        )
        self.assertIsNone(error)
        self.assertIs(target, self.b2)

    def test_platform_name_selects_unique_account(self):
        target, error = account_commands.resolve_monitor_account(
            "渠道服", self.accounts, self.b1
        )
        self.assertIsNone(error)
        self.assertIs(target, self.qu)

    def test_invalid_selector_returns_list(self):
        target, error = account_commands.resolve_monitor_account(
            "9", self.accounts, self.b1
        )
        self.assertIsNone(target)
        self.assertIn("超出范围", error)
        target, error = account_commands.resolve_monitor_account(
            "日服", self.accounts, self.b1
        )
        self.assertIsNone(target)
        self.assertIn("无法识别账号", error)

    def test_no_accounts_returns_hint(self):
        target, error = account_commands.resolve_monitor_account("", [])
        self.assertIsNone(target)
        self.assertIn("还没有绑定账号", error)


class UnbindTargetTests(unittest.TestCase):
    def setUp(self):
        self.b1 = account(1, platform=0, name="B1")
        self.b2 = account(2, platform=0, name="B2")
        self.qu = account(3, platform=1, name="QU")
        self.accounts = [self.b1, self.b2, self.qu]

    def test_empty_args_usage(self):
        target, error = account_commands.resolve_unbind_target([], self.accounts)
        self.assertIsNone(target)
        self.assertEqual(error, account_commands.UNBIND_USAGE)

    def test_unknown_platform(self):
        target, error = account_commands.resolve_unbind_target(["日服"], self.accounts)
        self.assertIsNone(target)
        self.assertIn("无法识别服务器", error)

    def test_not_bound_platform(self):
        target, error = account_commands.resolve_unbind_target(["台服"], self.accounts)
        self.assertIsNone(target)
        self.assertIn("没有绑定 台服 账号", error)

    def test_single_account_platform(self):
        target, error = account_commands.resolve_unbind_target(
            ["渠道服"], self.accounts
        )
        self.assertIsNone(error)
        self.assertIs(target, self.qu)

    def test_global_index(self):
        target, error = account_commands.resolve_unbind_target(["2"], self.accounts)
        self.assertIsNone(error)
        self.assertIs(target, self.b2)

    def test_platform_with_index(self):
        target, error = account_commands.resolve_unbind_target(
            ["B服", "2"], self.accounts
        )
        self.assertIsNone(error)
        self.assertIs(target, self.b2)

    def test_ambiguous_platform_requires_choice(self):
        target, error = account_commands.resolve_unbind_target(["B服"], self.accounts)
        self.assertIsNone(target)
        self.assertIn("绑定了 2 个账号", error)
        self.assertIn("解绑账号 1 或 解绑账号 B服 1", error)


class UnbindFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bound = account(7, platform=0, name="栞栞", viewer_id=111, default=1)
        self.sqla = NS(
            query_account=AsyncMock(return_value=[self.bound]),
            delete_account=AsyncMock(return_value=self.bound),
            query_default_account=AsyncMock(return_value=None),
        )
        self.namespace = load_login_functions(
            {"_unbind_account_text", "_forget_client_cache", "_switch_default_text"},
            {"pcr_sqla": self.sqla},
        )
        self.cache = self.namespace["client_cache"] = {}
        self.unbind = self.namespace["_unbind_account_text"]
        self.switch = self.namespace["_switch_default_text"]

    async def test_empty_args_returns_usage(self):
        text = await self.unbind(100, "   ")
        self.assertEqual(text, account_commands.UNBIND_USAGE)
        self.sqla.delete_account.assert_not_awaited()

    async def test_unknown_platform_does_not_touch_database(self):
        text = await self.unbind(100, "日服")
        self.assertIn("无法识别服务器", text)
        self.sqla.delete_account.assert_not_awaited()

    async def test_not_bound_platform_does_not_delete(self):
        text = await self.unbind(100, "台服")
        self.assertIn("没有绑定 台服 账号", text)
        self.sqla.delete_account.assert_not_awaited()

    async def test_unbind_deletes_by_account_id_and_clears_cache(self):
        self.cache[(111, 0)] = object()
        text = await self.unbind(100, "B服")
        self.assertIn("已解绑", text)
        self.sqla.delete_account.assert_awaited_once_with(100, 7)
        self.assertNotIn((111, 0), self.cache)

    async def test_unbind_default_reports_promoted_account(self):
        promoted = account(8, platform=1, name="新默认", default=1)
        self.sqla.query_default_account.return_value = promoted
        text = await self.unbind(100, "B服")
        self.assertIn("已将 渠道服：新默认", text)

    async def test_unbind_non_default_does_not_query_promotion(self):
        self.bound.is_default = 0
        await self.unbind(100, "B服")
        self.sqla.query_default_account.assert_not_awaited()

    async def test_switch_default_by_index(self):
        second = account(9, platform=1, name="二号")
        self.sqla.query_account.return_value = [self.bound, second]
        self.sqla.set_default_account = AsyncMock(return_value=second)
        self.namespace["pcr_sqla"] = self.sqla
        text = await self.switch(100, "2")
        self.assertIn("已将默认账号切换为", text)
        self.assertIn("渠道服：二号", text)
        self.sqla.set_default_account.assert_awaited_once_with(100, 9)

    async def test_switch_default_without_accounts(self):
        self.sqla.query_account.return_value = []
        self.namespace["pcr_sqla"] = self.sqla
        text = await self.switch(100, "1")
        self.assertIn("还没有绑定账号", text)


if __name__ == "__main__":
    unittest.main()
