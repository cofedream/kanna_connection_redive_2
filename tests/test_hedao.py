"""合刀伤害/补偿刀计算的回归测试：不需要 bot，也不需要游戏账号。"""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "hedao", ROOT / "clanbattle" / "hedao.py"
)
hedao = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hedao)


class ParseAmountTests(unittest.TestCase):
    def test_plain_and_wan_units(self):
        self.assertEqual(hedao.parse_amount("50"), 50)
        self.assertEqual(hedao.parse_amount("50.5"), 50.5)
        self.assertEqual(hedao.parse_amount("50万"), 500000)
        self.assertEqual(hedao.parse_amount("50w"), 500000)
        self.assertEqual(hedao.parse_amount("1.5W"), 15000)

    def test_commas_are_ignored(self):
        self.assertEqual(hedao.parse_amount("12,000,000"), 12000000)

    def test_invalid_input(self):
        for text in ("", "abc", "50万5", "-1", "1e400"):
            with self.subTest(text=text):
                with self.assertRaises(hedao.HedaoError):
                    hedao.parse_amount(text)


class CalculateTests(unittest.TestCase):
    def test_reference_example(self):
        result = hedao.calculate_hedao(50, 60, 70)
        self.assertTrue(result.killable)
        self.assertEqual(result.shortfall, 0)
        self.assertEqual(result.order_1.carry_seconds, 80)
        self.assertEqual(result.order_2.carry_seconds, 90)
        self.assertTrue(result.order_2.carry_capped)
        self.assertEqual(result.order_1.second_dealt, 20)
        self.assertEqual(result.order_1.second_overflow, 40)
        self.assertEqual(result.order_2.second_dealt, 10)
        self.assertEqual(result.order_2.second_overflow, 40)

    def test_cannot_kill(self):
        result = hedao.calculate_hedao(50, 60, 120)
        self.assertFalse(result.killable)
        self.assertEqual(result.shortfall, 10)
        self.assertIsNone(result.order_1.carry_seconds)
        self.assertIsNone(result.order_2.carry_seconds)

    def test_exact_kill_gives_base_bonus(self):
        result = hedao.calculate_hedao(50, 50, 100)
        self.assertEqual(result.order_1.carry_seconds, 20)
        self.assertEqual(result.order_2.carry_seconds, 20)

    def test_second_blade_overkill_uses_its_full_damage(self):
        # 刀1 先出 50 -> 剩 50；刀2 总伤害 200，约 22.5 秒击杀，
        # 返秒 = 110 - floor(90 * 50 / 200) = 88，而不是把 200 截成 100。
        result = hedao.calculate_hedao(50, 200, 100)
        self.assertEqual(result.order_1.carry_seconds, 88)
        self.assertEqual(result.order_1.second_dealt, 50)
        self.assertEqual(result.order_1.second_overflow, 150)

    def test_first_blade_can_solo_kill(self):
        result = hedao.calculate_hedao(200, 50, 100)
        self.assertTrue(result.order_1.first_solo)
        self.assertIsNone(result.order_1.carry_seconds)
        self.assertFalse(result.order_2.first_solo)
        self.assertEqual(result.order_2.carry_seconds, 88)

    def test_both_blades_can_solo_kill(self):
        result = hedao.calculate_hedao(200, 300, 100)
        self.assertTrue(result.order_1.first_solo)
        self.assertTrue(result.order_2.first_solo)

    def test_carry_is_capped_at_ninety_seconds(self):
        result = hedao.calculate_hedao(1, 1000, 100)
        self.assertEqual(result.order_1.carry_seconds, 90)
        self.assertTrue(result.order_1.carry_capped)

    def test_matches_existing_model_formula(self):
        # clanbattle/model.py 的合刀返秒：110 - floor(90 * hp / raw)，上限 90。
        for hp, raw in ((20, 60), (10, 50), (50, 200), (1, 1000), (99, 100)):
            with self.subTest(hp=hp, raw=raw):
                expected = min(90, 110 - (90 * hp // raw))
                self.assertEqual(hedao.estimate_carry_seconds(hp, raw), expected)

    def test_zero_and_negative_inputs(self):
        for args in ((0, 60, 70), (50, 0, 70), (50, 60, 0), (-1, 60, 70)):
            with self.subTest(args=args):
                with self.assertRaises(hedao.HedaoError):
                    hedao.calculate_hedao(*args)


class FormatTests(unittest.TestCase):
    def test_amount_uses_wan(self):
        self.assertEqual(hedao.format_amount(50), "50")
        self.assertEqual(hedao.format_amount(5000000), "500万")
        self.assertEqual(hedao.format_amount(0), "0")

    def test_reply_recommends_the_better_order(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(50, 60, 70))
        self.assertIn("刀2先出，刀1后出", reply)
        self.assertIn("补偿刀时长 90秒，已达上限", reply)
        self.assertIn("推荐：刀2先出，刀1可获得 90秒补偿刀", reply)

    def test_reply_reports_shortfall(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(50, 60, 120))
        self.assertIn("还差 10 伤害", reply)
        self.assertIn("打不死 BOSS", reply)

    def test_reply_handles_solo_kill(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(200, 50, 100))
        self.assertIn("刀1直接单独击破", reply)
        self.assertIn("推荐：刀2先出，刀1可获得 88秒补偿刀", reply)

    def test_reply_handles_both_solo_kills(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(200, 300, 100))
        self.assertIn("不需要合刀", reply)

    def test_reply_reports_tie(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(50, 70, 120))
        self.assertIn("两种出刀顺序收益相同", reply)


if __name__ == "__main__":
    unittest.main()
