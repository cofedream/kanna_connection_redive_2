"""账号查看/解绑指令的纯文本逻辑。

不依赖 bot、数据库或游戏客户端，方便单测；login.py 只负责取数据、删数据和发消息。
"""

from typing import Iterable, Optional

# 与 basedata.Platform 的取值保持一致（b_id=0 / qu_id=1 / tw_id=2）。
PLATFORM_NAMES = {
    0: "B服",
    1: "渠道服",
    2: "台服",
}

# 玩家可能怎么写服务器名；统一转小写后再查表。
PLATFORM_ALIASES = {
    "b服": 0,
    "b站": 0,
    "b站服": 0,
    "官服": 0,
    "b": 0,
    "0": 0,
    "渠道服": 1,
    "渠道": 1,
    "渠": 1,
    "qu": 1,
    "1": 1,
    "台服": 2,
    "台": 2,
    "tw": 2,
    "2": 2,
}

ALLOW_NAMES = {
    0: "仅本人",
    1: "允许管理",
    2: "任何人",
}

UNBIND_USAGE = (
    "请指定要解绑的服务器，可用：B服 / 渠道服 / 台服\n"
    "例如：解绑账号 B服（会立即删除该平台的绑定）。"
)


def platform_name(platform: int) -> str:
    return PLATFORM_NAMES.get(platform, f"未知服务器({platform})")


def parse_platform(text: str) -> Optional[int]:
    """把用户输入的服务器名转成 Platform 值，认不出来返回 None。"""
    return PLATFORM_ALIASES.get((text or "").strip().lower())


def _account_label(account) -> str:
    name = account.name or "未知昵称"
    viewer_id = account.viewer_id or "未知ID"
    return f"{platform_name(account.platform)}：{name}（游戏ID {viewer_id}）"


def format_account_list(accounts: Iterable) -> str:
    accounts = sorted(accounts, key=lambda item: item.platform)
    if not accounts:
        return "你还没有绑定账号。发送【绑定账号帮助】查看绑定方式。"
    lines = [f"你当前绑定的账号（共 {len(accounts)} 个）："]
    for index, account in enumerate(accounts, start=1):
        allow = ALLOW_NAMES.get(account.allow_others or 0, "仅本人")
        lines.append(f"{index}. {_account_label(account)}，触发权限：{allow}")
    return "\n".join(lines)


def format_unbind_done(account) -> str:
    return (
        f"已解绑：{_account_label(account)}\n"
        "如果该账号正在用于出刀或竞技场监控，监控不会自动停止，需要手动关闭。"
    )


def format_platform_unknown(text: str) -> str:
    return f'无法识别服务器"{text}"。可用：B服 / 渠道服 / 台服\n例如：解绑账号 B服'


def format_not_bound(platform: int) -> str:
    return f"你没有绑定 {platform_name(platform)} 账号。发送【查看绑定账号】查看已绑定账号。"
