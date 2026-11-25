"""群成员显示名的解析。

官方 QQ 机器人（经 Gensokyo 等 OneBot 桥接）没有「获取群成员信息」接口权限，
get_group_member_info 会返回 retcode=0、但 card/nickname 均为空的兜底数据，
插件直接显示就会出现「监控人为：」这样的空名字（且不会抛异常，走不到 except 分支）。

这里统一提供取名字的兜底顺序：
1. 消息事件 sender 里携带的昵称（不依赖任何接口权限，收到消息时顺手缓存）；
2. get_group_member_info（非官方机器人可用，成功后会写入缓存）；
3. 调用方给的 fallback。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict

if TYPE_CHECKING:
    from hoshino.typing import CQEvent, HoshinoBot

_member_names: Dict[int, str] = {}


def sender_display_name(ev: CQEvent) -> str:
    """从消息事件 sender 取昵称：优先群名片，其次 QQ 昵称。"""
    sender = ev.get("sender") or {}
    return str(sender.get("card") or sender.get("nickname") or "").strip()


def remember_member_name(ev: CQEvent) -> str:
    """缓存消息发送者的昵称，供之后展示（状态、预约表、查树等）使用。"""
    name = sender_display_name(ev)
    user_id = ev.get("user_id")
    if name and user_id is not None:
        _member_names[int(user_id)] = name
    return name


def cached_member_name(user_id: int) -> str:
    return _member_names.get(int(user_id), "")


async def get_member_name(
    bot: HoshinoBot, group_id: int, user_id: int, fallback: str = ""
) -> str:
    """按「事件缓存 → 群成员接口 → fallback」的顺序取显示名。"""
    name = cached_member_name(user_id)
    if name:
        return name

    if group_id is not None:
        try:
            info = await bot.get_group_member_info(
                group_id=int(group_id), user_id=int(user_id)
            )
        except Exception:
            info = None
        if info:
            name = str(info.get("card") or info.get("nickname") or "").strip()
            if name:
                _member_names[int(user_id)] = name
                return name

    return fallback
