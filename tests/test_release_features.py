"""Verify share cards, group policies and popularity-based comment selection."""

import json
import unittest
from types import SimpleNamespace

import httpx

from astrbot_plugin_bilibili_parser.bilibili_client import BilibiliClient
from astrbot_plugin_bilibili_parser.group_filter import group_allowed
from astrbot_plugin_bilibili_parser.parser import (
    VideoRef,
    find_card_reference,
    find_message_reference,
)

URL = "https://www.bilibili.com/video/BV1GJ411x7h7?p=2&share_source=qq"
CARD = {"app": "com.tencent.miniapp_01", "meta": {"detail_1": {"qqdocurl": URL}}}


class ShareCardTests(unittest.TestCase):
    def test_nested_json_and_escaped_link(self):
        self.assertEqual(find_card_reference(CARD), VideoRef("BV1GJ411x7h7", 2))
        self.assertEqual(
            find_card_reference(json.dumps(CARD).replace("/", "\\/")),
            VideoRef("BV1GJ411x7h7", 2),
        )
        self.assertEqual(
            find_card_reference({"meta": {"news": {"jumpUrl": "b23.tv/test123"}}}),
            "https://b23.tv/test123",
        )

    def test_xml_attribute_and_element(self):
        for payload in (
            '<msg url="' + URL.replace("&", "&amp;") + '"><item/></msg>',
            "<msg><item><url>" + URL.replace("&", "&amp;") + "</url></item></msg>",
        ):
            self.assertEqual(find_card_reference(payload), VideoRef("BV1GJ411x7h7", 2))

    def test_unrelated_and_malformed_cards_are_ignored(self):
        for payload in (
            {"title": "BV1GJ411x7h7", "url": "https://example.com/"},
            {"url": "https://evil.example/video/BV1GJ411x7h7"},
            {"url": "https://b23.tv.evil.example/a"},
            {"url": "https://b23.tv@evil.example/a"},
            {"url": "https://b23.tv:123/a"},
            {"url": "https://[invalid/"},
            "{broken json",
            "<!DOCTYPE msg [<!ENTITY a 'test'>]><msg>&a;</msg>",
            {"title": "x" * 65536, "url": URL},
        ):
            self.assertIsNone(find_card_reference(payload), str(payload)[:100])

    def test_raw_onebot_and_converted_components(self):
        raw = {"message": [{"type": "json", "data": {"data": json.dumps(CARD)}}]}
        self.assertEqual(
            find_message_reference("", [], raw), VideoRef("BV1GJ411x7h7", 2)
        )
        self.assertIsNone(find_message_reference("", [], raw, False))
        self.assertEqual(
            find_message_reference("", [SimpleNamespace(type="json", data=CARD)], None),
            VideoRef("BV1GJ411x7h7", 2),
        )
        raw = {
            "message": [
                {"type": "xml", "data": {"data": '<msg url="https://b23.tv/x"/>'}}
            ]
        }
        self.assertEqual(find_message_reference("", [], raw), "https://b23.tv/x")
        raw = {
            "message": '[CQ:json,data={"meta": {"news": {"jumpUrl": "https://b23.tv/x"}}}]'
        }
        self.assertEqual(find_message_reference("", [], raw), "https://b23.tv/x")


class GroupFilterTests(unittest.TestCase):
    def test_empty_default_and_existing_config(self):
        self.assertFalse(group_allowed("123", {}))
        self.assertTrue(group_allowed("123", {"allowed_group_ids": "123，456\n789"}))
        self.assertFalse(group_allowed("999", {"allowed_group_ids": "123，456\n789"}))

    def test_blacklist_all_and_invalid_group(self):
        config = {"group_filter_mode": "blacklist", "blocked_group_ids": "123 456"}
        self.assertFalse(group_allowed("123", config))
        self.assertTrue(group_allowed("789", config))
        self.assertTrue(group_allowed("123", {"group_filter_mode": "all"}))
        self.assertFalse(group_allowed("123", {"group_filter_mode": "invalid"}))
        for group in ("", "0", "private", "-1"):
            self.assertFalse(group_allowed(group, {"group_filter_mode": "all"}))


class CommentRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_likes_hot_dedup_and_pinned(self):
        first = {
            "rpid": 1,
            "like": 100,
            "member": {"uname": "A"},
            "content": {"message": "A"},
        }
        second = {
            "rpid": 2,
            "like": 3,
            "member": {"uname": "B"},
            "content": {"message": "B"},
        }
        third = {
            "rpid": 3,
            "like": 20,
            "member": {"uname": "C"},
            "content": {"message": "C"},
        }
        data = {
            "hots": [first],
            "replies": [first, second, third],
            "top": {"upper": {**second, "like": 9999}},
        }
        for sort, expected in (("likes", ["A", "C"]), ("hot", ["A", "B"])):
            client = BilibiliClient(sort)
            await client.http.aclose()
            client.http = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={"code": 0, "data": data})
                )
            )
            try:
                comments = await client._get_comments(123)
                self.assertEqual([c.text for c in comments], expected)
            finally:
                await client.close()

    async def test_wbi_fallback_uses_anonymous_nav_keys(self):
        paths = []

        def handler(request):
            paths.append(request.url.path)
            if request.url.path == "/x/v2/reply":
                return httpx.Response(200, json={"code": -412})
            if request.url.path == "/x/web-interface/nav":
                return httpx.Response(
                    200,
                    json={
                        "code": -101,
                        "data": {
                            "wbi_img": {
                                "img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
                                "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png",
                            }
                        },
                    },
                )
            self.assertEqual(request.url.params["mode"], "3")
            self.assertIn("w_rid", request.url.params)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "replies": [
                            {
                                "like": 42,
                                "member": {"uname": "A"},
                                "content": {"message": "Popular"},
                            }
                        ]
                    },
                },
            )

        client = BilibiliClient()
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            comments = await client._get_comments(123)
            self.assertEqual(comments[0].likes, 42)
            self.assertEqual(paths[-1], "/x/v2/reply/wbi/main")
        finally:
            await client.close()
