import asyncio
import re
import time
from typing import Dict

import nonebot
from hoshino import Service, message_preprocessor, priv, get_self_ids
from hoshino.typing import CQEvent, HoshinoBot
from loguru import logger

from ..basedata import NoticeType
from ..database.dal import pcr_sqla, Account, RecordDao
from ..database.models import ClanBattleKPI, NoticeCache, SLDao
from ..login import query
from ..setting import ClientSetting
from ..util import monitor_registry
from ..util.auto_boss import clan_boss_info
from ..util.decorator import check_account_qqid, check_priv_adimin
from ..util.account_commands import resolve_monitor_account
from ..util.member import format_monitor_name, get_member_name, remember_member_name
from ..util.tools import anywhere_send
from .base import (
    format_time,
    get_cbreport,
    get_plyerreport,
    get_stat,
    dao_detial,
    day_report,
    cuidao,
    get_kpireport,
    clanbattle_report,
)

from .hedao import HedaoError, calculate_hedao, format_hedao_reply, parse_amount
from .kpi import kpi_report
from .model import ClanBattle, ClanbattleItem, ClanBattlePool, PrioritizedQueryItem

help_text = """
* “+” 表示空格
【出刀监控 + （序号）】机器人登录账号监视出刀；不填序号用默认账号，如【出刀监控 2】
【催刀】栞栞谁没出满三刀
【当前战报】本期会战出刀情况
【我的战报 + 游戏名称】 栞栞个人出刀情况
【今日战报 + 游戏名称】 栞栞今日个人出刀情况
【昨日战报 + 游戏名称】 栞栞昨日个人出刀情况
【出刀详情 + 出刀编号】 栞栞你这刀怎么出的（出刀编号可以通过查看个人战报获得）
【合刀 + 刀1伤害 + 刀2伤害 + 剩余血量】 计算两刀合刀的伤害分配与补偿刀时长，如【合刀 50 60 70】
【今日出刀】今日出刀情况
【昨日出刀】昨日出刀情况
【启用肃正协议】数据出现异常使用即可清空所有数据（危险！！！）
【状态】查看当前进度
【预约表】栞栞谁预约了
【预约 + 数字 + （周目）+ （留言） 】预约boss, 周目和留言可不写，默认当前周目
【取消预约 + （数字）】取消预约
【清空预约 + （数字）】（仅）管理，清空预约
【查树】栞栞树上有几个人
【下树】寄，掉刀了
【挂树 + 数字】失误了, 寄
【sl】记录群成员SL；仅一条进刀时自动结束计时，多条时请用sl1～sl5指定Boss
【sl?】栞栞今天有没有用过sl
【申请出刀 + 数字 + （留言） 】 申请打boss，boss死亡自动清空
【取消申请】 模拟10次挂10次，老子不打了
【清空申请】（仅）管理，清空预约
【修正出刀 + 出刀编号 + (完整刀|尾刀|补偿)"】修正错误刀（出刀编号可以通过查看个人战报获得）
【会战KPI】查看等效出刀数
【kpi调整 + 游戏id + 补正】给某个玩家额外的kpi点数，可正可负
【清空kpi】删除所有kpi补正
【删除kpi+ 游戏id】删除特定补正
""".strip()

clanbattle_info: Dict[int, ClanBattle] = {}
notice_update_time: Dict[int, int] = {}
clanbattle_pool = ClanBattlePool(ClientSetting.clanbattle_max.value)

sv = Service(
    name="自动报刀2",  # 功能名
    visible=True,  # 可见性
    enable_on_default=True,  # 默认启用
    help_=help_text,  # 帮助说明
)


@message_preprocessor
async def cache_member_display_name(bot: HoshinoBot, ev: CQEvent, _):
    """官方机器人没有群成员信息接口权限，收到群消息时先把发送者昵称记下来。

    之后【状态】【预约表】【查树】等展示 QQ 昵称的地方才能不显示空名字。
    """
    try:
        if ev.get("message_type") == "group":
            remember_member_name(ev)
    except Exception:
        pass


@sv.on_fullmatch("自动报刀帮助")
async def clanbattle_help(bot: HoshinoBot, ev: CQEvent):
    await bot.send(ev, help_text)


HEDAO_HELP = (
    "请输入：合刀 刀1伤害 刀2伤害 剩余血量\n"
    "如：合刀 50 60 70\n"
    "伤害与血量请使用同一单位，支持 50、50w、50万 等写法\n"
    "补偿刀时长按 90 秒均匀出伤估算，仅供参考"
)


