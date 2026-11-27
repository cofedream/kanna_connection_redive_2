"""监控登记落盘与重启补播的回归测试：不需要 bot 或游戏账号。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "monitor_registry", ROOT / "util" / "monitor_registry.py"
)
registry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registry)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "monitors.json"
        registry._PATH = self.path
        registry.logger = Mock()
        registry.reset_state()

    def restart(self):
        """模拟进程重启：丢掉内存状态，只保留磁盘文件。"""
        registry.reset_state()

    def saved(self):
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))["monitors"]

    def test_register_persists_entry(self):
        registry.register(
            registry.KIND_CLAN,
            100,
            bot_id=11,
            user_id=22,
            loop_num=3,
            account_name="玩家A",
        )
        saved = self.saved()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["group_id"], 100)
        self.assertEqual(saved[0]["loop_num"], 3)
        self.assertEqual(saved[0]["account_name"], "玩家A")
        self.assertEqual(saved[0]["heartbeat"], saved[0]["started_at"])
        # 本进程里新开的监控不是「重启残留」，不该被补播。
        self.assertEqual(registry.pending_exits(), [])
        self.assertFalse(self.path.with_name("monitors.json.tmp").exists())

    def test_register_overwrites_same_group(self):
        registry.register(registry.KIND_CLAN, 100, loop_num=1)
        registry.register(registry.KIND_CLAN, 100, loop_num=2, account_name="新")
        saved = self.saved()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["loop_num"], 2)
        self.assertEqual(saved[0]["account_name"], "新")

    def test_restart_leaves_pending_exit(self):
        registry.register(registry.KIND_CLAN, 100, loop_num=3)
        self.restart()
        entries = registry.pending_exits()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["group_id"], 100)
        self.assertEqual(entries[0]["loop_num"], 3)
        self.assertTrue(registry.is_pending(entries[0]))
        # pending_exits 是快照，重复读取不应该把登记消费掉。
        self.assertEqual(len(registry.pending_exits()), 1)

    def test_resolve_pending_removes_entry(self):
        registry.register(registry.KIND_CLAN, 100, loop_num=3)
        self.restart()
        entry = registry.pending_exits()[0]
        registry.resolve_pending(entry)
        self.assertEqual(self.saved(), [])
        self.restart()
        self.assertEqual(registry.pending_exits(), [])

    def test_reopened_monitor_cancels_stale_notice(self):
        registry.register(
            registry.KIND_CLAN, 100, loop_num=3, account_name="旧"
        )
        self.restart()
        stale = registry.pending_exits()[0]
        # 补播前用户重新开了监控：重启后 loop_num 从 1 重新计数，故意用同号。
        registry.register(
            registry.KIND_CLAN, 100, loop_num=3, account_name="新"
        )
        self.assertFalse(registry.is_pending(stale))
        self.assertEqual(registry.pending_exits(), [])
        # 旧登记的快照不能把新监控的登记删掉。
        registry.resolve_pending(stale)
        self.restart()
        entries = registry.pending_exits()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["account_name"], "新")

    def test_unregister_requires_matching_loop(self):
        registry.register(registry.KIND_CLAN, 100, loop_num=3)
        # 旧任务的 __aexit__ 不能删掉重启后新开监控（loop_num 不同）。
        registry.unregister(registry.KIND_CLAN, 100, 2)
        self.assertEqual(len(self.saved()), 1)
        registry.unregister(registry.KIND_CLAN, 100, 3)
        self.assertEqual(self.saved(), [])

    def test_unregister_missing_entry_is_noop(self):
        registry.unregister(registry.KIND_CLAN, 404, 1)
        self.assertEqual(self.saved(), [])

    def test_heartbeat_is_throttled(self):
        registry.register(registry.KIND_CLAN, 100, loop_num=3)
        heartbeat = self.saved()[0]["heartbeat"]
        registry.heartbeat(registry.KIND_CLAN, 100, 3, now=heartbeat + 5)
        self.assertEqual(self.saved()[0]["heartbeat"], heartbeat)
        registry.heartbeat(
            registry.KIND_CLAN, 100, 3, now=heartbeat + registry.HEARTBEAT_INTERVAL
        )
        self.assertEqual(
            self.saved()[0]["heartbeat"],
            heartbeat + registry.HEARTBEAT_INTERVAL,
        )

    def test_heartbeat_ignores_other_loop(self):
        registry.register(registry.KIND_CLAN, 100, loop_num=3)
        heartbeat = self.saved()[0]["heartbeat"]
        registry.heartbeat(registry.KIND_CLAN, 100, 4, now=heartbeat + 600)
        self.assertEqual(self.saved()[0]["heartbeat"], heartbeat)

    def test_corrupt_registry_is_ignored(self):
        self.path.write_text("{这不是 json", encoding="utf-8")
        self.restart()
        self.assertEqual(registry.pending_exits(), [])

    def test_entry_without_required_fields_is_ignored(self):
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "monitors": [
                        {"kind": "clan", "group_id": 1},
                        {"kind": "clan", "group_id": 2, "loop_num": 1,
                         "started_at": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.restart()
        entries = registry.pending_exits()
        self.assertEqual([entry["group_id"] for entry in entries], [2])


if __name__ == "__main__":
    unittest.main()
