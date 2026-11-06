import asyncio
import unittest

import httpx

from astrbot_plugin_bilibili_parser.bilibili_client import (
    BilibiliClient,
    VideoInfo,
    sign_wbi,
)
from astrbot_plugin_bilibili_parser.formatter import format_video
from astrbot_plugin_bilibili_parser.parser import VideoRef, find_reference


class ParserTests(unittest.TestCase):
    def test_url_and_page(self):
        self.assertEqual(
            find_reference("看看 https://www.bilibili.com/video/BV1GJ411x7h7?p=2"),
            VideoRef("BV1GJ411x7h7", 2),
        )

    def test_short_link(self):
        self.assertEqual(
            find_reference("https://b23.tv/abc1234"), "https://b23.tv/abc1234"
        )

    def test_unrelated_host_does_not_trigger(self):
        self.assertIsNone(find_reference("https://evil.example/video/BV1GJ411x7h7"))

    def test_bare_id(self):
        self.assertEqual(find_reference("BV1GJ411x7h7"), VideoRef("BV1GJ411x7h7"))


class ClientTests(unittest.TestCase):
    def test_sign_wbi(self):
        signed = sign_wbi({"oid": 123, "type": 1}, "a" * 32, "b" * 32, now=1234567890)
        self.assertEqual(signed["wts"], 1234567890)
        self.assertRegex(str(signed["w_rid"]), r"^[0-9a-f]{32}$")

    def test_video_info_with_two_comments(self):
        async def run():
            def handler(request: httpx.Request) -> httpx.Response:
                path = request.url.path
                if path == "/x/web-interface/view":
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "aid": 123,
                                "bvid": "BV1GJ411x7h7",
                                "title": "测试视频",
                                "desc": "视频简介",
                                "owner": {
                                    "name": "UP 主",
                                    "face": "https://i0.hdslb.com/up.jpg",
                                },
                                "duration": 61,
                                "pic": "http://i0.hdslb.com/test.jpg",
                                "pages": [{"page": 1}],
                                "stat": {"view": 12345, "like": 123, "reply": 2},
                            },
                        },
                    )
                if path == "/x/tag/archive/tags":
                    return httpx.Response(
                        200, json={"code": 0, "data": [{"tag_name": "科技"}]}
                    )
                if path == "/x/v2/reply":
                    return httpx.Response(
                        200,
                        json={
                            "code": 0,
                            "data": {
                                "replies": [
                                    {
                                        "member": {
                                            "uname": "甲",
                                            "avatar": "https://i0.hdslb.com/a.jpg",
                                        },
                                        "content": {"message": "第一条"},
                                        "like": 5,
                                    },
                                    {
                                        "member": {"uname": "乙"},
                                        "content": {"message": "第二条"},
                                        "like": 3,
                                    },
                                    {
                                        "member": {"uname": "丙"},
                                        "content": {"message": "第三条"},
                                        "like": 1,
                                    },
                                ]
                            },
                        },
                    )
                return httpx.Response(404)

            client = BilibiliClient()
            await client.http.aclose()
            client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                info = await client.get_video(VideoRef("BV1GJ411x7h7"))
            finally:
                await client.close()
            return info

        info: VideoInfo = asyncio.run(run())
        self.assertEqual(info.tags, ("科技",))
        self.assertEqual(info.author_avatar_url, "https://i0.hdslb.com/up.jpg")
        self.assertEqual(info.comments[0].avatar_url, "https://i0.hdslb.com/a.jpg")
        self.assertEqual(
            [comment.text for comment in info.comments], ["第一条", "第二条"]
        )
        message = format_video(info)
        self.assertIn("播放：1.2万", message)
        self.assertIn("视频简介", message)
        self.assertIn("封面：https://i0.hdslb.com/test.jpg", message)

    def test_short_link_rejects_external_redirect(self):
        async def run():
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    302, headers={"Location": "https://evil.example/video/BV1GJ411x7h7"}
                )

            client = BilibiliClient()
            await client.http.aclose()
            client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                await client.resolve_short_url("https://b23.tv/abc1234")
            finally:
                await client.close()

        from astrbot_plugin_bilibili_parser.bilibili_client import BilibiliError

        with self.assertRaises(BilibiliError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