@sv.on_prefix("合刀")
async def hedao_calc(bot: HoshinoBot, ev: CQEvent):
    args = ev.message.extract_plain_text().strip().split()
    if len(args) != 3:
        await bot.send(ev, HEDAO_HELP)
        return
    try:
        damage1, damage2, rest_hp = (parse_amount(arg) for arg in args)
        result = calculate_hedao(damage1, damage2, rest_hp)
    except HedaoError as e:
        await bot.send(ev, str(e))
        return
    await bot.send(ev, format_hedao_reply(result))


@sv.on_prefix("出刀监控")
@check_account_qqid
async def clanbattle_monitor(
    bot: HoshinoBot, ev: CQEvent, account: Account, qq_id: int
):

    group_id = ev.group_id
    selector = ev.message.extract_plain_text().strip()
    accounts = await pcr_sqla.query_account(qq_id)
    # 不填序号用默认账号；填了序号/服务器名只影响这次监控，不改默认账号
    account, error = resolve_monitor_account(selector, accounts, account)
    if error:
        await bot.send(ev, error)
        return

    await bot.send(ev, f"正在登录账号，请耐心等待，当前监控玩家为{account.name}")

    # 初始化
    if group_id not in clanbattle_info:
        clanbattle_info[group_id] = ClanBattle(group_id)
    clan_info = clanbattle_info[group_id]
    try:
        await clan_info.init(await query(account), qq_id, ev.self_id)
    except Exception:
        try:
            await clan_info.init(await query(account, True), qq_id, ev.self_id)
        except Exception as e:
            await bot.send(
                ev, "初始化多次失败，请重绑账号或者手动上号检查是否可登录后重试"
            )
            await bot.send(ev, str(e))
            return

    loop_num = clan_info.loop_num
    # 监控人以游戏昵称为主（登录时已刷新），QQ 昵称只作括注，官方机器人拿不到群名片
    clan_info.monitor_name = remember_member_name(ev) if qq_id == ev.user_id else ""
    clan_info.account_name = clan_info.account_name or account.name or ""
    message = (
        f"开始监控中, 可以发送【取消出刀监控】或者顶号退出\n#监控编号HN000{loop_num}"
    )
    if len(accounts) > 1 and not selector:
        message += "\n当前使用默认账号，可发送【出刀监控 序号】指定其他账号，例如【出刀监控 2】"
    await bot.send(ev, message)
    await clanbattle_pool.add_task(
        PrioritizedQueryItem(data=ClanbattleItem(clan_info, loop_num))
    )


@sv.on_fullmatch("取消出刀监控")
async def delete_monitor(bot: HoshinoBot, ev: CQEvent):
    group_id = ev.group_id
    qq_id = ev.user_id
    if group_id not in clanbattle_info:
        await bot.send(ev, "本群未曾开过出刀监控")
        return
    clan_info = clanbattle_info[group_id]
    if qq_id == clan_info.user_id or priv.check_priv(ev, priv.ADMIN):
        old_loop_num = clan_info.loop_num
        clan_info.loop_num += 1
        # 先删登记，避免任务已经静默死掉时，下次重启误补播「监控已退出」；
        # 任务还活着的话下一轮会照常播报「监控已关闭」。
        monitor_registry.unregister(
            monitor_registry.KIND_CLAN, group_id, old_loop_num
        )
    else:
        await bot.send(ev, "你不是监控人或者管理")


@sv.on_fullmatch("状态")
async def daostate(bot: HoshinoBot, ev: CQEvent):
    group_id = ev.group_id
    if group_id not in clanbattle_info:
        await bot.send(ev, "未查询到本群当前进度，请开启出刀监控")
        return

    clan_info = clanbattle_info[group_id]
    now = time.time()
    msg = f"当前排名：{clan_info.rank}\n监控状态："
    if clan_info.loop_check:
        msg += "开启"
        # 游戏昵称不依赖官方机器人接口权限，QQ 昵称拿得到时才括注
        qq_name = clan_info.monitor_name or await get_member_name(
            bot, group_id, clan_info.user_id
        )
        msg += f"\n监控人为：{format_monitor_name(clan_info.account_name, qq_name)}"
        msg += "(高占用)" if now - clan_info.loop_check > 30 else ""
    else:
        msg += "关闭"
    msg += "\n" + clan_info.general_boss()
    await bot.send(ev, msg)

    msg = ""
    for i in range(1, 5 + 1):
        if apply_info := await pcr_sqla.get_notice(NoticeType.apply.value, group_id, i):
            msg += f"========={i}王=========\n"
            msg += f"当前有{len(apply_info)}人申请挑战boss\n"
            for i, info in enumerate(apply_info):
                name = await get_member_name(
                    bot, group_id, info.user_id, fallback="未知"
                )
                msg += f"->{i+1}：{name} {info.text} 已过去{format_time(now - info.time)}\n"
    if msg:
        await bot.send(ev, msg.strip())


