"""Exercise QQ command parsing and group-local mention bindings."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from astrbot.api.message_components import At, Image, Plain

from astrbot_plugin_bilibili_parser.main import BilibiliParser
from astrbot_plugin_bilibili_parser.subscription_service import SubscriptionService

ROOT = Path(__file__).resolve().parents[1]
TARGET = {
    "platform": "test",
    "bot_id": "999",
    "group_id": "100",
    "group_name": "测试群",
}
CREATOR = {"uid": "1596826105", "name": "测试主播", "avatar": ""}


class FakeBot:
    async def call_action(self, action, **kwargs):
        if action == "get_group_member_info":
            return {"user_id": kwargs["user_id"], "role": "member"}
        raise AssertionError(action)


class FakeEvent:
    def __init__(self, plain, mention=None, admin=True):
        components = [Plain(plain)]
        if mention:
            components.append(At(qq=mention))
        self.message_obj = SimpleNamespace(self_id="999", message=components)
        self.platform_meta = SimpleNamespace(id="test")
        self.bot = FakeBot()
        self.admin = admin
        self.stopped = False

    def get_sender_id(self):
        return "777"

    def get_group_id(self):
        return "100"

    def is_admin(self):
        return self.admin

    def plain_result(self, value):
        return value

    def chain_result(self, value):
        return value

    def stop_event(self):
        self.stopped = True


class QQCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "runtime")
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        )

        async def targets():
            return [TARGET]

        async def prepare(*args):
            return None

        async def send(*args):
            return "1"

        self.service = SubscriptionService(
            {"live_enabled": True, "group_filter_mode": "all"},
            Path(self.tmp.name),
            self.http,
            targets,
            prepare,
            send,
        )
        self.sid, _ = self.service.store.add(TARGET, CREATOR, [], False, "test")
        self.service.store.edit(self.sid, 1, {"live_record": True}, "test")
        self.plugin = BilibiliParser.__new__(BilibiliParser)
        self.plugin.subscriptions = self.service
        self.plugin.config = self.service.config
        self.plugin.render_slots = asyncio.Semaphore(1)

        async def render(*args, **kwargs):
            return Path(__file__)

        self.plugin.html_render = render

    async def asyncTearDown(self):
        await self.service.close()
        await self.http.aclose()
        self.tmp.cleanup()

    async def command(self, text, plain=None, mention=None, admin=True):
        event = FakeEvent(plain or "/bili " + text, mention, admin)
        output = [value async for value in self.plugin.bili_command(event, text)]
        return output, event

    async def test_two_word_live_record_uid_renders_image(self):
        result, event = await self.command("直播 记录 1596826105")
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0][0], Image)
        self.assertTrue(event.stopped)

    async def test_at_lookup_requires_admin_binding_once(self):
        response, _ = await self.command(
            "直播 记录 @昵称(555)", "/bili 直播 记录 ", "555", False
        )
        self.assertIn("尚未绑定主播", response[0])
        response, _ = await self.command(
            "绑定 1596826105 @昵称(555)", "/bili 绑定 1596826105 ", "555"
        )
        self.assertIn("已将本群成员", response[0])
        result, _ = await self.command(
            "直播 记录 @昵称(555)", "/bili 直播 记录 ", "555", False
        )
        self.assertIsInstance(result[0][0], Image)
        response, _ = await self.command("解绑 @昵称(555)", "/bili 解绑 ", "555")
        self.assertIn("已解除", response[0])
        self.assertIsNone(self.service.store.bound_uid(TARGET, "555"))

    async def test_binding_is_group_local_and_requires_subscription(self):
        self.service.store.bind_qq(TARGET, "555", CREATOR["uid"], "test")
        other = {**TARGET, "group_id": "200"}
        self.assertIsNone(self.service.store.bound_uid(other, "555"))
        with self.assertRaises(ValueError):
            self.service.store.bind_qq(TARGET, "555", "123", "test")

    async def test_non_admin_cannot_bind_and_bot_mention_is_ignored(self):
        response, _ = await self.command(
            "绑定 1596826105 @昵称(555)", "/bili 绑定 1596826105 ", "555", False
        )
        self.assertIn("只有本群群主", response[0])
        self.assertIsNone(self.service.store.bound_uid(TARGET, "555"))
        result, _ = await self.command(
            "直播 记录 1596826105", mention="999", admin=False
        )
        self.assertIsInstance(result[0][0], Image)

    async def test_month_selector_after_mention(self):
        self.service.store.bind_qq(TARGET, "555", CREATOR["uid"], "test")
        result, _ = await self.command(
            "直播 记录 @昵称(555) 2099-01", "/bili 直播 记录 2099-01", "555", False
        )
        self.assertIn("可用月份", result[0])
