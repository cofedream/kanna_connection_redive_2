"""合刀伤害 / 补偿刀时长计算。

两名玩家合作击破一个 BOSS 时，后出的一刀能拿到多长的补偿刀，取决于
BOSS 还剩多少血、这一刀本身能打多少伤害。参考 HoshinoBot 的
pcrclanbattle/hedao.py，这里只做纯计算，不依赖 hoshino，方便单元测试。

约定：伤害与剩余血量使用同一单位（都按原始血量，或都按“万”）；公式本身
与单位无关。补偿刀时长与 clanbattle.model.estimate_carry_seconds 的合刀分支
是同一个公式：

    击杀用时 = 90 * 击杀前剩余血量 / 后出刀总伤害
    补偿刀时长 = (90 - 击杀用时) + 20 = 110 - 90 * 剩余血量 / 后出刀伤害

20 秒是击破 BOSS 的固定奖励；上限 90 秒。游戏内返秒取整数，所以这里也按
110 - floor(90 * 剩余血量 / 后出刀伤害) 输出整数秒。
"""

import math
import re
from dataclasses import dataclass
from typing import Optional

BATTLE_SECONDS = 90
CARRY_BONUS_SECONDS = 20
MAX_CARRY_SECONDS = 90
EPSILON = 1e-9

_AMOUNT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([万wW]?)$")


class HedaoError(ValueError):
    """输入无法计算合刀收益。"""


@dataclass(frozen=True)
class HedaoOrder:
    """一种出刀顺序的计算结果。"""

    first: int  # 先出刀的编号（1/2）
    second: int  # 后出刀的编号（1/2）
    first_damage: float
    second_damage: float
    first_dealt: float
    second_dealt: float
    hp_after_first: float
    remaining_after_second: float
    first_overflow: float
    second_overflow: float
    carry_seconds: Optional[float]  # 后出刀击破时能拿到的补偿刀时长
    carry_capped: bool
    first_solo: bool  # 先出刀已经单独击破


@dataclass(frozen=True)
class HedaoResult:
    damage1: float
    damage2: float
    rest_hp: float
    killable: bool
    shortfall: float
    order_1: HedaoOrder  # 刀1先出
    order_2: HedaoOrder  # 刀2先出


def parse_amount(text: str) -> float:
    """把指令参数解析成数值，支持 50、50.5w、50万 和 12,000,000 等写法。"""
    token = text.strip().replace(",", "")
    match = _AMOUNT_RE.fullmatch(token)
    if not match:
        raise HedaoError(f"“{text}”不是有效的数字，请检查后重试")
    value = float(match.group(1))
    if match.group(2):
        value *= 10000
    if not math.isfinite(value):
        raise HedaoError(f"“{text}”超出可计算范围")
    return value


def estimate_carry_seconds(hp: float, raw_damage: float) -> int:
    """合刀返秒：110 - floor(90 * 击杀前血量 / 后出刀伤害)，上限 90 秒。

    与 clanbattle.model.estimate_carry_seconds 在 start_time == battle_time == 90
    时的合刀分支一致，只是这里的血量/伤害允许是浮点数。
    """
    elapsed = math.floor(round(BATTLE_SECONDS * hp / raw_damage, 9))
    return min(
        MAX_CARRY_SECONDS,
        BATTLE_SECONDS + CARRY_BONUS_SECONDS - elapsed,
    )


def _evaluate(
    first: int,
    second: int,
    first_damage: float,
    second_damage: float,
    rest_hp: float,
) -> HedaoOrder:
    first_dealt = min(first_damage, rest_hp)
    hp_after_first = max(rest_hp - first_dealt, 0.0)
    first_solo = hp_after_first <= EPSILON

    if first_solo:
        # 先出刀已经击破，后出刀没有出场机会。
        return HedaoOrder(
            first=first,
            second=second,
            first_damage=first_damage,
            second_damage=second_damage,
            first_dealt=first_dealt,
            second_dealt=0.0,
            hp_after_first=0.0,
            remaining_after_second=0.0,
            first_overflow=max(first_damage - first_dealt, 0.0),
            second_overflow=0.0,
            carry_seconds=None,
            carry_capped=False,
            first_solo=True,
        )

    second_dealt = min(second_damage, hp_after_first)
    remaining_after_second = max(hp_after_first - second_dealt, 0.0)
    kills = remaining_after_second <= EPSILON

    carry_seconds = None
    carry_capped = False
    if kills:
        # 后出刀击破：与监控播报的返秒使用同一个公式。
        carry_seconds = estimate_carry_seconds(hp_after_first, second_damage)
        carry_capped = carry_seconds >= MAX_CARRY_SECONDS

    return HedaoOrder(
        first=first,
        second=second,
        first_damage=first_damage,
        second_damage=second_damage,
        first_dealt=first_dealt,
        second_dealt=second_dealt,
        hp_after_first=hp_after_first,
        remaining_after_second=remaining_after_second,
        first_overflow=0.0,
        second_overflow=max(second_damage - second_dealt, 0.0),
        carry_seconds=carry_seconds,
        carry_capped=carry_capped,
        first_solo=False,
    )