@sv.on_rex(r"^预约\s?(\d)(\s\d+)?(\s\S*)?$")
async def subscirbe(bot: HoshinoBot, ev: CQEvent):
    match = ev["match"]
    boss = int(match.group(1))
    lap = int(match.group(2)[1:]) if match.group(2) else 0

    if boss > 5 or boss < 1:
        await bot.send(ev, "不约，滚")
        return

    text = text[1:] if (text := match.group(3)) else ""
    await pcr_sqla.add_notice(
        NoticeCache(
            group_id=ev.group_id,
            notice_type=NoticeType.subscribe.value,
            user_id=ev.user_id,
            boss=boss,
            lap=lap,
            text=text,
        )
    )
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "预约成功", at_sender=True)


@sv.on_fullmatch("预约表", only_to_me=False)
async def formsubscribe(bot: HoshinoBot, ev: CQEvent):
    group_id = ev.group_id
    FormSubscribe = ""
    subscribers = []
    for boss in range(1, 5 + 1):
        if info := await pcr_sqla.get_notice(
            NoticeType.subscribe.value, group_id, boss
        ):
            for player in info:
                lap = f"第{player.lap}周目" if player.lap else "当前周目"
                name = await get_member_name(
                    bot, ev.group_id, player.user_id, fallback="未知"
                )
                msg = (f"{name}:{player.text}" if player.text else name) + f" {lap}"
                subscribers.append(msg)
        if subscribers:
            FormSubscribe += f"\n========={boss}王=========\n" + "\n".join(subscribers)
            subscribers.clear()

    await bot.send(
        ev, f"当前预约列表{FormSubscribe}" if FormSubscribe else "无人预约呢喵"
    )


@sv.on_rex(r"^取消预约\s?(\d)$")
@check_priv_adimin()
async def cancelsubscirbe(bot: HoshinoBot, ev: CQEvent, qq_id: int):
    boss = int(ev["match"].group(1))

    if boss > 5 or boss < 1:
        await bot.send(ev, "爬爬")
        return

    await pcr_sqla.delete_notice(NoticeType.subscribe.value, ev.group_id, boss, qq_id)
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "取消成功", at_sender=True)


@sv.on_rex(r"^清空预约\s?(\d)$")
@check_priv_adimin(False)
async def cleansubscirbe(bot: HoshinoBot, ev: CQEvent, qq_id: int):

    boss = int(ev["match"].group(1))
    if boss > 5 or boss < 1:
        await bot.send(ev, "爬爬")
        return

    await pcr_sqla.delete_notice(NoticeType.subscribe.value, ev.group_id, boss)
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "清除成功", at_sender=True)


@sv.on_rex(r"^[sS][lL]\s*([1-5])?$")
async def addsl(bot: HoshinoBot, ev: CQEvent):
    clan = clanbattle_info.get(ev.group_id)
    specified = ev['match'].group(1)

    async def record_sl():
        return await pcr_sqla.add_sl(
            SLDao(group_id=ev.group_id, user_id=ev.user_id, time=int(time.time()))
        )

    if clan is None:
        added = await record_sl()
        message = 'SL已记录' if added else '今天已经SL过了'
    else:
        async with clan.entry_lock:
            active = clan.entries.active_bosses()
            count = sum(len(state['entries']) for state in clan.entries.bosses.values())
            if not specified and count > 1:
                await bot.send(ev, '当前有多条进刀，请发送 sl1～sl5 指定Boss', at_sender=True)
                return
            boss = int(specified) if specified else active[0] if active else None
            if specified and boss not in active:
                await bot.send(ev, f'{boss}王没有待结束的进刀，请核对Boss编号；SL尚未记录。', at_sender=True)
                return
            added = await record_sl()
            message = 'SL已记录' if added else '今天已经SL过了'
            if added and boss is not None:
                clan.entries.finish(boss)
                await pcr_sqla.save_battle_entries(ev.group_id, clan.entries.dump())
                clan.boss[boss - 1].fighter_num = len(clan.entries.bosses[str(boss)]['entries'])
                message += f'，已结束{boss}王最早一条进刀计时'
    if added:
        notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, message, at_sender=True)


