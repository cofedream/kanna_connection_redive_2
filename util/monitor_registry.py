"""监控登记落盘：进程重启后凭它补发「监控已退出」播报。

出刀监控的存活状态原本只在内存里（``clanbattle_info`` 的 ``loop_check``），进程一
重启就彻底丢失：原来的退出播报只在轮询抛出异常时发，重启时那条路径不会执行，
新进程也没有任何「这个群之前在监控」的记录，于是监控静默消失、群里毫不知情。

这里把登记原子写盘（每次启动生成一个 token，用来识别同一次监控），启动后由
定时任务读出残留登记、补发一条退出播报，发送成功后再删除。重新开了监控的群会被
``register`` 覆盖，不会补发旧监控的退出播报。

只依赖标准库，方便不启动 Hoshino 的单元测试。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:  # 单元测试环境不装 loguru
    from loguru import logger
except ImportError:  # pragma: no cover
    import logging

    logger = logging.getLogger(__name__)


KIND_CLAN = "clan"
KIND_ARENA = "arena"

# 心跳落盘的节流间隔：监控大约每 15s 轮询一次，没必要每轮都写盘。
HEARTBEAT_INTERVAL = 30

_PATH = Path(__file__).resolve().parents[1] / "resource" / "data" / "monitors.json"

_lock = threading.RLock()
_entries: Dict[Tuple[str, int], dict] = {}
# 启动时从磁盘读到、还没补发退出播报的登记：key -> 当时的登记副本。
# 存副本而不是直接遍历 _entries，才能识别「补播前已经重新开了监控」。
_pending: Dict[Tuple[str, int], dict] = {}
_loaded = False
_dirty = False


def _key(kind: str, group_id: int) -> Tuple[str, int]:
    return str(kind), int(group_id)


def _same_monitor(first: dict, second: dict) -> bool:
    """判断两份登记是否来自同一次监控启动。

    loop_num 在重启后会从头计数、可能和旧值相同，启动时间也可能落在同一秒，
    所以优先用每次登记时生成的 token；旧格式没有 token 时退回 (loop_num, started_at)。
    """
    first_token = first.get("token")
    second_token = second.get("token")
    if first_token is not None or second_token is not None:
        return first_token == second_token
    return (
        int(first.get("loop_num", -1)) == int(second.get("loop_num", -1))
        and int(first.get("started_at", 0)) == int(second.get("started_at", 0))
    )


def _load_locked() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_PATH, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return
    except Exception as error:
        logger.warning(f"读取监控登记失败，本次按无登记处理：{error}")
        return
    if not isinstance(data, dict):
        return
    for entry in data.get("monitors") or []:
        if not isinstance(entry, dict):
            continue
        try:
            key = _key(entry["kind"], entry["group_id"])
            int(entry["loop_num"])
            int(entry["started_at"])
        except (KeyError, TypeError, ValueError):
            continue
        _entries[key] = entry
        _pending[key] = dict(entry)


def _touch_locked() -> None:
    """标记需要写盘并立即落盘（调用方需持有 ``_lock``）。"""
    global _dirty
    _dirty = True
    _flush_locked()


def _flush_locked() -> None:
    global _dirty
    if not _dirty:
        return
    payload = {"version": 1, "monitors": list(_entries.values())}
    tmp_path = _PATH.with_name(_PATH.name + ".tmp")
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=False)
        os.replace(tmp_path, _PATH)
    except Exception as error:
        # 保留 _dirty，下一次操作会重试写盘；落盘失败不影响监控本身。
        logger.warning(f"写入监控登记失败：{error}")
        return
    _dirty = False


def register(
    kind: str,
    group_id: int,
    *,
    bot_id: int = 0,
    user_id: int = 0,
    loop_num: int = 0,
    account_name: str = "",
) -> None:
    """登记一次刚启动的监控（覆盖同群旧登记）。"""
    now = int(time.time())
    key = _key(kind, group_id)
    with _lock:
        _load_locked()
        _entries[key] = {
            "kind": key[0],
            "group_id": key[1],
            "bot_id": int(bot_id or 0),
            "user_id": int(user_id or 0),
            "loop_num": int(loop_num),
            "account_name": account_name or "",
            "started_at": now,
            "heartbeat": now,
            "token": uuid.uuid4().hex,
        }
        # 重新开了监控，就不该再补发旧监控的退出播报。
        _pending.pop(key, None)
        _touch_locked()


def unregister(
    kind: str, group_id: int, loop_num: Optional[int] = None
) -> None:
    """删除登记；给了 loop_num 时只删这一次监控，避免误删新开的监控。"""
    key = _key(kind, group_id)
    with _lock:
        _load_locked()
        entry = _entries.get(key)
        if entry is None:
            _pending.pop(key, None)
            return
        if loop_num is not None and int(entry.get("loop_num", -1)) != int(loop_num):
            return
        del _entries[key]
        _pending.pop(key, None)
        _touch_locked()


def heartbeat(
    kind: str, group_id: int, loop_num: int, now: Optional[int] = None
) -> None:
    """更新存活时间（节流写盘），用于重启后估计离线时长。"""
    now = int(time.time() if now is None else now)
    key = _key(kind, group_id)
    with _lock:
        _load_locked()
        entry = _entries.get(key)
        if entry is None or int(entry.get("loop_num", -1)) != int(loop_num):
            return
        if now - int(entry.get("heartbeat", 0)) < HEARTBEAT_INTERVAL:
            return
        entry["heartbeat"] = now
        _touch_locked()


def pending_exits() -> List[dict]:
    """启动时残留、尚未补发退出播报的登记。

    当前登记已经被重新注册（不是同一次监控）的条目会被剔除。
    """
    with _lock:
        _load_locked()
        result: List[dict] = []
        for key, snapshot in list(_pending.items()):
            current = _entries.get(key)
            if current is None or not _same_monitor(current, snapshot):
                _pending.pop(key, None)
                continue
            result.append(dict(current))
        return result


def is_pending(entry: dict) -> bool:
    """``entry`` 是否仍是待补播的旧登记（发送前再确认一次，防止补播前重开监控）。"""
    key = _key(entry.get("kind", ""), entry.get("group_id", 0))
    with _lock:
        _load_locked()
        snapshot = _pending.get(key)
        current = _entries.get(key)
        return (
            snapshot is not None
            and current is not None
            and _same_monitor(snapshot, entry)
            and _same_monitor(current, entry)
        )


def resolve_pending(entry: dict) -> None:
    """补播成功后调用：清掉待补播标记并删除这一次监控的登记。"""
    key = _key(entry.get("kind", ""), entry.get("group_id", 0))
    with _lock:
        _load_locked()
        _pending.pop(key, None)
        current = _entries.get(key)
        if current is None or not _same_monitor(current, entry):
            return  # 已经被新监控覆盖，别删新登记
        del _entries[key]
        _touch_locked()


def reset_state() -> None:
    """清空内存缓存，下次访问重新读盘（单元测试用）。"""
    global _loaded, _dirty
    with _lock:
        _entries.clear()
        _pending.clear()
        _loaded = False
        _dirty = False
