"""Shared QQ/Web subscription operations and durable background workers."""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from .dynamic_client import DynamicClient, TYPE_LABELS, parse_uid
from .downloader import DownloadError
from .group_filter import group_allowed
from .subscription_store import SubscriptionStore
from .live import LiveTracker


class DeliveryPreparationError(Exception):
    """A failure before a send, which is safe to retry."""


class SubscriptionService:
    def __init__(self, config, data_dir: Path, http, targets, prepare, send):
        self.config = config
        self.data_dir = data_dir
        self.store = SubscriptionStore(data_dir / "subscriptions.sqlite3")
        self.credentials_path = data_dir / "credentials.json"
        self.cookie = ""
        if self.credentials_path.exists():
            self.cookie = str(
                json.loads(self.credentials_path.read_text("utf-8")).get("cookie", "")
            )
        self.client = DynamicClient(http, lambda: self.cookie)
        self.targets = targets
        self.prepare = prepare
        self.send = send
        self.workers = []
        self.wake = asyncio.Event()
        self.stopping = False
        self.worker_error = ""
        self.delivery_error = ""
        self.last_cleanup = 0.0
        self.live = LiveTracker(self, http)

    def live_allowed(self, sub):
        return bool(self.config.get("live_enabled", False)) and group_allowed(
            sub["group_id"], self.config
        )

    def accepts(self, sub, item):
        if item["kind"] in {"live_start", "live_end"}:
            if item["kind"] == "live_start":
                session_id = item["id"].split(":")[2]
                if not self.store.rows(
                    "SELECT id FROM live_sessions WHERE id=? AND ended IS NULL",
                    (session_id,),
                ):
                    return False
            return bool(
                sub["enabled"] and sub["live_notify"] and self.live_allowed(sub)
            )
        return bool(
            sub["enabled"] and self.allowed(sub) and item["kind"] in sub["types"]
        )

    def allowed(self, sub: dict) -> bool:
        return bool(self.config.get("dynamic_enabled", False)) and group_allowed(
            sub["group_id"], self.config
        )

    def start(self):
        if not self.workers:
            self.workers = [
                asyncio.create_task(self.collect_loop()),
                asyncio.create_task(self.delivery_loop()),
                asyncio.create_task(self.live.loop()),
            ]

    async def close(self):
        self.stopping = True
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        self.store.close()

    async def add(
        self,
        target: dict,
        value: str,
        types,
        video: bool,
        actor: str,
        live_notify=False,
        live_record=False,
    ):
        uid = parse_uid(value)
        types = self.validate_types(types)
        if not isinstance(live_notify, bool) or not isinstance(live_record, bool):
            raise ValueError("直播开关必须为布尔值")
        if not types and not (live_notify or live_record):
            raise ValueError("请至少选择一种动态类型、直播通知或直播记录")
        if not isinstance(video, bool):
            raise ValueError("视频开关必须为布尔值")
        if not group_allowed(str(target.get("group_id", "")), self.config):
            raise ValueError("此群不在插件允许范围，请先由管理员修改群过滤设置")
        valid = await self.targets()
        match = next(
            (
                row
                for row in valid
                if all(
                    str(row[key]) == str(target.get(key))
                    for key in ("platform", "bot_id", "group_id")
                )
            ),
            None,
        )
        if not match:
            raise ValueError("目标群不可访问或机器人账号已改变，请刷新群列表")
        existing = next(
            (row for row in self.store.subscriptions(match) if row["uid"] == uid), None
        )
        if existing:
            return {"id": existing["id"], "created": False, "name": existing["name"]}
        creator = await self.client.creator(uid)
        identity, created = self.store.add(match, creator, types, video, actor)
        if created and (live_notify or live_record):
            self.store.edit(
                identity,
                1,
                {"live_notify": live_notify, "live_record": live_record},
                actor,
            )
        self.wake.set()
        return {"id": identity, "created": created, "name": creator["name"]}

    @staticmethod
    def validate_types(types) -> list[str]:
        if types == "all":
            return list(TYPE_LABELS)
        if isinstance(types, str):
            types = types.replace("，", ",").split(",")
        aliases = {label: key for key, label in TYPE_LABELS.items()}
        if not isinstance(types, list):
            raise ValueError("请至少选择一种动态类型")
        result = list(
            dict.fromkeys(aliases.get(str(value), str(value)) for value in types)
        )
        if any(value not in TYPE_LABELS for value in result):
            raise ValueError("类型支持：视频、图文、文字、转发、专栏、其他")
        return result

    def edit(self, identity: int, revision: int, changes: dict, actor: str):
        changes = {
            k: v
            for k, v in changes.items()
            if k in {"types", "enabled", "send_video", "live_notify", "live_record"}
        }
        for key in ("enabled", "send_video", "live_notify", "live_record"):
            if key in changes and not isinstance(changes[key], bool):
                raise ValueError("开关必须为布尔值")
        if "types" in changes:
            changes["types"] = self.validate_types(changes["types"])
        self.store.edit(identity, revision, changes, actor)
        self.wake.set()

    def request(self, identity: int, kind: str, actor: str):
        sub = self.store.get(identity)
        if not sub["types"]:
            raise ValueError(
                "此订阅仅启用了直播；直播将按检查间隔自动采集，动态检查/测试需选择动态类型"
            )
        if not sub["enabled"] or not self.allowed(sub):
            raise ValueError("请先开启动态总开关、允许此群并恢复该订阅")
        if kind not in {"check", "test"}:
            raise ValueError("不支持的操作")
        task = self.store.request(sub, kind, actor)
        self.wake.set()
        return task

    def settings(self) -> dict:
        keys = {
            "dynamic_enabled": False,
            "dynamic_interval": 180,
            "live_enabled": False,
            "live_interval": 60,
            "group_filter_mode": "whitelist",
            "allowed_group_ids": "",
            "blocked_group_ids": "",
            "max_video_mb": 200,
            "cache_hours": 2,
        }
        return {
            **{key: self.config.get(key, default) for key, default in keys.items()},
            "cookie_configured": bool(self.cookie),
        }

    def save_settings(self, values: dict, actor: str):
        current = self.settings()
        changes = {
            key: value
            for key, value in values.items()
            if key in current and key != "cookie_configured"
        }
        merged = {**current, **changes}
        if (
            not isinstance(merged["live_enabled"], bool)
            or not isinstance(merged["dynamic_enabled"], bool)
            or merged["group_filter_mode"] not in {"whitelist", "blacklist", "all"}
        ):
            raise ValueError("开关或群过滤模式不正确")
        for key, low, high in (
            ("dynamic_interval", 60, 86400),
            ("live_interval", 30, 3600),
            ("max_video_mb", 1, 2048),
            ("cache_hours", 1, 168),
        ):
            if type(merged[key]) is not int or not low <= merged[key] <= high:
                raise ValueError(f"{key} 必须是 {low} 到 {high} 的整数")
        for key in ("allowed_group_ids", "blocked_group_ids"):
            if not isinstance(merged[key], str) or len(merged[key]) > 20000:
                raise ValueError("群列表格式错误")
        if "cookie" in values:
            cookie = values["cookie"]
            if (
                not isinstance(cookie, str)
                or len(cookie) > 12000
                or "\n" in cookie
                or "\r" in cookie
            ):
                raise ValueError("Cookie 格式错误")
            tmp = self.credentials_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"cookie": cookie.strip()}), "utf-8")
            tmp.chmod(0o600)
            tmp.replace(self.credentials_path)
            self.cookie = cookie.strip()
        self.config.update(changes)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        self.store.sync_allowed(self.allowed)
        with self.store.db:
            self.store.db.execute("UPDATE creators SET next_check=0")
            self.store.db.execute("UPDATE live_state SET next_check=0")
            self.store.audit(actor, "settings", ",".join(values.keys()))
        self.wake.set()

    def snapshot(self) -> dict:
        subs = self.store.subscriptions()
        for row in subs:
            live_state = self.store.rows(
                "SELECT * FROM live_state WHERE uid=?", (row["uid"],)
            )
            row["live_state"] = live_state[0] if live_state else {}
            row["live_allowed"] = self.live_allowed(row)
            row["state"] = (
                "paused"
                if not row["enabled"]
                else "blocked"
                if not self.allowed(row)
                else "error"
                if row["error"]
                else "initializing"
                if not row["initialized"]
                else "active"
            )
            if not row["types"]:
                row["state"] = (
                    "paused"
                    if not row["enabled"]
                    else "blocked"
                    if not self.live_allowed(row)
                    else "error"
                    if row["live_state"].get("error")
                    else "active"
                    if row["live_state"].get("checked")
                    else "initializing"
                )
                row["error"] = row["live_state"].get("error", "")
                row["success"] = row["live_state"].get("checked", 0)
        counts = {
            row["status"]: row["n"]
            for row in self.store.rows(
                "SELECT status,count(*) n FROM deliveries GROUP BY status"
            )
        }
        return {
            "subscriptions": subs,
            "counts": counts,
            "creators": len({row["uid"] for row in subs}),
            "enabled": bool(self.config.get("dynamic_enabled", False)),
            "worker_error": " · ".join(
                filter(None, (self.worker_error, self.delivery_error, self.live.error))
            ),
            "requests": self.store.rows(
                "SELECT r.*,s.uid FROM requests r LEFT JOIN subscriptions s ON s.id=r.subscription_id ORDER BY r.id DESC LIMIT 30"
            ),
        }

    async def poll(self, uid: str):
        active = [
            row
            for row in self.store.subscriptions()
            if row["uid"] == uid
            and row["enabled"]
            and row["types"]
            and self.allowed(row)
        ]
        if not active:
            return
        baseline_only = not any(row["initialized"] for row in active)
        items, gap = await self.client.feed(
            uid, self.store.known(uid), first_page_only=baseline_only
        )
        interval = max(60, int(self.config.get("dynamic_interval", 180)))
        self.store.sync_allowed(self.allowed)
        self.store.ingest(
            uid, items, gap, time.time() + interval * random.uniform(0.8, 1.2)
        )

    async def collect_loop(self):
        while not self.stopping:
            try:
                if time.monotonic() - self.last_cleanup > 3600:
                    self.store.prune()
                    self.last_cleanup = time.monotonic()
                self.store.sync_allowed(self.allowed)
                if self.config.get("dynamic_enabled", False):
                    tasks = self.store.rows(
                        "SELECT * FROM requests WHERE status='pending' ORDER BY id LIMIT 1"
                    )
                    if tasks:
                        task = tasks[0]
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE requests SET status='running',updated=? WHERE id=?",
                                (time.time(), task["id"]),
                            )
                        try:
                            sub = self.store.get(task["subscription_id"])
                            if not sub["enabled"] or not self.allowed(sub):
                                raise ValueError("订阅已暂停或群已禁用")
                            if task["kind"] == "test":
                                items, _ = await self.client.feed(
                                    sub["uid"], set(), first_page_only=True
                                )
                                matching = [
                                    item
                                    for item in items
                                    if item["kind"] in sub["types"]
                                ]
                                if not matching:
                                    raise ValueError("该 UP 主没有可见且符合类型的动态")
                                with self.store.db:
                                    self.store.enqueue(
                                        sub,
                                        matching[-1],
                                        test=True,
                                        token=str(task["id"]),
                                    )
                            else:
                                await self.poll(sub["uid"])
                            status, error = "done", ""
                        except Exception as exc:
                            status, error = "failed", str(exc)[:500]
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE requests SET status=?,error=?,updated=? WHERE id=?",
                                (status, error, time.time(), task["id"]),
                            )
                    due = self.store.rows(
                        "SELECT c.* FROM creators c WHERE c.next_check<=? AND EXISTS(SELECT 1 FROM subscriptions s WHERE s.uid=c.uid AND s.enabled=1 AND s.blocked=0 AND s.types<>'[]') ORDER BY c.next_check LIMIT 1",
                        (time.time(),),
                    )
                    if due:
                        creator = due[0]
                        try:
                            await self.poll(creator["uid"])
                        except Exception as exc:
                            failures = creator["failures"] + 1
                            delay = min(
                                3600,
                                max(60, int(self.config.get("dynamic_interval", 180)))
                                * 2 ** min(failures, 5),
                            )
                            with self.store.db:
                                self.store.db.execute(
                                    "UPDATE creators SET checked=?,next_check=?,failures=?,error=? WHERE uid=?",
                                    (
                                        time.time(),
                                        time.time() + delay,
                                        failures,
                                        str(exc)[:500],
                                        creator["uid"],
                                    ),
                                )
                self.worker_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.worker_error = "检查任务异常：" + str(exc)[:200]
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2)
            except TimeoutError:
                pass

    async def deliver(self, job: dict):
        """Run one delivery, saving each send boundary before calling OneBot.

        Args:
            job: A pending or retryable persisted delivery.
        """
        db = self.store.db
        with db:
            db.execute(
                "UPDATE deliveries SET status='preparing',updated=? WHERE id=?",
                (time.time(), job["id"]),
            )
        try:
            sub = self.store.get(job["subscription_id"])
            item = json.loads(job["payload"])
            if not self.accepts(sub, item) or any(
                sub[key] != job[key]
                for key in ("platform", "bot_id", "group_id", "uid")
            ):
                raise ValueError("订阅已关闭或目标已改变")
            if not job["test"] and job["created"] < time.time() - 86400:
                raise ValueError("已超过 24 小时自动补发范围")
        except ValueError as exc:
            with db:
                db.execute(
                    "UPDATE deliveries SET status='cancelled',error=? WHERE id=?",
                    (str(exc), job["id"]),
                )
            return
        for stage in ("card", "video"):
            if job[stage + "_status"] in {"sent", "skipped"}:
                continue
            if stage == "video" and not (
                sub["send_video"] and self.config.get("send_video", True)
            ):
                with db:
                    db.execute(
                        "UPDATE deliveries SET video_status='skipped' WHERE id=?",
                        (job["id"],),
                    )
                continue
            sending = False
            try:
                with db:
                    db.execute(
                        "UPDATE deliveries SET stage=?,status='preparing' WHERE id=?",
                        (stage, job["id"]),
                    )
                message = await self.prepare(item, stage, bool(job["test"]))
                fresh = self.store.get(job["subscription_id"])
                if not self.accepts(fresh, item):
                    with db:
                        db.execute(
                            "UPDATE deliveries SET status='cancelled',error='发送前订阅已禁用' WHERE id=?",
                            (job["id"],),
                        )
                    return
                if stage == "video" and not (
                    fresh["send_video"] and self.config.get("send_video", True)
                ):
                    with db:
                        db.execute(
                            "UPDATE deliveries SET video_status='skipped' WHERE id=?",
                            (job["id"],),
                        )
                    continue
                with db:
                    db.execute(
                        "UPDATE deliveries SET status='sending',updated=? WHERE id=?",
                        (time.time(), job["id"]),
                    )
                sending = True
                receipt = await self.send(fresh, message)
                receipts = json.loads(
                    self.store.rows(
                        "SELECT receipts FROM deliveries WHERE id=?", (job["id"],)
                    )[0]["receipts"]
                )
                receipts.append({"stage": stage, "message_id": receipt})
                with db:
                    db.execute(
                        f"UPDATE deliveries SET {stage}_status='sent',status='preparing',receipts=?,error='',attempts=0,updated=? WHERE id=?",
                        (json.dumps(receipts), time.time(), job["id"]),
                    )
                job[stage + "_status"] = "sent"
                job["attempts"] = 0
            except asyncio.CancelledError:
                with db:
                    db.execute(
                        "UPDATE deliveries SET status=?,error=? WHERE id=?",
                        (
                            "uncertain" if sending else "pending",
                            "发送期间中断，请核对群记录" if sending else "",
                            job["id"],
                        ),
                    )
                raise
            except Exception as exc:
                uncertain = sending and not isinstance(exc, DeliveryPreparationError)
                attempts = job["attempts"] + 1
                permanent = isinstance(exc, DownloadError) and exc.permanent
                status = (
                    "uncertain"
                    if uncertain
                    else "failed"
                    if permanent or attempts > 3
                    else "retry"
                )
                delay = (60, 300, 900)[min(attempts - 1, 2)]
                with db:
                    db.execute(
                        "UPDATE deliveries SET status=?,attempts=?,next_attempt=?,error=?,updated=? WHERE id=?",
                        (
                            status,
                            attempts,
                            time.time() + delay,
                            str(exc)[:500],
                            time.time(),
                            job["id"],
                        ),
                    )
                    if status == "failed":
                        db.execute(
                            f"UPDATE deliveries SET {stage}_status='failed' WHERE id=?",
                            (job["id"],),
                        )
                return
        with db:
            db.execute(
                "UPDATE deliveries SET status='done',error='',updated=? WHERE id=?",
                (time.time(), job["id"]),
            )

    async def delivery_loop(self):
        while not self.stopping:
            try:
                self.store.sync_allowed(self.allowed)
                jobs = self.store.rows(
                    """SELECT d.* FROM deliveries d WHERE d.status IN ('pending','retry') AND d.next_attempt<=?
                    AND NOT EXISTS(SELECT 1 FROM deliveries earlier WHERE earlier.platform=d.platform
                    AND earlier.bot_id=d.bot_id AND earlier.group_id=d.group_id AND earlier.id<d.id
                    AND earlier.status IN ('pending','retry','preparing','sending','uncertain'))
                    ORDER BY d.created,d.id LIMIT 1""",
                    (time.time(),),
                )
                if jobs:
                    await self.deliver(jobs[0])
                self.delivery_error = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.delivery_error = "发送任务异常：" + str(exc)[:200]
            await asyncio.sleep(2)
