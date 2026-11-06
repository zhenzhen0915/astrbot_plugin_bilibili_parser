"""Verify live transitions, routing, retention, recovery and API validation."""

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from jinja2 import Environment

from astrbot_plugin_bilibili_parser.bilibili_client import BilibiliError
from astrbot_plugin_bilibili_parser.live import LiveClient, live_item
from astrbot_plugin_bilibili_parser.subscription_service import SubscriptionService
from astrbot_plugin_bilibili_parser.subscription_store import SubscriptionStore

ROOT = Path(__file__).resolve().parents[1]
TARGET = {"platform": "test", "bot_id": "999", "group_id": "100", "group_name": "Test"}
CREATOR = {"uid": "123", "name": "测试主播", "avatar": ""}


class LiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "runtime")
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )
        self.sent = []

        async def targets():
            return [TARGET, {**TARGET, "group_id": "200"}]

        async def prepare(item, stage, test):
            return item["kind"]

        async def send(sub, message):
            self.sent.append((sub["group_id"], message))
            return str(len(self.sent))

        self.service = SubscriptionService(
            {
                "live_enabled": True,
                "dynamic_enabled": False,
                "group_filter_mode": "all",
            },
            Path(self.tmp.name),
            self.http,
            targets,
            prepare,
            send,
        )
        self.store = self.service.store
        self.tracker = self.service.live
        self.sid = self.subscribe(TARGET, notify=True, record=True)
        self.now = time.time()
        self.info = {
            "uid": "123",
            "room_id": "888",
            "live": True,
            "started": int(self.now - 3600),
            "author": "测试主播",
            "avatar": "",
            "title": "游戏实况",
            "area": "游戏 / 单机",
            "cover": "",
        }

    async def asyncTearDown(self):
        await self.service.close()
        await self.http.aclose()
        self.tmp.cleanup()

    def subscribe(self, target, notify=False, record=False):
        sid, _ = self.store.add(target, CREATOR, [], False, "test")
        self.store.edit(sid, 1, {"live_notify": notify, "live_record": record}, "test")
        return sid

    def jobs(self):
        return self.store.rows("SELECT * FROM deliveries ORDER BY id")

    def edit(self, sid, changes):
        self.service.edit(sid, self.store.get(sid)["revision"], changes, "test")

    async def test_multi_group_live_only_dedup_and_delivery(self):
        self.subscribe({**TARGET, "group_id": "200"}, notify=False, record=True)
        self.tracker.ingest("123", self.info, self.now)
        self.tracker.ingest("123", self.info, self.now + 60)
        self.assertEqual(len(self.jobs()), 1)
        self.store.sync_allowed(self.service.allowed)
        await self.service.deliver(self.jobs()[0])
        self.assertEqual(self.sent, [("100", "live_start")])
        self.assertEqual(self.jobs()[0]["status"], "done")
        self.assertEqual(self.jobs()[0]["video_status"], "skipped")

    async def test_end_confirmation_duration_and_title_changes(self):
        self.tracker.ingest("123", self.info, self.now)
        self.tracker.ingest(
            "123", {**self.info, "title": "聊天", "area": "生活"}, self.now + 60
        )
        offline = {**self.info, "live": False, "started": 0}
        self.tracker.ingest("123", offline, self.now + 120)
        self.assertIsNone(self.store.rows("SELECT * FROM live_sessions")[0]["ended"])
        self.tracker.ingest("123", offline, self.now + 180)
        self.tracker.ingest("123", offline, self.now + 240)
        sessions = self.store.rows("SELECT * FROM live_sessions")
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["ended"], self.now + 120)
        self.assertEqual(len(json.loads(sessions[0]["history"])), 2)
        payload = json.loads(self.jobs()[-1]["payload"])
        self.assertIn("聊天", payload["title"])
        self.assertIn("1小时02分", payload["text"])
        self.assertEqual(len(self.jobs()), 2)
        self.assertEqual(self.jobs()[0]["status"], "cancelled")

    async def test_transient_offline_does_not_end(self):
        self.tracker.ingest("123", self.info, self.now)
        self.tracker.ingest("123", {**self.info, "live": False}, self.now + 60)
        self.tracker.ingest("123", self.info, self.now + 120)
        self.assertIsNone(self.store.rows("SELECT * FROM live_sessions")[0]["ended"])
        self.assertEqual(len(self.jobs()), 1)

    async def test_restart_reuses_session_and_record(self):
        self.tracker.ingest("123", self.info, self.now)
        path = Path(self.tmp.name) / "subscriptions.sqlite3"
        self.store.close()
        self.store = SubscriptionStore(path)
        self.service.store = self.tracker.store = self.store
        self.tracker.ingest("123", self.info, self.now + 60)
        self.assertEqual(len(self.jobs()), 1)
        self.assertEqual(len(self.tracker.history("123")["rows"]), 1)

    async def test_missed_restart_creates_two_sessions(self):
        self.tracker.ingest("123", self.info, self.now)
        self.tracker.ingest(
            "123", {**self.info, "started": int(self.now + 60)}, self.now + 120
        )
        sessions = self.store.rows("SELECT * FROM live_sessions ORDER BY id")
        self.assertEqual(len(sessions), 2)
        self.assertIsNotNone(sessions[0]["ended"])
        self.assertIsNone(sessions[1]["ended"])

    async def test_group_switch_during_render_prevents_send(self):
        self.tracker.ingest("123", self.info, self.now)

        async def prepare(*args):
            self.edit(self.sid, {"live_notify": False})
            return "card"

        self.service.prepare = prepare
        await self.service.deliver(self.jobs()[0])
        self.assertFalse(self.sent)
        self.assertEqual(self.jobs()[0]["status"], "cancelled")
        self.tracker.ingest("123", {**self.info, "title": "记录继续"}, self.now + 60)
        self.assertEqual(self.tracker.history("123")["rows"][0]["title"], "记录继续")

    async def test_group_filter_and_global_switch_prevent_delivery(self):
        self.tracker.ingest("123", self.info, self.now)
        self.service.config["live_enabled"] = False
        await self.service.deliver(self.jobs()[0])
        self.assertFalse(self.sent)
        self.service.config.update(
            live_enabled=True, group_filter_mode="whitelist", allowed_group_ids="200"
        )
        self.tracker.ingest(
            "123", {**self.info, "started": int(self.now + 10)}, self.now + 60
        )
        self.assertEqual(len(self.store.rows("SELECT * FROM live_sessions")), 1)

    async def test_stream_ending_during_render_cancels_start(self):
        self.tracker.ingest("123", self.info, self.now)

        async def prepare(*args):
            self.tracker.ingest("123", {**self.info, "live": False}, self.now + 60)
            self.tracker.ingest("123", {**self.info, "live": False}, self.now + 120)
            return "card"

        self.service.prepare = prepare
        await self.service.deliver(self.jobs()[0])
        self.assertFalse(self.sent)
        self.assertEqual(self.jobs()[0]["status"], "cancelled")

    async def test_missing_start_can_be_corrected_without_duplicate(self):
        self.tracker.ingest("123", {**self.info, "started": 0}, self.now)
        self.tracker.ingest("123", self.info, self.now + 60)
        session = self.store.rows("SELECT * FROM live_sessions")[0]
        self.assertFalse(session["start_estimated"])
        self.assertEqual(session["started"], self.info["started"])
        self.assertEqual(len(self.jobs()), 1)

    async def test_record_opt_in_retention_and_active_session(self):
        self.edit(self.sid, {"live_record": False})
        self.tracker.ingest("123", self.info, self.now)
        self.assertEqual(self.tracker.history("123")["rows"], [])
        self.edit(self.sid, {"live_record": True})
        self.tracker.ingest("123", self.info, self.now + 60)
        self.assertEqual(len(self.tracker.history("123")["rows"]), 1)
        self.store.prune()
        self.assertEqual(len(self.store.rows("SELECT * FROM live_sessions")), 1)
        with self.store.db:
            self.store.db.execute(
                "UPDATE live_sessions SET ended=?", (self.now - 31 * 86400,)
            )
        self.store.prune()
        self.assertEqual(self.store.rows("SELECT * FROM live_sessions"), [])

    async def test_unknown_start_and_template_escaping(self):
        self.tracker.ingest(
            "123",
            {**self.info, "started": 0, "title": "<script>alert(1)</script>"},
            self.now,
        )
        data = self.tracker.history("123")
        self.assertTrue(data["rows"][0]["start_estimated"])
        data.update(name="测试主播", calendar=data["calendars"][0])
        html = (
            Environment()
            .from_string(
                (
                    ROOT / "astrbot_plugin_bilibili_parser/templates/live_history.html"
                ).read_text("utf-8")
            )
            .render(**data)
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn(
            "首次检测",
            live_item(self.store.rows("SELECT * FROM live_sessions")[0])["text"],
        )

    async def test_new_live_only_subscription(self):
        async def creator(uid):
            return {**CREATOR, "uid": uid}

        self.service.client.creator = creator
        result = await self.service.add(
            TARGET, "456", [], False, "test", live_notify=True
        )
        row = self.store.get(result["id"])
        self.assertEqual(row["types"], [])
        self.assertTrue(row["live_notify"])
        with self.assertRaises(ValueError):
            self.service.save_settings({"live_interval": 1}, "test")
        with self.assertRaises(ValueError):
            self.service.edit(
                row["id"], row["revision"], {"live_notify": "true"}, "test"
            )


class LiveApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_status_and_missing_entry(self):
        raw = {"uid": 123, "room_id": 88, "live_status": 2, "live_time": 0}
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"code": 0, "data": {"123": raw}})
            )
        ) as http:
            status = await LiveClient(http).status("123")
            self.assertFalse(status["live"])
            raw["live_status"] = 1
            self.assertTrue((await LiveClient(http).status("123"))["live"])
            raw["live_status"] = 9
            with self.assertRaises(BilibiliError):
                await LiveClient(http).status("123")
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"code": 0, "data": {}})
            )
        ) as http:
            with self.assertRaises(BilibiliError):
                await LiveClient(http).status("123")

    def test_v1_migration_keeps_subscriptions(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "runtime") as folder:
            path = Path(folder) / "old.sqlite3"
            store = SubscriptionStore(path)
            sid, _ = store.add(TARGET, CREATOR, ["word"], False, "test")
            store.close()
            db = sqlite3.connect(path)
            with db:
                db.execute("ALTER TABLE subscriptions DROP COLUMN live_notify")
                db.execute("ALTER TABLE subscriptions DROP COLUMN live_record")
                db.execute("PRAGMA user_version=1")
            db.close()
            store = SubscriptionStore(path)
            self.assertEqual(store.get(sid)["types"], ["word"])
            self.assertEqual(store.get(sid)["live_notify"], 0)
            self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 3)
            store.close()
