import contextlib
from re import finditer
from typing import Dict, Tuple
import httpx
import traceback
from hoshino import Service
from hoshino.typing import HoshinoBot, CQEvent, CommandSession
from .client import (
    check_client,
    decryptxml,
    decrypt_access_key,
    pcrclient,
    tw_pcrclient,
    BaseClient,
    get_access_key,
)
from .database.models import Account, RefreshAccount
from .basedata import Platform, FilePath
from nonebot import NoticeSession, get_bot, on_command, on_notice, logger, on_startup
from .database.dal import pcr_sqla
from .util.account_commands import (
    format_account_list,
    format_bind_success,
    format_default_done,
    format_not_bound,
    format_unbind_done,
    is_default_account,
    resolve_account_selector,
    resolve_unbind_target,
    split_default_flag,
)
from .util.tools import load_config, write_config
from .errorclass import AccessKeyExpiredError, NeedRefreshError, UnknowError

sv_help = """
（加好友私聊）
【绑定账号+账号+密码】加号为空格，access_key过期可自动续期
【B绑定账号+uid+access_key】加号为空格，无需帐密，凭据过期后需重新提取并重新绑定
【渠绑定账号+login_id+token】加号为空格
【台绑定账号+short_udid+udid_viewer_id】加号为空格
同一个QQ可以绑定多个账号（同一服务器也可以绑多个），绑定指令末尾加「默认」可把它设为默认账号
【查看绑定账号】查看自己绑定的所有账号，带 [默认] 的账号供指令默认使用
【切换默认账号+序号】把列表里的某个账号设为默认账号，之后所有指令默认使用它
【解绑账号+服务器或序号】立即删除绑定，如：解绑账号 B服 / 解绑账号 2
login_id和token群里有提取器
short_udid、udid和viewer_id也有提取器
B服的uid和access_key也有提取器
"""

sv = Service("你只需要好好出刀", help_=sv_help, visible=True)

run_group: Dict[int, int] = {}
client_cache: Dict[Tuple[int, int], BaseClient] = {}


def _rebind_hint(account: Account) -> str:
    """凭据失效且没有帐密可自动续期时的重新绑定提示。"""
    if account.platform == Platform.qu_id.value:
        return "登录凭据已失效（token 过期或填写有误），请重新提取 login_id 和 token 后重新绑定"
    return "登录凭据已失效（access_key 过期或填写有误），请重新提取 uid 和 access_key 后重新绑定"


async def _save_bound_account(qq_id: int, account: Account, **overrides) -> str:
    """写入绑定结果并生成提示；overrides 用于覆盖不落库的字段。"""
    saved = await pcr_sqla.add_account(
        qq_id, {**account.dict(exclude_none=True), **overrides}
    )
    if saved is None:
        saved = account
    return format_bind_success(
        saved, await pcr_sqla.query_default_account(qq_id)
    )


async def query(account: Account, is_force=False):
    player = (account.viewer_id, account.platform)
    if player in client_cache and not is_force:
        client = client_cache[player]
        if await check_client(client):
            return client
    if account.platform != Platform.tw_id.value:
        client = pcrclient(account.account, account.password, account.platform)
    else:
        client = tw_pcrclient(
            account.account,
            account.password,
            account.viewer_id,
        )
    try:
        await client.login()
    except NeedRefreshError:
        logger.warning(f"{account.viewer_id}的access_key过期，重新获取")
        bili_account = (
            await pcr_sqla.query_refresh(account.refresh)
            if account.refresh
            else None
        )
        if not bili_account:
            raise AccessKeyExpiredError(_rebind_hint(account)) from None
        uid, access_key = await get_access_key(
            bili_account.account, bili_account.password, account.user_id
        )
        account.account = str(uid)
        account.password = access_key
        await pcr_sqla.add_account(account.user_id, account.dict(exclude_none=True))
        client = pcrclient(account.account, account.password, account.platform)
        await client.login()
    if await check_client(client):
        client_cache[player] = client
        return client
    raise UnknowError("登录失败，请重试，很可能是网络问题")


@on_startup
async def init_database():
    await pcr_sqla.create_all()


@sv.on_fullmatch("绑定账号帮助", only_to_me=False)
async def send_jjchelp(bot: HoshinoBot, ev: CQEvent):
    await bot.send(ev, sv_help)
    await bot.send_private_msg(user_id=ev.user_id, message=sv_help)


@on_command("绑定账号帮助", only_to_me=False)
async def send_jjchelp_private(session: CommandSession):
    """私聊里的“绑定账号帮助”。

    Hoshino 的 Service 触发器（hoshino.msghandler）只处理群消息，私聊需要
    交给 nonebot 的命令处理器；群消息仍由上面的 sv 处理器负责，这里直接跳过。
    """
    if session.ctx.detail_type != "private":
        return
    await session.send(sv_help)