@sv.on_fullmatch(("sl?", "SL?", "sl？", "SL？"))
async def issl(bot: HoshinoBot, ev: CQEvent):
    if await pcr_sqla.check_sl(ev.user_id, ev.group_id):
        await bot.send(ev, "今天已经SL过了", at_sender=True)
    else:
        await bot.send(ev, "今天还没有使用过SL", at_sender=True)


@sv.on_rex(r"^(上|挂)树\s?(\d)\s?(.+)?$")
async def climbtree(bot: HoshinoBot, ev: CQEvent):
    match: re.Match = ev["match"]
    boss = match[2]
    text = match[3] or " "
    await pcr_sqla.add_notice(
        NoticeCache(
            group_id=ev.group_id,
            notice_type=NoticeType.tree.value,
            user_id=ev.user_id,
            boss=boss,
            text=text,
        )
    )
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "上树成功", at_sender=True)


@sv.on_fullmatch("下树")
async def offtree(bot: HoshinoBot, ev: CQEvent):
    await pcr_sqla.delete_notice(NoticeType.tree.value, ev.group_id, user_id=ev.user_id)
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "下树成功", at_sender=True)


@sv.on_fullmatch("查树")
async def checktree(bot: HoshinoBot, ev: CQEvent):
    reply = ""
    for i in range(1, 5 + 1):
        if info := await pcr_sqla.get_notice(NoticeType.tree.value, ev.group_id, i):
            reply += f"{i}王树上目前有{len(info)}人\n"
            for i, player in enumerate(info):
                name = await get_member_name(
                    bot, ev.group_id, player.user_id, fallback="未知"
                )
                reply += f'->{i+1}：{name} {player.text} 已过去{format_time(time.time() - player.time)}\n'
    await bot.send(ev, reply or "目前树上空空如也")


@sv.on_rex(r"^申请出刀\s?(\d)\s?(\S+)?$")
async def apply(bot: HoshinoBot, ev: CQEvent):

    match: re.Match = ev["match"]
    boss = match[1]
    text = match[2] or ""
    now = time.time()
    await pcr_sqla.add_notice(
        NoticeCache(
            group_id=ev.group_id,
            notice_type=NoticeType.apply.value,
            user_id=ev.user_id,
            boss=boss,
            text=text,
        )
    )
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "申请成功", at_sender=True)
    msg = ""
    apply_info = await pcr_sqla.get_notice(
        NoticeType.apply.value, ev.group_id, int(boss)
    )
    msg += f"========={boss}王=========\n"
    msg += f"当前有{len(apply_info)}人申请挑战boss\n"
    for i, info in enumerate(apply_info):
        name = await get_member_name(bot, ev.group_id, info.user_id, fallback="未知")
        msg += f"->{i+1}：{name} {info.text} 已过去{format_time(now - info.time)}\n"
    await bot.send(ev, msg.strip())


@sv.on_prefix("取消申请")
@check_priv_adimin()
async def cancel_apply(bot: HoshinoBot, ev: CQEvent, qq_id: int):

    await pcr_sqla.delete_notice(NoticeType.apply.value, ev.group_id, user_id=qq_id)
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "取消成功", at_sender=True)


@sv.on_fullmatch("清空申请")
@check_priv_adimin(False)
async def clear_apply(bot: HoshinoBot, ev: CQEvent, qq_id: int):
    await pcr_sqla.delete_notice(NoticeType.apply.value, ev.group_id)
    notice_update_time[ev.group_id] = int(time.time())
    await bot.send(ev, "清空成功", at_sender=True)


@sv.on_fullmatch("今日出刀", "昨日出刀")
async def dao_state(bot: HoshinoBot, ev: CQEvent):
    group_id = ev.group_id
    members = {}
    if group_id not in clanbattle_info:
        await bot.send(ev, "未开启出刀监控,不显示没出刀的人")
    else:
        members = clanbattle_info[group_id].members
    data = (
        await pcr_sqla.get_day_rcords(int(time.time()), group_id)
        if "今日" in ev.raw_message
        else await pcr_sqla.get_day_rcords(int(time.time()) - 3600 * 24, group_id)
    )
    await bot.send(ev, await get_stat(await day_report(data, members)))


