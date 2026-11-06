"""AstrBot entry point: parse videos in explicitly enabled OneBot QQ groups."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Image, Plain, Video
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .bilibili_client import BilibiliClient, BilibiliError
from .card import TEMPLATE, build_card_data, build_dynamic_card_data
from .downloader import DownloadError, cleanup_cache, download_video
from .formatter import format_video
from .group_filter import group_allowed
from .parser import VideoRef, find_message_reference
from .dynamic_client import TYPE_LABELS, parse_uid
from .subscription_service import DeliveryPreparationError, SubscriptionService
from .subscription_web import SubscriptionWeb


class BilibiliParser(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = BilibiliClient(
            comment_sort=str(config.get("comment_sort", "likes"))
        )
        self.cache_dir = (
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_bilibili_parser"
            / "videos"
        )
        self.download_slots = asyncio.Semaphore(2)
        self.cooldowns: dict[tuple[str, str, int], float] = {}
        self.cooldown_lock = asyncio.Lock()
        self.card_template = TEMPLATE.read_text(encoding="utf-8")
        self.render_slots = asyncio.Semaphore(2)
        self.subscriptions = None
        if context is not None:
            self.subscriptions = SubscriptionService(
                config,
                self.cache_dir.parent,
                self.client.http,
                self.subscription_targets,
                self.prepare_subscription_message,
                self.send_subscription_message,
            )
            self.subscription_web = SubscriptionWeb(context, self.subscriptions)

    async def initialize(self):
        if self.subscriptions:
            self.subscriptions.start()

    async def subscription_targets(self) -> list[dict]:
        targets = []
        for platform in self.context.platform_manager.platform_insts:
            if platform.meta().name != "aiocqhttp":
                continue
            bot = platform.bot
            identities = list(getattr(bot, "_wsr_api_clients", {})) or [None]
            for identity in identities:
                routing = (
                    {"self_id": int(identity)}
                    if identity and str(identity).isdigit()
                    else {}
                )
                try:
                    login = await asyncio.wait_for(
                        bot.call_action("get_login_info", **routing), 8
                    )
                    groups = await asyncio.wait_for(
                        bot.call_action("get_group_list", **routing), 8
                    )
                    for group in groups:
                        group_id = str(group["group_id"])
                        if self._allowed(group_id):
                            targets.append(
                                {
                                    "platform": platform.meta().id,
                                    "bot_id": str(login["user_id"]),
                                    "group_id": group_id,
                                    "group_name": str(
                                        group.get("group_name") or group_id
                                    ),
                                }
                            )
                except Exception:
                    logger.warning(
                        "Could not list subscription targets for %s", platform.meta().id
                    )
        return targets

    async def prepare_subscription_message(self, item: dict, stage: str, test: bool):
        if stage == "video":
            async with self.download_slots:
                await asyncio.to_thread(
                    cleanup_cache,
                    self.cache_dir,
                    int(self.config.get("cache_hours", 2)),
                )
                path = await download_video(
                    VideoRef(item["bvid"]),
                    self.cache_dir,
                    int(self.config.get("max_video_mb", 200)),
                )
            return MessageChain([Video.fromFileSystem(str(path))])
        fallback = (
            ("【测试推送】\n" if test else "")
            + item["author"]
            + (" 直播通知\n" if item["kind"].startswith("live_") else " 发布了新动态\n")
            + item["title"]
            + "\n"
            + item["text"][:1600]
            + "\n"
            + item["url"]
        )
        try:
            if item["kind"] == "video" and item.get("bvid"):
                info = await self.client.get_video(VideoRef(item["bvid"]))
                data = await build_card_data(info, self.client.http)
                data["is_test"] = test
            else:
                data = await build_dynamic_card_data(item, self.client.http, test)
            if self.config.get("render_card", True):
                async with self.render_slots:
                    path = await asyncio.wait_for(
                        self.html_render(
                            self.card_template,
                            data,
                            return_url=False,
                            options={"type": "jpeg", "quality": 92, "full_page": True},
                        ),
                        45,
                    )
                return MessageChain([Image.fromFileSystem(str(path))])
        except Exception:
            logger.warning("Dynamic card unavailable; using text fallback")
        return MessageChain([Plain(fallback)])

    async def send_subscription_message(self, sub: dict, message: MessageChain):
        from aiocqhttp.exceptions import ActionFailed
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
            AiocqhttpMessageEvent,
        )

        platform = next(
            (
                p
                for p in self.context.platform_manager.platform_insts
                if p.meta().id == sub["platform"] and p.meta().name == "aiocqhttp"
            ),
            None,
        )
        if not platform:
            raise DeliveryPreparationError("机器人实例未连接")
        routing = {"self_id": int(sub["bot_id"])}
        try:
            login = await asyncio.wait_for(
                platform.bot.call_action("get_login_info", **routing), 8
            )
            if str(login.get("user_id")) != sub["bot_id"]:
                raise DeliveryPreparationError("机器人账号已改变，请重新绑定订阅")
            parts = await AiocqhttpMessageEvent._parse_onebot_json(message)
        except Exception as exc:
            raise DeliveryPreparationError(
                "发送前检查失败，请检查机器人连接和文件访问"
            ) from exc
        try:
            result = await asyncio.wait_for(
                platform.bot.call_action(
                    "send_group_msg",
                    group_id=int(sub["group_id"]),
                    message=parts,
                    **routing,
                ),
                180,
            )
        except ActionFailed as exc:
            raise DeliveryPreparationError(
                "NapCat 明确拒绝发送，请检查群权限或文件访问"
            ) from exc
        if not isinstance(result, dict) or not result.get("message_id"):
            raise RuntimeError("发送回执缺少消息 ID，请核对群记录")
        return str(result["message_id"])

    @filter.command("bili")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def bili_command(self, event: AstrMessageEvent, args: GreedyStr):
        """Manage the current QQ group's persistent dynamic subscriptions."""
        if not self.subscriptions or str(event.get_sender_id()) == str(
            event.message_obj.self_id
        ):
            return
        service = self.subscriptions
        target = {
            "platform": event.platform_meta.id,
            "bot_id": str(event.message_obj.self_id),
            "group_id": str(event.get_group_id()),
            "group_name": str(event.get_group_id()),
        }
        parts = str(args).split()
        if (
            len(parts) >= 2
            and parts[0] == "直播"
            and parts[1] in {"订阅", "通知", "记录"}
        ):
            parts = ["直播" + parts[1], *parts[2:]]
        action = parts[0] if parts else "帮助"
        mentions = list(
            dict.fromkeys(
                str(component.qq)
                for component in event.message_obj.message
                if isinstance(component, At)
                and str(component.qq).isdigit()
                and str(component.qq) != target["bot_id"]
            )
        )
        actor = f"qq:{event.get_sender_id()}:{target['group_id']}"
        try:
            if action == "帮助":
                result = "Bilibili 动态订阅\nbili 订阅 <UID/主页> [全部/视频/图文等]\nbili 列表 / 状态\nbili 取消/暂停/恢复 <UID>\nbili 设置 <UID> 类型 视频,图文\nbili 设置 <UID> 视频 开/关\nbili 检查/测试 <UID>\nbili 暂停全部 / 恢复全部\n修改需群主、群管理员或机器人管理员权限。\n动态总开关在插件 Web 页面开启。"
                result += "\n直播：\nbili 直播订阅 <UID>\nbili 直播通知 <UID> 开/关\nbili 直播记录 <UID> 开/关\nbili 直播通知 开/关（切换本群全部主播）\nbili 绑定 <UID> @群成员（管理员设置）\nbili 解绑 @群成员\nbili 直播记录 <UID 或 @群成员> [YYYY-MM]（查看月历）\n@查询需管理员先绑定本群成员与已订阅主播。直播总开关在 Web 设置开启，记录保留 30 天。"
            elif not self._allowed(target["group_id"]):
                result = "本群不在允许范围，请由机器人管理员在 Web 修改群过滤。"
            elif action == "直播记录" and (
                len(parts) < 3 or parts[2] not in {"开", "关"}
            ):
                if mentions:
                    if len(mentions) != 1:
                        raise ValueError("请只 @ 一位群成员")
                    uid = service.store.bound_uid(target, mentions[0])
                    if not uid:
                        raise ValueError(
                            "该群成员尚未绑定主播。请管理员先发送：/bili 绑定 <UID> @群成员"
                        )
                else:
                    if len(parts) < 2:
                        raise ValueError(
                            "用法：/bili 直播记录 <UID 或 @群成员> [YYYY-MM]"
                        )
                    uid = parse_uid(parts[1])
                sub = next(
                    (s for s in service.store.subscriptions(target) if s["uid"] == uid),
                    None,
                )
                if not sub:
                    raise ValueError("本群未订阅这个 UID")
                data = service.live.history(uid)
                if mentions:
                    choices = [
                        part
                        for part in parts[1:]
                        if re.fullmatch(r"\d{4}-\d{2}|[1-3]", part)
                    ]
                    selected = choices[0] if choices else "1"
                else:
                    selected = parts[2] if len(parts) > 2 else "1"
                calendars = data["calendars"]
                if selected.isdigit() and 1 <= int(selected) <= len(calendars):
                    month = calendars[int(selected) - 1]
                else:
                    month = next((c for c in calendars if c["month"] == selected), None)
                if month is None:
                    raise ValueError(
                        "可用月份：" + "、".join(c["month"] for c in calendars)
                    )
                data.update(name=sub["name"], calendar=month)
                session_ids = {
                    event["id"]
                    for day in month["days"]
                    for event in day.get("events", [])
                }
                data["rows"] = [r for r in data["rows"] if r["id"] in session_ids][:10]
                try:
                    async with self.render_slots:
                        path = await asyncio.wait_for(
                            self.html_render(
                                (
                                    Path(__file__).parent
                                    / "templates"
                                    / "live_history.html"
                                ).read_text("utf-8"),
                                data,
                                return_url=False,
                                options={"type": "png", "full_page": True},
                            ),
                            45,
                        )
                    yield event.chain_result([Image.fromFileSystem(str(path))])
                    event.stop_event()
                    return
                except Exception:
                    result = (
                        f"{month['label']} · 约 {month['total']}\n日历渲染暂不可用，可在 Web 导出日历；以下最多展示 10 场。\n"
                        + "\n".join(
                            f"{r['title']}\n{r['started']} → {r['ended']} · 约 {r['duration']}"
                            for r in data["rows"]
                        )
                    )
            elif action in {"列表", "状态"}:
                rows = service.store.subscriptions(target)
                lines = [
                    "动态总开关："
                    + ("开" if service.config.get("dynamic_enabled", False) else "关")
                ]
                lines.append(
                    "直播总开关："
                    + ("开" if service.config.get("live_enabled", False) else "关")
                )
                for sub in rows:
                    state = (
                        "暂停"
                        if not sub["enabled"]
                        else "受限：" + sub["error"]
                        if sub["error"]
                        else "运行中"
                        if sub["initialized"]
                        else "等待初始化"
                    )
                    lines.append(
                        f"{sub['name']} ({sub['uid']}) · {state}\n类型：{','.join(TYPE_LABELS[t] for t in sub['types'])} · 视频：{'开' if sub['send_video'] else '关'}"
                    )
                    lines.append(
                        f"直播通知：{'开' if sub['live_notify'] else '关'} · 直播记录：{'开' if sub['live_record'] else '关'}"
                    )
                    if action == "状态":
                        stamp = (
                            time.strftime("%m-%d %H:%M", time.localtime(sub["success"]))
                            if sub["success"]
                            else "尚未成功"
                        )
                        lines.append(
                            "最近成功检查："
                            + stamp
                            + (" · 有分页缺口" if sub["gap"] else "")
                        )
                result = "\n".join(lines) if rows else lines[0] + "\n本群暂无订阅。"
                if action == "状态":
                    jobs = service.store.rows(
                        "SELECT status,count(*) n FROM deliveries WHERE platform=? AND bot_id=? AND group_id=? GROUP BY status",
                        (target["platform"], target["bot_id"], target["group_id"]),
                    )
                    result += "\n发送记录：" + ", ".join(
                        f"{r['status']}={r['n']}" for r in jobs
                    )
            else:
                permitted = event.is_admin()
                if not permitted:
                    try:
                        member = await asyncio.wait_for(
                            event.bot.call_action(
                                "get_group_member_info",
                                group_id=int(target["group_id"]),
                                user_id=int(event.get_sender_id()),
                                self_id=int(target["bot_id"]),
                                no_cache=True,
                            ),
                            8,
                        )
                        permitted = member.get("role") in {"owner", "admin"}
                    except Exception:
                        permitted = False
                if not permitted:
                    raise ValueError(
                        "只有本群群主、群管理员或机器人管理员可以修改和测试订阅"
                    )
                if action in {"绑定", "解绑"}:
                    if len(mentions) != 1:
                        raise ValueError(
                            f"用法：/bili {action} {'<UID> ' if action == '绑定' else ''}@一位群成员"
                        )
                    member_id = mentions[0]
                    try:
                        member = await asyncio.wait_for(
                            event.bot.call_action(
                                "get_group_member_info",
                                group_id=int(target["group_id"]),
                                user_id=int(member_id),
                                self_id=int(target["bot_id"]),
                                no_cache=True,
                            ),
                            8,
                        )
                        if not isinstance(member, dict):
                            raise ValueError("群成员不存在")
                    except Exception as exc:
                        raise ValueError("无法确认被 @ 的成员仍在本群") from exc
                    if action == "绑定":
                        # The QQ mention is a message component; the UID must come from plain text.
                        plain = " ".join(
                            component.text
                            for component in event.message_obj.message
                            if isinstance(component, Plain)
                        )
                        candidates = re.findall(r"(?<!\d)[1-9]\d{0,19}(?!\d)", plain)
                        if len(candidates) != 1:
                            raise ValueError("用法：/bili 绑定 <主播UID> @群成员")
                        uid = parse_uid(candidates[0])
                        service.store.bind_qq(target, member_id, uid, actor)
                        result = f"已将本群成员 QQ {member_id} 关联到主播 UID {uid}。现在可使用 /bili 直播记录 @该成员。"
                    else:
                        removed = service.store.unbind_qq(target, member_id, actor)
                        result = (
                            "已解除本群成员的主播关联。"
                            if removed
                            else "该成员在本群没有主播关联。"
                        )
                elif action == "直播订阅":
                    if len(parts) != 2:
                        raise ValueError("用法：bili 直播订阅 <UID>")
                    added = await service.add(
                        target,
                        parts[1],
                        [],
                        False,
                        actor,
                        live_notify=True,
                        live_record=True,
                    )
                    if not added["created"]:
                        sub = service.store.get(added["id"])
                        service.edit(
                            sub["id"],
                            sub["revision"],
                            {"live_notify": True, "live_record": True},
                            actor,
                        )
                    result = (
                        "已开启本群直播通知和记录："
                        + added["name"]
                        + "\n请确认 Web 直播总开关已开启；首次检测正在直播时会通知。"
                    )
                elif (
                    action == "直播通知"
                    and len(parts) == 2
                    and parts[1] in {"开", "关"}
                ):
                    rows = service.store.subscriptions(target)
                    for sub in rows:
                        service.edit(
                            sub["id"],
                            sub["revision"],
                            {"live_notify": parts[1] == "开"},
                            actor,
                        )
                    result = f"本群 {len(rows)} 个主播的直播通知已{'开启' if parts[1] == '开' else '关闭'}。"
                elif action == "订阅":
                    if len(parts) < 2:
                        raise ValueError(
                            "用法：bili 订阅 <UID/主页地址> [全部/视频/图文]"
                        )
                    types = "all" if len(parts) < 3 or parts[2] == "全部" else parts[2]
                    added = await service.add(target, parts[1], types, True, actor)
                    result = (
                        ("已订阅：" if added["created"] else "本群已订阅：")
                        + added["name"]
                        + "\n首次初始化不补发历史。"
                        + (
                            ""
                            if service.config.get("dynamic_enabled", False)
                            else "\n请在 Web 打开动态总开关。"
                        )
                    )
                elif action in {"暂停全部", "恢复全部"}:
                    rows = service.store.subscriptions(target)
                    for sub in rows:
                        service.edit(
                            sub["id"],
                            sub["revision"],
                            {"enabled": action == "恢复全部"},
                            actor,
                        )
                    result = f"已{action}：{len(rows)} 条订阅。"
                else:
                    if len(parts) < 2:
                        raise ValueError("请提供 UID，输入 bili 帮助 查看指令")
                    uid = parse_uid(parts[1])
                    sub = next(
                        (
                            row
                            for row in service.store.subscriptions(target)
                            if row["uid"] == uid
                        ),
                        None,
                    )
                    if not sub:
                        raise ValueError("本群未订阅这个 UID")
                    if action in {"直播通知", "直播记录"}:
                        if len(parts) != 3 or parts[2] not in {"开", "关"}:
                            raise ValueError(f"用法：bili {action} <UID> 开/关")
                        key = "live_notify" if action == "直播通知" else "live_record"
                        service.edit(
                            sub["id"], sub["revision"], {key: parts[2] == "开"}, actor
                        )
                        result = f"{sub['name']} 的{action}已{'开启' if parts[2] == '开' else '关闭'}。"
                    elif action == "取消":
                        service.store.remove(sub["id"], sub["revision"], actor)
                        result = "已取消本群订阅：" + sub["name"]
                    elif action in {"暂停", "恢复"}:
                        service.edit(
                            sub["id"],
                            sub["revision"],
                            {"enabled": action == "恢复"},
                            actor,
                        )
                        result = f"已{action}：{sub['name']}"
                    elif action in {"检查", "测试"}:
                        task = service.request(
                            sub["id"], "check" if action == "检查" else "test", actor
                        )
                        result = f"已安排{action}任务 #{task}，结果可在 Web 查看。"
                    elif action == "设置" and len(parts) >= 4:
                        if parts[2] == "类型":
                            changes = {
                                "types": "all" if parts[3] == "全部" else parts[3]
                            }
                        elif parts[2] == "视频" and parts[3] in {"开", "关"}:
                            changes = {"send_video": parts[3] == "开"}
                        else:
                            raise ValueError(
                                "用法：bili 设置 <UID> 类型 视频,图文 或 视频 开/关"
                            )
                        service.edit(sub["id"], sub["revision"], changes, actor)
                        result = "订阅设置已保存。"
                    else:
                        raise ValueError("未知指令，请输入 bili 帮助")
        except (ValueError, BilibiliError) as exc:
            result = str(exc)
        yield event.plain_result(result)
        event.stop_event()

    def _allowed(self, group_id: str) -> bool:
        return group_allowed(group_id, self.config)

    async def _in_cooldown(self, group_id: str, ref: VideoRef) -> bool:
        key = (group_id, ref.video_id, ref.page)
        now = time.monotonic()
        seconds = max(0, int(self.config.get("cooldown_seconds", 60)))
        async with self.cooldown_lock:
            self.cooldowns = {
                item: stamp
                for item, stamp in self.cooldowns.items()
                if now - stamp < max(seconds, 60)
            }
            if now - self.cooldowns.get(key, -1e12) < seconds:
                return True
            self.cooldowns[key] = now
            return False

    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """Parse and send Bilibili videos in explicitly enabled QQ groups."""
        group_id = str(event.message_obj.group_id or "")
        if not self._allowed(group_id) or str(event.get_sender_id()) == str(
            event.message_obj.self_id
        ):
            return
        found = find_message_reference(
            event.message_str or "",
            event.message_obj.message,
            event.message_obj.raw_message,
            bool(self.config.get("parse_share_cards", True)),
        )
        if found is None:
            return
        try:
            ref = (
                await self.client.resolve_short_url(found)
                if isinstance(found, str)
                else found
            )
            if await self._in_cooldown(group_id, ref):
                return
            info = await self.client.get_video(ref)
        except BilibiliError as exc:
            logger.warning("Bilibili metadata lookup failed: %s", exc)
            yield event.plain_result(f"Bilibili 视频解析失败：{exc}")
            return
        except Exception:
            logger.exception("Unexpected Bilibili metadata error")
            yield event.plain_result("Bilibili 视频解析失败，请稍后重试。")
            return

        card_path = None
        if self.config.get("render_card", True):
            try:
                async with self.render_slots:
                    data = await build_card_data(info, self.client.http)
                    card_path = await asyncio.wait_for(
                        self.html_render(
                            self.card_template,
                            data,
                            return_url=False,
                            options={"type": "jpeg", "quality": 92, "full_page": True},
                        ),
                        timeout=45,
                    )
            except Exception:
                logger.exception("Native card rendering failed; using text fallback")
        if card_path:
            yield event.image_result(card_path)
        else:
            yield event.plain_result(format_video(info))
            if self.config.get("send_cover_image", True):
                cover = urlparse(info.cover_url)
                if cover.scheme == "https" and (cover.hostname or "").endswith(
                    ".hdslb.com"
                ):
                    yield event.image_result(info.cover_url)

        if not self.config.get("send_video", True):
            return

        try:
            async with self.download_slots:
                await asyncio.to_thread(
                    cleanup_cache,
                    self.cache_dir,
                    int(self.config.get("cache_hours", 2)),
                )
                path = await download_video(
                    info.ref, self.cache_dir, int(self.config.get("max_video_mb", 200))
                )
            yield event.chain_result([Video.fromFileSystem(str(path))])
        except DownloadError as exc:
            logger.warning("Bilibili download failed: %s", exc)
            yield event.plain_result(f"视频文件未能发送：{exc}。图文信息已发送。")
        except Exception:
            logger.exception("Unexpected Bilibili download error")
            yield event.plain_result("视频文件未能发送，请稍后重试。图文信息已发送。")

    async def terminate(self):
        if self.subscriptions:
            await self.subscriptions.close()
        await self.client.close()