@sv.on_fullmatch("缓存运行群")
async def resatrt_remind(bot: HoshinoBot, ev: CQEvent):
    write_config(FilePath.run_group.value, run_group)
    await bot.send(ev, "成功")


@sv.on_fullmatch("提醒掉线")
async def offline_remind(bot: HoshinoBot, ev: CQEvent):
    bot = get_bot()
    for gid in (group_dict := load_config(FilePath.run_group.value)):
        with contextlib.suppress(Exception):
            await bot.send_group_msg(
                self_id=group_dict[gid],
                group_id=gid,
                message="遭遇神秘的桥本环奈偷袭，请检查监控",
            )
    write_config(FilePath.run_group.value, {})
    await bot.send(ev, "成功")


@on_command("绑定账号")
async def bind_support(session: NoticeSession):
    content, want_default = split_default_flag(
        session.ctx.message.extract_plain_text().split()
    )
    qq_id = session.ctx.user_id
    if len(content) != 3:
        await session.send(sv_help)
        return

    bili_account = RefreshAccount(account=content[1], password=content[2])
    try:
        uid, access_key = await get_access_key(
            bili_account.account, bili_account.password, qq_id
        )
        account = Account(
            user_id=qq_id,
            platform=Platform.b_id.value,
            account=uid,
            password=access_key,
            refresh=bili_account.account,
            is_default=1 if want_default else 0,
        )
        client = await query(account, True)
        if load_index := await check_client(client):
            account.viewer_id = load_index.user_info.viewer_id
            account.name = load_index.user_info.user_name
            await pcr_sqla.add_refresh(bili_account)
            await session.send(await _save_bound_account(qq_id, account))
        else:
            await session.send("绑定失败，请检查你的账号密码并重试")
    except Exception as e:
        logger.error(traceback.format_exc())
        await session.send(f"绑定失败{str(e)}")


@on_command(
    "B绑定账号",
    aliases=("b绑定账号", "B服绑定账号", "b服绑定账号"),
)
async def bind_support_b(session: NoticeSession):
    content, want_default = split_default_flag(
        session.ctx.message.extract_plain_text().split()
    )
    qq_id = session.ctx.user_id
    if len(content) != 3:
        await session.send(sv_help)
        return
    uid, access_key = content[1], content[2]
    if not uid.isdigit():
        await session.send(
            "uid 应为B站数字账号（例如 123456789）\n"
            "如果你想用B站帐密绑定，请发送【绑定账号 账号 密码】"
        )
        return
    try:
        account = Account(
            user_id=qq_id,
            platform=Platform.b_id.value,
            account=uid,
            password=access_key,
            is_default=1 if want_default else 0,
        )
        client = await query(account, True)
        if load_index := await check_client(client):
            account.viewer_id = load_index.user_info.viewer_id
            account.name = load_index.user_info.user_name
            # 非帐密绑定不保存B站帐密，清掉可能残留的 refresh，避免凭据失效后误用旧帐密续期
            await session.send(
                await _save_bound_account(qq_id, account, refresh=None)
            )
        else:
            await session.send("绑定失败，请检查你的 uid 和 access_key 是否完整并重试")
    except AccessKeyExpiredError as e:
        await session.send(str(e))
    except Exception as e:
        logger.error(traceback.format_exc())
        await session.send(f"绑定失败{str(e)}")


@on_command("渠绑定账号")
async def bind_support_qu(session: NoticeSession):
    content, want_default = split_default_flag(
        session.ctx.message.extract_plain_text().split()
    )
    qq_id = session.ctx.user_id
    if len(content) == 3:
        password = content[2]
    elif len(content) == 4:
        password = decrypt_access_key(f"{content[2]} {content[3]}")
    else:
        await session.send(sv_help)
        return
    try:
        account = Account(
            user_id=qq_id,
            platform=Platform.qu_id.value,
            account=content[1],
            password=password,
            is_default=1 if want_default else 0,
        )
        client = await query(account, True)
        if load_index := await check_client(client):
            account.viewer_id = load_index.user_info.viewer_id
            account.name = load_index.user_info.user_name
            await session.send(await _save_bound_account(qq_id, account))
        else:
            await session.send("绑定失败，请检查你的获取的数据是否完整并重试")
    except Exception as e:
        logger.error(traceback.format_exc())
        await session.send(f"绑定失败{str(e)}")


@on_command("台绑定账号")
async def bind_support_tw(session: NoticeSession):
    content, want_default = split_default_flag(
        session.ctx.message.extract_plain_text().split()
    )
    qq_id = session.ctx.user_id
    if len(content) < 4:
        await session.send(sv_help)
        return
    short_udid = content[1]
    udid = content[2]
    view_id = int(content[3])
    try:
        account = Account(
            user_id=qq_id,
            platform=Platform.tw_id.value,
            viewer_id=view_id,
            account=short_udid,
            password=udid,
            is_default=1 if want_default else 0,
        )
        client = await query(account, True)
        if load_index := await check_client(client):
            account.name = load_index.user_info.user_name
            await session.send(await _save_bound_account(qq_id, account))
        else:
            await session.send("绑定失败，请检查你的获取的数据是否完整并重试")
    except Exception as e:
        logger.error(traceback.format_exc())
        await session.send(f"绑定失败{str(e)}")


