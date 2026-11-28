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


class ParseHedaoArgsTests(unittest.TestCase):
    def test_argument_order_is_boss_first(self):
        result = hedao.parse_hedao_args(["46700", "36900", "40000"])
        self.assertEqual(result.rest_hp, 46700)
        self.assertEqual(result.damage1, 36900)
        self.assertEqual(result.damage2, 40000)

    def test_wrong_argument_count(self):
        with self.assertRaises(hedao.HedaoError):
            hedao.parse_hedao_args(["46700", "36900"])


class FormatTests(unittest.TestCase):
    def test_amount_format(self):
        self.assertEqual(hedao.format_amount(50), "50")
        self.assertEqual(hedao.format_amount(46700), "46700")
        self.assertEqual(hedao.format_amount(40000), "4w")
        self.assertEqual(hedao.format_amount(5000000), "500w")
        self.assertEqual(hedao.format_amount(0), "0")

    def test_reply_matches_example(self):
        reply = hedao.format_hedao_reply(
            hedao.calculate_hedao(36900, 40000, 46700)
        )
        self.assertEqual(
            reply,
            "boss血量=46700\n"
            "对boss伤害=36900 | 4w\n"
            "若[36900]先出，[4w]后出，补偿88s\n"
            "若[4w]先出，[36900]后出，补偿90s\n"
            "推荐：[4w]先出，[36900]可获90s补偿",
        )

    def test_reply_reports_shortfall(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(50, 60, 120))
        self.assertEqual(
            reply,
            "boss血量=120\n"
            "对boss伤害=50 | 60\n"
            "两刀合计110，还差10，打不死",
        )

    def test_reply_handles_solo_kill(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(200, 50, 100))
        self.assertEqual(
            reply,
            "boss血量=100\n"
            "对boss伤害=200 | 50\n"
            "若[200]先出，直接击破，无需[50]出场\n"
            "若[50]先出，[200]后出，补偿88s\n"
            "推荐：[50]先出，[200]可获88s补偿",
        )

    def test_reply_handles_both_solo_kills(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(200, 300, 100))
        self.assertIn("推荐：任意一刀都能直接击破，不需要合刀", reply)

    def test_reply_reports_tie(self):
        reply = hedao.format_hedao_reply(hedao.calculate_hedao(50, 70, 120))
        self.assertIn("推荐：两种顺序均可，后出刀补偿20s", reply)


if __name__ == "__main__":
    unittest.main()