@sv.on_fullmatch("启用肃正协议")
async def kill_all(bot: HoshinoBot, ev: CQEvent):
    await pcr_sqla.refresh(RecordDao, -1, ev.group_id)
    await bot.send(
        ev,
        "[WARNING]肃正协议将清理一切事物（不分敌我），期间出现任何报错均为正常现象，事后请重新开启出刀监控",
    )


@sv.on_fullmatch("当前战报")
async def get_report(bot: HoshinoBot, ev: CQEvent):
    if not (data := await pcr_sqla.get_all_records(ev.group_id)):
        await bot.send(ev, "数据库为空，请确保开启出刀监控")
        return
    players, all_damage, all_score = clanbattle_report(
        data, await pcr_sqla.get_max_dao(ev.group_id)
    )
    await bot.send(ev, await get_cbreport(players, all_damage, all_score))


@sv.on_prefix("今日战报", "昨日战报", "我的战报")
async def player_report(bot: HoshinoBot, ev: CQEvent):
    name: str = ev.message.extract_plain_text().strip()
    if (preid := ev.prefix[:2]) == "今日":
        day = 0
    elif preid == "昨日":
        day = 1
    else:
        day = 5
    data = None
    if name.isdigit():
        data = await pcr_sqla.get_player_records(int(name), day, ev.group_id)
    if not data:
        members = await pcr_sqla.clanbattle_name2pcrid(ev.group_id, name)
        if not members:
            await bot.send(ev, "昵称错误")
            return
        if len(members) != 1:
            await bot.send(
                ev,
                "出现重名，请使用id查询，以下是可能id"
                + "\n".join([str(x) for x in members[:3]]),
            )
            return
        pcrid = members[0]
        data = await pcr_sqla.get_player_records(pcrid, day, ev.group_id)

    if not data:
        await bot.send(ev, "数据库为空，请确保开启出刀监控或使用正确的角色名")
        return
    await bot.send(ev, await get_plyerreport(data))


@sv.on_prefix("出刀详情")
async def single_player_report(bot: HoshinoBot, ev: CQEvent):
    if _id := ev.message.extract_plain_text().strip():
        if not _id.isdigit():
            await bot.send(ev, "请输入正确的出刀编号")
        elif info := await pcr_sqla.get_history(int(_id), ev.group_id):
            await bot.send(ev, await dao_detial(info))
        else:
            await bot.send(ev, "请检查你的出刀编号是否正确。")


@sv.on_rex(r"修正出刀\s?(\d+)\s?(完整刀|尾刀|补偿)")
async def correct_dao(bot: HoshinoBot, ev: CQEvent):
    info: re.Match = ev["match"]
    dao = info[2]
    if await pcr_sqla.correct_dao(
        int(info[1]), 0 if dao == "完整刀" else 1 if dao == "尾刀" else 0.5, ev.group_id
    ):
        await bot.send(ev, "修改成功")
    else:
        await bot.send(ev, "请检查你输入了正确的出刀编号")


@sv.on_fullmatch("催刀")
async def nei_gui(bot: HoshinoBot, ev: CQEvent):
    group_id = ev.group_id
    members = {}
    if group_id not in clanbattle_info:
        await bot.send(ev, "未开启出刀监控,不显示没出刀的人")
    else:
        members = clanbattle_info[group_id].members
    if not (data := await pcr_sqla.get_day_rcords(int(time.time()), ev.group_id)):
        await bot.send(ev, "数据库为空，请确保开启出刀监控")
        return
    await bot.send(ev, cuidao(await day_report(data, members)))


@sv.on_fullmatch("会战KPI", "会战kpi")
async def get_kpi(bot: HoshinoBot, ev: CQEvent):
    if not (data := await pcr_sqla.get_all_records(ev.group_id)):
        await bot.send(ev, "数据库为空，请确保开启出刀监控")
        return
    img = await get_kpireport(kpi_report(data, await pcr_sqla.get_kpis(ev.group_id)))
    await bot.send(ev, img)


