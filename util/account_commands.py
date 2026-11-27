"""账号绑定/查看/切换默认/解绑指令的纯文本逻辑。

不依赖 bot、数据库或游戏客户端，方便单测；login.py 只负责取数据、写数据和发消息。
"""

from typing import Iterable, List, Optional, Sequence, Tuple

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

# 绑定指令末尾可以带这些词，表示把这次绑定的账号设为默认账号。
DEFAULT_FLAG_TOKENS = ("默认", "[默认]", "设为默认", "作为默认")

DEFAULT_MARKER = "[默认]"

UNBIND_USAGE = (
    "请指定要解绑的账号：可以填服务器名，也可以填【查看绑定账号】列表里的序号\n"
    "例如：解绑账号 B服 / 解绑账号 2\n"
    "同一服务器有多个账号时请用序号：解绑账号 B服 2"
)


def platform_name(platform: int) -> str:
    return PLATFORM_NAMES.get(platform, f"未知服务器({platform})")


def parse_platform(text: str) -> Optional[int]:
    """把用户输入的服务器名转成 Platform 值，认不出来返回 None。"""
    return PLATFORM_ALIASES.get((text or "").strip().lower())


def is_default_account(account) -> bool:
    return bool(getattr(account, "is_default", 0))


def account_sort_key(account) -> tuple:
    """账号列表的展示顺序：先按服务器，再按入库顺序，保证序号稳定。"""
    return (
        getattr(account, "platform", 0) or 0,
        getattr(account, "id", 0) or 0,
    )


def sort_accounts(accounts: Iterable) -> List:
    return sorted(accounts, key=account_sort_key)


def pick_default_account(accounts: Iterable):
    """返回默认账号；没有标记时退回列表里的第一个，保证旧数据也能用。"""
    accounts = sort_accounts(accounts)
    if not accounts:
        return None
    for account in accounts:
        if is_default_account(account):
            return account
    return accounts[0]


def pick_preferred_account(accounts: Iterable, platform: int, viewer_id: Optional[int] = None):
    """按服务器取一个账号：优先游戏ID匹配，其次默认账号，最后入库最早的。"""
    candidates = [item for item in sort_accounts(accounts) if item.platform == platform]
    if viewer_id is not None:
        for item in candidates:
            if item.viewer_id == viewer_id:
                return item
    return pick_default_account(candidates)


def pick_preferred_accounts(accounts: Iterable) -> List:
    """按 (QQ, 服务器) 去重，每个组合优先保留默认账号。

    WebUI 在「一个服务器一个账号」的前提下展示和操作账号，这里只做兜底去重，
    不改变原有接口形状；机器人指令仍然可以看到全部账号。
    """
    best = {}
    for account in sort_accounts(accounts):
        key = (getattr(account, "user_id", None), account.platform)
        current = best.get(key)
        if current is None or (
            is_default_account(account) and not is_default_account(current)
        ):
            best[key] = account
    return sort_accounts(best.values())


def group_default_accounts(accounts: Iterable) -> dict:
    """按 QQ 分组并取每人的默认账号，用于查询多个人的 BOX。"""
    grouped = {}
    for account in accounts:
        grouped.setdefault(getattr(account, "user_id", None), []).append(account)
    return {
        user_id: pick_default_account(items) for user_id, items in grouped.items()
    }


def split_default_flag(tokens: Sequence[str]) -> Tuple[List[str], bool]:
    """拆出绑定指令末尾的「默认」标记。"""
    tokens = [token for token in tokens if token]
    if tokens and tokens[-1] in DEFAULT_FLAG_TOKENS:
        return tokens[:-1], True
    return tokens, False


def _account_label(account) -> str:
    name = account.name or "未知昵称"
    viewer_id = account.viewer_id or "未知ID"
    return f"{platform_name(account.platform)}：{name}（游戏ID {viewer_id}）"


def format_account_line(index: int, account) -> str:
    allow = ALLOW_NAMES.get(account.allow_others or 0, "仅本人")
    text = f"{index}. {_account_label(account)}，触发权限：{allow}"
    if is_default_account(account):
        text += f" {DEFAULT_MARKER}"
    return text


def format_account_list(accounts: Iterable) -> str:
    accounts = sort_accounts(accounts)
    if not accounts:
        return "你还没有绑定账号。发送【绑定账号帮助】查看绑定方式。"
    lines = [f"你当前绑定的账号（共 {len(accounts)} 个）："]
    lines.extend(
        format_account_line(index, account)
        for index, account in enumerate(accounts, start=1)
    )
    lines.append(
        "指令默认使用带 [默认] 标记的账号，发送【切换默认账号 序号】可以更换。"
    )
    return "\n".join(lines)


