"""Exercise durable baselines, multi-group fanout, permissions and recovery."""

import json
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from jinja2 import Environment

from astrbot_plugin_bilibili_parser.bilibili_client import BilibiliError
from astrbot_plugin_bilibili_parser.downloader import DownloadError
from astrbot_plugin_bilibili_parser.card import TEMPLATE, build_dynamic_card_data
from astrbot_plugin_bilibili_parser.dynamic_client import (
    DynamicClient,
    normalize_dynamic,
    parse_uid,
)
from astrbot_plugin_bilibili_parser.subscription_service import (
    SubscriptionService,
    DeliveryPreparationError,
)
from astrbot_plugin_bilibili_parser.subscription_store import SubscriptionStore


def item(identity="101", published=None, kind="word"):
    return {
        "id": identity,
        "uid": "123",
        "author": "Test UP",
        "avatar": "",
        "published": published or int(time.time()),
        "kind": kind,
        "title": "Title",
        "text": "Text",
        "images": [],
        "image_count": 0,
        "bvid": "BV1GJ411x7h7" if kind == "video" else "",
        "stats": {},
        "url": "https://t.bilibili.com/" + identity,
        "pinned": False,
    }


TARGET = {
    "platform": "test",
    "bot_id": "999001",
    "group_id": "100",
    "group_name": "Test group",
}
CREATOR = {"uid": "123", "name": "Test UP", "avatar": ""}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / "runtime"
        )
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.store = SubscriptionStore(self.path)
        self.sid, _ = self.store.add(TARGET, CREATOR, ["word", "video"], True, "test")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_baseline_pinned_and_shared_fanout(self):
        old = item("1", int(time.time()) - 10000)
        old["pinned"] = True
        self.store.ingest("123", [old], False, 0)
        self.assertEqual(self.store.rows("SELECT * FROM deliveries"), [])
        self.store.db.execute(
            "UPDATE subscriptions SET baseline=?", (time.time() - 10,)
        )
        second, _ = self.store.add(
            {**TARGET, "group_id": "200"}, CREATOR, ["word"], False, "test"
        )
        new = item("2")
        self.store.ingest("123", [old, new], False, 0)
        deliveries = self.store.rows("SELECT * FROM deliveries")
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["group_id"], "100")
        self.store.db.execute(
            "UPDATE subscriptions SET baseline=? WHERE id=?", (time.time() - 10, second)
        )
        self.store.db.commit()
        self.store.ingest("123", [old, new, item("3")], False, 0)
        self.store.ingest("123", [old, new, item("3")], False, 0)
        self.assertEqual(len(self.store.rows("SELECT * FROM deliveries")), 3)

    def test_edit_conflicts_and_blocking_cancel_queue(self):
        sub = self.store.get(self.sid)
        self.store.enqueue(sub, item("1"))
        self.store.db.commit()
        self.store.edit(self.sid, 1, {"enabled": False}, "test")
        with self.assertRaises(ValueError):
            self.store.edit(self.sid, 1, {"enabled": True}, "test")
        self.assertEqual(
            self.store.rows("SELECT status FROM deliveries")[0]["status"], "cancelled"
        )
        self.store.edit(self.sid, 2, {"enabled": True}, "test")
        self.assertEqual(self.store.get(self.sid)["initialized"], 0)
        self.store.sync_allowed(lambda s: False)
        self.assertEqual(self.store.get(self.sid)["blocked"], 1)

    def test_restart_and_no_recycled_subscription_ids(self):
        sub = self.store.get(self.sid)
        delivery = self.store.enqueue(sub, item())
        with self.store.db:
            self.store.db.execute(
                "UPDATE deliveries SET status='sending' WHERE id=?", (delivery,)
            )
        self.store.close()
        self.store = SubscriptionStore(self.path)
        self.assertEqual(
            self.store.rows("SELECT status FROM deliveries")[0]["status"], "uncertain"
        )
        with self.assertRaises(ValueError):
            self.store.retry(delivery, "test")
        self.store.remove(self.sid, 1, "test")
        new_id, _ = self.store.add(TARGET, CREATOR, ["word"], True, "test")
        self.assertGreater(new_id, self.sid)

    def test_duplicate_add_does_not_change_settings(self):
        sid, created = self.store.add(TARGET, CREATOR, ["article"], False, "test")
        self.assertEqual(sid, self.sid)
        self.assertFalse(created)
        self.assertEqual(self.store.get(sid)["types"], ["word", "video"])

    def test_cleanup_keeps_old_ids_and_uncertain_jobs(self):
        old = item("old1", int(time.time()) - 40 * 86400)
        self.store.ingest("123", [old], False, 0)
        identity = self.store.enqueue(self.store.get(self.sid), item())
        with self.store.db:
            self.store.db.execute(
                "UPDATE deliveries SET status='uncertain',updated=? WHERE id=?",
                (time.time() - 40 * 86400, identity),
            )
        self.store.prune()
        self.assertIn("old1", self.store.known("123"))
        self.assertEqual(
            self.store.rows("SELECT payload FROM dynamic_items")[0]["payload"], "{}"
        )
        self.assertEqual(
            self.store.rows("SELECT status FROM deliveries")[0]["status"], "uncertain"
        )

    def test_future_schema_is_not_downgraded(self):
        with self.store.db:
            self.store.db.execute("PRAGMA user_version=99")
        with self.assertRaises(RuntimeError):
            SubscriptionStore(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_catchup_cap_and_type_filter(self):
        self.store.ingest("123", [], False, 0)
        with self.store.db:
            self.store.db.execute(
                "UPDATE subscriptions SET baseline=?", (time.time() - 100,)
            )
        self.store.ingest(
            "123",
            [item(str(i)) for i in range(1, 16)] + [item("30", kind="article")],
            True,
            0,
        )
        self.assertEqual(
            len(self.store.rows("SELECT * FROM deliveries WHERE status='pending'")), 10
        )
        self.assertEqual(
            len(self.store.rows("SELECT * FROM deliveries WHERE status='skipped'")), 5
        )
        self.assertEqual(self.store.rows("SELECT gap FROM creators")[0]["gap"], 1)

    def test_manual_cooldown_survives_reopen(self):
        self.store.request(self.store.get(self.sid), "check", "test")
        self.store.close()
        self.store = SubscriptionStore(self.path)
        with self.assertRaises(ValueError):
            self.store.request(self.store.get(self.sid), "test", "test")


class DynamicClientTests(unittest.IsolatedAsyncioTestCase):
    def test_uid_validation(self):
        self.assertEqual(parse_uid("https://space.bilibili.com/123?x=1"), "123")
        for value in (
            "abc",
            "0",
            "https://space.bilibili.com.evil/123",
            "https://space.bilibili.com@evil/123",
        ):
            with self.assertRaises(ValueError):
                parse_uid(value)

    def test_opus_and_forward_normalization(self):
        raw = {
            "id_str": "101",
            "type": "DYNAMIC_TYPE_DRAW",
            "modules": {
                "module_author": {"mid": 123, "name": "UP", "pub_ts": 100},
                "module_dynamic": {
                    "major": {
                        "opus": {
                            "title": "Hello",
                            "summary": {"text": "body"},
                            "pics": [{"url": "https://i0.hdslb.com/a.png"}],
                        }
                    }
                },
                "module_stat": {"like": {"count": 3}},
            },
        }
        result = normalize_dynamic(raw, "123")
        self.assertEqual(result["text"], "body")
        self.assertEqual(result["stats"], {"like": 3})
        self.assertIsNone(normalize_dynamic(raw, "124"))
        forward = {**raw, "type": "DYNAMIC_TYPE_FORWARD", "orig": raw}
        self.assertIn("转发自 UP", normalize_dynamic(forward, "123")["text"])

    async def test_limited_response_is_not_an_empty_feed(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(412, text="blocked"))
        ) as http:
            client = DynamicClient(http)
            with self.assertRaisesRegex(BilibiliError, "412"):
                await client.feed("123", set())

    async def test_cookie_only_sent_to_fixed_api(self):
        seen = []

        def handler(req):
            seen.append(req)
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [], "has_more": False}}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = DynamicClient(http, lambda: "SESSDATA=test")
            items, gap = await client.feed("123", set())
        self.assertFalse(items)
        self.assertFalse(gap)
        self.assertEqual(seen[0].url.host, "api.bilibili.com")
        self.assertEqual(seen[0].headers["cookie"], "SESSDATA=test")

    async def test_dynamic_template_escapes_text(self):
        data = item()
        data["text"] = "<script>alert(1)</script>"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ) as http:
            values = await build_dynamic_card_data(data, http, test=True)
        html = Environment().from_string(TEMPLATE.read_text("utf-8")).render(**values)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("测试推送", html)
        self.assertIn("查看动态", html)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / "runtime"
        )
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )
        self.sent = []

        async def targets():
            return [TARGET]

        async def prepare(data, stage, test):
            return stage

        async def send(sub, message):
            self.sent.append(message)
            return str(len(self.sent))

        self.service = SubscriptionService(
            {"dynamic_enabled": True, "allowed_group_ids": "100"},
            Path(self.tmp.name),
            self.http,
            targets,
            prepare,
            send,
        )
        self.sid, _ = self.service.store.add(
            TARGET, CREATOR, ["word", "video"], True, "test"
        )

    async def asyncTearDown(self):
        await self.service.close()
        await self.http.aclose()
        self.tmp.cleanup()

    def job(self, kind="video"):
        self.service.store.enqueue(self.service.store.get(self.sid), item(kind=kind))
        self.service.store.db.commit()
        return self.service.store.rows(
            "SELECT * FROM deliveries ORDER BY id DESC LIMIT 1"
        )[0]

    async def test_image_then_video_and_success_persisted(self):
        await self.service.deliver(self.job())
        self.assertEqual(self.sent, ["card", "video"])
        row = self.service.store.rows("SELECT * FROM deliveries")[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(len(json.loads(row["receipts"])), 2)

    async def test_global_send_video_switch_skips_video_stage(self):
        self.service.config["send_video"] = False
        await self.service.deliver(self.job())
        self.assertEqual(self.sent, ["card"])
        row = self.service.store.rows("SELECT * FROM deliveries")[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["video_status"], "skipped")

    async def test_video_retry_never_resends_card(self):
        original = self.service.prepare

        async def fail_video(data, stage, test):
            if stage == "video":
                raise DeliveryPreparationError("download temporary failure")
            return await original(data, stage, test)

        self.service.prepare = fail_video
        await self.service.deliver(self.job())
        self.assertEqual(self.sent, ["card"])
        self.service.prepare = original
        await self.service.deliver(
            self.service.store.rows("SELECT * FROM deliveries")[0]
        )
        self.assertEqual(self.sent, ["card", "video"])

    async def test_uncertain_send_is_not_automatically_retried(self):
        async def timeout(sub, message):
            raise TimeoutError("ambiguous send")

        self.service.send = timeout
        await self.service.deliver(self.job())
        row = self.service.store.rows("SELECT * FROM deliveries")[0]
        self.assertEqual(row["status"], "uncertain")
        self.assertEqual(
            self.service.store.rows(
                "SELECT * FROM deliveries WHERE status IN ('pending','retry')"
            ),
            [],
        )

    async def test_disable_during_render_prevents_send(self):
        async def disable(data, stage, test):
            self.service.config["dynamic_enabled"] = False
            return stage

        self.service.prepare = disable
        await self.service.deliver(self.job())
        self.assertEqual(self.sent, [])
        self.assertEqual(
            self.service.store.rows("SELECT status FROM deliveries")[0]["status"],
            "cancelled",
        )

    async def test_manual_confirmation_resumes_only_remaining_stage(self):
        job = self.job()
        with self.service.store.db:
            self.service.store.db.execute(
                "UPDATE deliveries SET status='uncertain',stage='card' WHERE id=?",
                (job["id"],),
            )
        self.service.store.resolve_delivery(job["id"], "sent", "web:test")
        await self.service.deliver(
            self.service.store.rows("SELECT * FROM deliveries")[0]
        )
        self.assertEqual(self.sent, ["video"])
        row = self.service.store.rows("SELECT * FROM deliveries")[0]
        self.assertEqual(row["status"], "done")
        self.assertEqual(len(json.loads(row["receipts"])), 2)
        with self.assertRaises(ValueError):
            self.service.store.resolve_delivery(job["id"], "not_sent", "web:test")

    async def test_manual_confirmation_not_sent_retries_stage(self):
        job = self.job("word")
        with self.service.store.db:
            self.service.store.db.execute(
                "UPDATE deliveries SET status='uncertain' WHERE id=?", (job["id"],)
            )
        self.service.store.resolve_delivery(job["id"], "not_sent", "web:test")
        await self.service.deliver(
            self.service.store.rows("SELECT * FROM deliveries")[0]
        )
        self.assertEqual(self.sent, ["card"])

    async def test_disabled_group_cannot_be_subscribed(self):
        with self.assertRaises(ValueError):
            await self.service.add(
                {**TARGET, "group_id": "200"}, "123", "all", True, "test"
            )

    async def test_oversized_video_is_not_automatically_retried(self):
        original = self.service.prepare

        async def oversized(data, stage, test):
            if stage == "video":
                raise DownloadError("视频超过下载大小上限", permanent=True)
            return await original(data, stage, test)

        self.service.prepare = oversized
        await self.service.deliver(self.job())
        row = self.service.store.rows("SELECT * FROM deliveries")[0]
        self.assertEqual(self.sent, ["card"])
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["video_status"], "failed")

    async def test_uncertain_target_blocks_later_jobs_but_not_another_group(self):
        first = self.job("word")
        with self.service.store.db:
            self.service.store.db.execute(
                "UPDATE deliveries SET status='uncertain' WHERE id=?", (first["id"],)
            )
            self.service.store.enqueue(self.service.store.get(self.sid), item("102"))
        self.service.config["allowed_group_ids"] = "100,200"
        sid, _ = self.service.store.add(
            {**TARGET, "group_id": "200"}, CREATOR, ["word"], False, "test"
        )
        with self.service.store.db:
            self.service.store.enqueue(self.service.store.get(sid), item("103"))
        original = self.service.send

        async def send(sub, message):
            self.assertEqual(sub["group_id"], "200")
            self.service.stopping = True
            return await original(sub, message)

        self.service.send = send
        await self.service.delivery_loop()
        rows = self.service.store.rows("SELECT status FROM deliveries ORDER BY id")
        self.assertEqual([r["status"] for r in rows], ["uncertain", "pending", "done"])

    async def test_credentials_are_redacted_and_invalid_settings_rejected(self):
        self.service.save_settings({"cookie": "SESSDATA=secret"}, "test")
        self.assertTrue(self.service.settings()["cookie_configured"])
        self.assertNotIn("secret", json.dumps(self.service.snapshot()))
        self.assertNotIn("cookie", self.service.settings())
        with self.assertRaises(ValueError):
            self.service.save_settings({"dynamic_interval": 1}, "test")
        self.assertNotIn(
            "secret", json.dumps(self.service.store.rows("SELECT * FROM audit_log"))
        )


if __name__ == "__main__":
    unittest.main()
