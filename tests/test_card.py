"""Check hostile content, missing assets and card data boundaries."""

import unittest

import httpx
from jinja2 import Environment

from astrbot_plugin_bilibili_parser.bilibili_client import Comment, VideoInfo
from astrbot_plugin_bilibili_parser.card import TEMPLATE, build_card_data
from astrbot_plugin_bilibili_parser.parser import VideoRef


class CardTests(unittest.IsolatedAsyncioTestCase):
    async def test_untrusted_content_is_escaped_and_remote_assets_are_restricted(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(503)

        info = VideoInfo(
            ref=VideoRef("BV1GJ411x7h7", 2),
            aid=123,
            title='<script>alert("title")</script>',
            description="A description with <img src=x onerror=alert(1)>",
            author="<b>UP</b>",
            duration=61,
            cover_url="https://i0.hdslb.com/cover.jpg",
            stats={"view": 100000000},
            tags=("<svg onload=alert(1)>",),
            comments=(
                Comment(
                    "<script>user</script>",
                    "<iframe>comment</iframe>",
                    1,
                    "http://127.0.0.1/private",
                ),
            ),
            comments_available=True,
            author_avatar_url="https://unrelated.example/avatar.jpg",
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            data = await build_card_data(info, http)
        self.assertEqual(requested, ["https://i0.hdslb.com/cover.jpg"])
        self.assertFalse(data["cover"])
        self.assertFalse(data["author_avatar"])
        self.assertTrue(data["background"].startswith("data:image/png;base64,"))
        self.assertNotEqual(data["background"], data["character"])
        self.assertEqual(data["stats"][0]["value"], "1.0亿")
        self.assertEqual(data["url"], "https://www.bilibili.com/video/BV1GJ411x7h7?p=2")
        html = (
            Environment(autoescape=False)
            .from_string(TEMPLATE.read_text("utf-8"))
            .render(**data)
        )
        self.assertNotIn("<script>", html)
        self.assertNotIn("<iframe>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
        self.assertIn("封面暂不可用", html)
        self.assertIn("<h2>评论</h2>", html)

    async def test_unavailable_comments_and_long_text(self):
        info = VideoInfo(
            ref=VideoRef("BV1GJ411x7h7"),
            aid=123,
            title="题" * 300,
            description="文" * 2000,
            author="UP",
            duration=90,
            cover_url="",
            stats={},
            tags=(),
            comments=(),
            comments_available=False,
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ) as http:
            data = await build_card_data(info, http)
        self.assertEqual(len(data["title"]), 120)
        self.assertEqual(len(data["description"]), 800)
        self.assertEqual(data["comments_empty"], "评论暂不可用")
        self.assertEqual(data["duration"], "01:30")


if __name__ == "__main__":
    unittest.main()