def format_bind_success(account, default_account=None) -> str:
    """绑定成功提示：说明这次绑的是哪个号、当前默认账号是哪个。"""
    label = _account_label(account)
    if is_default_account(account):
        return f"绑定成功：{label}，已设为默认账号"
    if default_account is not None:
        return (
            f"绑定成功：{label}\n"
            f"当前默认账号：{_account_label(default_account)}"
            "（发送【切换默认账号 序号】可更换）"
        )
    return f"绑定成功：{label}"


def format_default_done(account) -> str:
    return f"已将默认账号切换为：{_account_label(account)} {DEFAULT_MARKER}"


def format_selector_unknown(text: str) -> str:
    return f'无法识别账号"{text}"，请发送【查看绑定账号】查看序号或服务器名。'


def handle_selector_error(accounts: Iterable, text: str) -> str:
    """选择账号失败时统一给出列表，方便用户直接挑序号。"""
    return f"{text}\n{format_account_list(accounts)}"


def _global_index(accounts: Sequence, target) -> int:
    for index, account in enumerate(accounts, start=1):
        if account is target or (
            getattr(account, "id", None) is not None
            and getattr(account, "id", None) == getattr(target, "id", None)
        ):
            return index
    return 0


def resolve_account_selector(text: str, accounts: Iterable) -> Tuple[Optional[object], Optional[str]]:
    """把「切换默认账号」的参数解析成账号：支持列表序号或唯一的服务器名。"""
    accounts = sort_accounts(accounts)
    token = (text or "").strip()
    if not token:
        return None, handle_selector_error(
            accounts, "请指定要设为默认的账号序号，例如【切换默认账号 2】。"
        )
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(accounts):
            return accounts[index - 1], None
        return None, handle_selector_error(
            accounts, f"序号 {index} 超出范围，当前共有 {len(accounts)} 个账号。"
        )
    platform = parse_platform(token)
    if platform is None:
        return None, handle_selector_error(accounts, format_selector_unknown(token))
    candidates = [account for account in accounts if account.platform == platform]
    if not candidates:
        return None, format_not_bound(platform)
    if len(candidates) == 1:
        return candidates[0], None
    return None, handle_selector_error(
        accounts,
        f"你在{platform_name(platform)}绑定了 {len(candidates)} 个账号，"
        "请用列表序号指定，例如【切换默认账号 2】。",
    )


def resolve_unbind_target(tokens: Sequence[str], accounts: Iterable) -> Tuple[Optional[object], Optional[str]]:
    """把「解绑账号」的参数解析成账号。

    支持三种写法：
    - 解绑账号 2           列表序号
    - 解绑账号 B服         该服务器只有一个账号时
    - 解绑账号 B服 2       该服务器的第 2 个账号
    """
    accounts = sort_accounts(accounts)
    tokens = [token for token in tokens if token]
    if not tokens:
        return None, UNBIND_USAGE
    first = tokens[0]
    if first.isdigit():
        if len(tokens) > 1:
            return None, UNBIND_USAGE
        index = int(first)
        if 1 <= index <= len(accounts):
            return accounts[index - 1], None
        return None, f"序号 {index} 超出范围，当前共有 {len(accounts)} 个账号。"
    platform = parse_platform(first)
    if platform is None:
        return None, format_platform_unknown(first)
    candidates = [account for account in accounts if account.platform == platform]
    if not candidates:
        return None, format_not_bound(platform)
    if len(candidates) == 1:
        return candidates[0], None
    if len(tokens) >= 2 and tokens[1].isdigit():
        index = int(tokens[1])
        if 1 <= index <= len(candidates):
            return candidates[index - 1], None
        return None, f"{platform_name(platform)}只有 {len(candidates)} 个账号。"
    lines = "\n".join(
        format_account_line(_global_index(accounts, account), account)
        for account in candidates
    )
    example = _global_index(accounts, candidates[0])
    return None, (
        f"你在{platform_name(platform)}绑定了 {len(candidates)} 个账号，请指定要解绑的那个：\n"
        f"{lines}\n"
        f"例如：解绑账号 {example} 或 解绑账号 {platform_name(platform)} 1"
    )


def format_unbind_done(account, promoted=None) -> str:
    text = f"已解绑：{_account_label(account)}"
    if promoted is not None:
        text += f"\n已将 {_account_label(promoted)} 设为默认账号。"
    text += "\n如果该账号正在用于出刀或竞技场监控，监控不会自动停止，需要手动关闭。"
    return text


def format_platform_unknown(text: str) -> str:
    return f'无法识别服务器"{text}"。可用：B服 / 渠道服 / 台服\n例如：解绑账号 B服'


def format_not_bound(platform: int) -> str:
    return f"你没有绑定 {platform_name(platform)} 账号。发送【查看绑定账号】查看已绑定账号。"