@sv.on_prefix("kpi调整")
@check_priv_adimin(False)
async def correct_kpi(bot: HoshinoBot, ev: CQEvent, qq_id: int):
    try:
        info = ev.message.extract_plain_text().strip().split()
        await pcr_sqla.add_kpi_special(
            ClanBattleKPI(group_id=ev.group_id, pcrid=int(info[0]), bouns=int(info[1]))
        )
        await bot.send(ev, "设置成功")
    except Exception:
        await bot.send(ev, "设置失败，一定是你输入了奇怪的东西，爬爬")


@sv.on_fullmatch("清空kpi", "清空KPI")
async def clean_kpi(bot: HoshinoBot, ev: CQEvent):
    await pcr_sqla.delete_kpi(ev.group_id)
    await bot.send(ev, "清空成功")


@sv.on_prefix("删除kpi", "删除KPI")
async def del_kpi(bot: HoshinoBot, ev: CQEvent):
    await pcr_sqla.delete_kpi(ev.group_id, int(ev.message.extract_plain_text().strip()))
    await bot.send(ev, "删除成功")


@nonebot.on_startup
async def start_loop_handle():
    clanbattle_pool.init()


# 重启补播：最多等 5 分钟（覆盖 go-cqhttp 连得慢 / 发送暂时失败的情况），每 10s 重试。
# 用启动时拉起的一次性后台任务而不是 20s 一次的 scheduled_job：Hoshino 的
# scheduled_job 包装层每轮会打两条 INFO 日志，高频轮询会刷屏；补播也只在启动后需要。
_RESTART_NOTICE_TIMEOUT = 300
_RESTART_NOTICE_INTERVAL = 10
_restart_notice_tasks: set = set()


async def _announce_restart_monitor_exit():
    deadline = time.time() + _RESTART_NOTICE_TIMEOUT
    warned: set = set()
    try:
        while time.time() < deadline:
            entries = monitor_registry.pending_exits()
            if not entries:
                return
            connected = set(get_self_ids())
            if connected:
                for entry in entries:
                    if entry.get("kind") != monitor_registry.KIND_CLAN:
                        continue  # 竞技场监控暂未接入这套补播
                    if not monitor_registry.is_pending(entry):
                        continue  # 补播前已经被重新注册，别播旧监控的退出
                    group_id = int(entry["group_id"])
                    loop_num = int(entry["loop_num"])
                    silent = int(time.time()) - int(entry.get("heartbeat") or 0)
                    message = f"#编号HN000{loop_num}出刀监控已退出：机器人重启导致监控中断"
                    if silent >= 60:
                        message += f"（已中断约{format_time(silent)}）"
                    message += "，请重新发送【出刀监控】开启。"
                    if account_name := entry.get("account_name"):
                        message += f"\n监控账号：{account_name}"
                    bot_id = int(entry.get("bot_id") or 0)
                    sent = await anywhere_send(
                        message, group_id, bot_id if str(bot_id) in connected else None
                    )
                    if sent:
                        monitor_registry.resolve_pending(entry)
                        continue
                    key = (group_id, loop_num)
                    if key not in warned:
                        warned.add(key)
                        logger.warning(
                            f"重启后补发监控退出播报失败，稍后重试：群{group_id} HN000{loop_num}"
                        )
            await asyncio.sleep(_RESTART_NOTICE_INTERVAL)
    except Exception:
        # 后台任务没人 await，异常必须自己记掉，否则只会得到一条
        # “Task exception was never retrieved”。
        logger.exception("重启后补发监控退出播报异常")


@nonebot.on_startup
async def announce_restart_monitor_exit():
    """进程重启后补发「出刀监控已退出」。

    「监控退出」原本只在轮询抛异常时播报，进程被重启/杀掉时那条路径不会执行，
    新进程里也没有任何监控状态。这里读落盘的残留登记补发，发送成功后才删登记；
    CQ 还没连上或发送失败时留到下一轮重试。
    """
    task = asyncio.create_task(_announce_restart_monitor_exit())
    _restart_notice_tasks.add(task)
    task.add_done_callback(_restart_notice_tasks.discard)


@sv.scheduled_job("cron", hour="1")
async def refresh_boss_info():
    try:
        await clan_boss_info.update_boss()
    except Exception as e:
        logger.warning(f"更新boss数据异常：{e}")


@nonebot.on_startup
@sv.scheduled_job("cron", hour="8")
async def refresh_record():
    await pcr_sqla.refresh(RecordDao, 30)