def calculate_hedao(damage1: float, damage2: float, rest_hp: float) -> HedaoResult:
    """计算两种出刀顺序的伤害分配与补偿刀时长。"""
    if damage1 <= 0 or damage2 <= 0:
        raise HedaoError("两刀伤害都要大于 0")
    if rest_hp <= 0:
        raise HedaoError("BOSS 剩余血量要大于 0")

    total = damage1 + damage2
    killable = total + EPSILON >= rest_hp
    return HedaoResult(
        damage1=damage1,
        damage2=damage2,
        rest_hp=rest_hp,
        killable=killable,
        shortfall=max(rest_hp - total, 0.0),
        order_1=_evaluate(1, 2, damage1, damage2, rest_hp),
        order_2=_evaluate(2, 1, damage2, damage1, rest_hp),
    )


def _trim(value: float, digits: int = 2) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def format_amount(value: float) -> str:
    """大于等于一万时用“万”显示，避免一长串 0。"""
    if value >= 10000:
        return f"{_trim(value / 10000)}万"
    return _trim(value)


def format_seconds(value: float) -> str:
    return f"{_trim(value)}秒"


def _recommend(result: HedaoResult) -> str:
    orders = (result.order_1, result.order_2)
    valid = [order for order in orders if order.carry_seconds is not None]
    if not valid:
        return "两刀任意一刀都能单独击破 BOSS，不需要合刀"

    if len(valid) == 1:
        order = valid[0]
        return (
            f"推荐：刀{order.first}先出，刀{order.second}可获得 "
            f"{format_seconds(order.carry_seconds)}补偿刀"
            f"（若刀{order.second}先出会直接击破，刀{order.first}吃不到补偿）"
        )

    first, second = valid
    if abs(first.carry_seconds - second.carry_seconds) <= EPSILON:
        return (
            "两种出刀顺序收益相同，后出刀都能获得 "
            f"{format_seconds(first.carry_seconds)}补偿刀"
        )
    better, worse = (
        (first, second)
        if first.carry_seconds > second.carry_seconds
        else (second, first)
    )
    gap = better.carry_seconds - worse.carry_seconds
    return (
        f"推荐：刀{better.first}先出，刀{better.second}可获得 "
        f"{format_seconds(better.carry_seconds)}补偿刀"
        f"（比另一顺序多 {format_seconds(gap)}）"
    )


def format_hedao_reply(result: HedaoResult) -> str:
    head = (
        f"合刀计算：刀1 {format_amount(result.damage1)} + "
        f"刀2 {format_amount(result.damage2)}，"
        f"BOSS 剩余 {format_amount(result.rest_hp)}"
    )
    total = result.damage1 + result.damage2
    if not result.killable:
        return (
            f"{head}\n两刀合计 {format_amount(total)}，"
            f"还差 {format_amount(result.shortfall)} 伤害，打不死 BOSS"
        )

    lines = [head, f"两刀合计 {format_amount(total)}，可击破 BOSS", ""]
    for order in (result.order_1, result.order_2):
        lines.append(f"刀{order.first}先出，刀{order.second}后出：")
        if order.first_solo:
            lines.append(
                f"  刀{order.first}直接单独击破，伤害按剩余血量 "
                f"{format_amount(result.rest_hp)} 计"
                f"（溢出 {format_amount(order.first_overflow)}），"
                f"无需刀{order.second}出场"
            )
        else:
            lines.append(
                f"  刀{order.first}原始伤害 {format_amount(order.first_damage)}，"
                f"BOSS 还剩 {format_amount(order.hp_after_first)}"
            )
            if order.carry_seconds is None:
                lines.append(
                    f"  刀{order.second}原始伤害 "
                    f"{format_amount(order.second_damage)}，"
                    f"打完后 BOSS 还剩 "
                    f"{format_amount(order.remaining_after_second)}"
                )
            else:
                detail = f"计入 {format_amount(order.second_dealt)}"
                if order.second_overflow > 0:
                    detail += f"（溢出 {format_amount(order.second_overflow)}）"
                cap = "，已达上限" if order.carry_capped else ""
                lines.append(
                    f"  刀{order.second}原始伤害 "
                    f"{format_amount(order.second_damage)}，{detail}，"
                    f"补偿刀时长 {format_seconds(order.carry_seconds)}{cap}"
                )
        lines.append("")

    lines.append(_recommend(result))
    return "\n".join(lines)