async def _account_list_text(user_id: int) -> str:
    return format_account_list(await pcr_sqla.query_account(user_id))


def _forget_client_cache(account: Account):
    """解绑后清掉登录缓存，避免后续指令继续复用旧凭据。"""
    client_cache.pop((account.viewer_id, account.platform), None)


async def _unbind_account_text(user_id: int, args: str) -> str:
    tokens = args.split()
    accounts = await pcr_sqla.query_account(user_id)
    account, error = resolve_unbind_target(tokens, accounts)
    if error:
        return error
    was_default = is_default_account(account)
    deleted = await pcr_sqla.delete_account(user_id, account.id)
    if deleted is None:
        return format_not_bound(account.platform)
    _forget_client_cache(account)
    promoted = (
        await pcr_sqla.query_default_account(user_id) if was_default else None
    )
    return format_unbind_done(account, promoted)


async def _switch_default_text(user_id: int, args: str) -> str:
    accounts = await pcr_sqla.query_account(user_id)
    if not accounts:
        return "你还没有绑定账号。发送【绑定账号帮助】查看绑定方式。"
    account, error = resolve_account_selector(args, accounts)
    if error:
        return error
    updated = await pcr_sqla.set_default_account(user_id, account.id)
    if updated is None:
        return "切换默认账号失败，请发送【查看绑定账号】确认后再试。"
    return format_default_done(account)


@sv.on_fullmatch("查看绑定账号", "查询绑定账号")
async def view_bound_account(bot: HoshinoBot, ev: CQEvent):
    await bot.send(ev, await _account_list_text(ev.user_id))


@sv.on_prefix("解绑账号", "删除绑定账号")
async def unbind_bound_account(bot: HoshinoBot, ev: CQEvent):
    await bot.send(
        ev, await _unbind_account_text(ev.user_id, ev.message.extract_plain_text())
    )


@sv.on_prefix("切换默认账号", "设定默认账号", "设为默认账号")
async def switch_default_account(bot: HoshinoBot, ev: CQEvent):
    await bot.send(
        ev, await _switch_default_text(ev.user_id, ev.message.extract_plain_text())
    )


@on_command("查看绑定账号", aliases=("查询绑定账号",))
async def view_bound_account_private(session: CommandSession):
    await session.send(await _account_list_text(session.ctx.user_id))


@on_command("解绑账号", aliases=("删除绑定账号",))
async def unbind_bound_account_private(session: CommandSession):
    args = session.current_arg_text or ""
    await session.send(await _unbind_account_text(session.ctx.user_id, args))


@on_command(
    "切换默认账号",
    aliases=("设定默认账号", "设为默认账号"),
)
async def switch_default_account_private(session: CommandSession):
    args = session.current_arg_text or ""
    await session.send(await _switch_default_text(session.ctx.user_id, args))


@on_notice("offline_file")
async def qu_bind(session: NoticeSession):
    file = session.event.file
    # 防止贱民传过大文件搞事情
    if file["size"] // 1024 // 1024 < 1 and (
        "v2.playerprefs" in file["name"] or "base.track" in file["name"]
    ):
        qq_id = session.ctx.user_id
        async with httpx.AsyncClient() as AsyncClient:
            res = await AsyncClient.get(url=file["url"])
            temp_dict = {"user_id": qq_id}

        if "bilibili.priconne" in file["name"]:
            access_key, viewer_id = decryptxml(
                res.content.decode(), Platform.qu_id.value
            )
            temp_dict["password"] = access_key
            temp_dict["viewer_id"] = viewer_id
            temp_dict["platform"] = Platform.qu_id.value
        elif "base.track" in file["name"]:
            for re in finditer(
                r'<string name="(.*)">(\d{22})</string>', res.content.decode()
            ):
                temp_dict["account"] = re.groups()[1]
                temp_dict["platform"] = Platform.qu_id.value
                break
        else:
            account, password, viewer_id, _ = decryptxml(
                res.content.decode(), Platform.tw_id.value
            )
            temp_dict["account"] = account
            temp_dict["password"] = password
            temp_dict["viewer_id"] = viewer_id
            temp_dict["platform"] = Platform.tw_id.value

        saved = await pcr_sqla.add_account(qq_id, temp_dict)
        await session.send("接受文件成功")
        if saved is None:
            saved = await pcr_sqla.query_default_account(qq_id)
        if saved is None:
            return

        client = await query(saved)
        if load_index := await check_client(client):
            await pcr_sqla.add_account(
                qq_id, {"id": saved.id, "name": load_index.user_info.user_name}
            )
            await session.send("绑定成功")
