"""Bounded access to public Bilibili creator feeds."""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse

import httpx

from .bilibili_client import API_ROOT, BilibiliError

TYPE_LABELS = {
    "video": "视频",
    "draw": "图文",
    "word": "文字",
    "forward": "转发",
    "article": "专栏",
    "other": "其他",
}
TYPE_MAP = {
    "DYNAMIC_TYPE_AV": "video",
    "DYNAMIC_TYPE_DRAW": "draw",
    "DYNAMIC_TYPE_WORD": "word",
    "DYNAMIC_TYPE_FORWARD": "forward",
    "DYNAMIC_TYPE_ARTICLE": "article",
}


def parse_uid(value: str) -> str:
    """Accept a numeric UID or an exact Bilibili space URL."""
    value = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        try:
            url = urlparse(value)
            if (
                url.scheme not in {"http", "https"}
                or url.hostname != "space.bilibili.com"
                or url.username
                or url.port not in {None, 80, 443}
            ):
                raise ValueError
            value = url.path.strip("/").split("/")[0]
        except ValueError:
            raise ValueError(
                "请提供数字 UID 或 space.bilibili.com 个人主页地址"
            ) from None
    if not re.fullmatch(r"[1-9][0-9]{0,19}", value):
        raise ValueError("UID 格式不正确")
    return value


def normalize_dynamic(raw: dict, uid: str) -> dict | None:
    """Normalize one feed item, including opus content and a forwarded original.

    Args:
        raw: An item returned by the public feed API.
        uid: The creator whose feed was requested.

    Returns:
        A JSON-serializable card payload, or None for an invalid item.
    """
    dynamic_id = str(raw.get("id_str") or "")
    modules = raw.get("modules") or {}
    author = modules.get("module_author") or {}
    if not dynamic_id.isdigit() or str(author.get("mid")) != uid:
        return None
    detail = modules.get("module_dynamic") or {}
    major = detail.get("major") or {}
    archive = major.get("archive") or {}
    opus = major.get("opus") or {}
    article = major.get("article") or {}
    kind = TYPE_MAP.get(raw.get("type"), "other")
    text = str(
        (detail.get("desc") or {}).get("text")
        or (opus.get("summary") or {}).get("text")
        or archive.get("desc")
        or ""
    )
    images = [
        str(p.get("src") or p.get("url") or "")
        for p in ((major.get("draw") or {}).get("items") or opus.get("pics") or [])
        if isinstance(p, dict)
    ]
    title = str(archive.get("title") or article.get("title") or opus.get("title") or "")
    images = images or [str(x) for x in (article.get("covers") or [])]
    if archive.get("cover"):
        images = [str(archive["cover"])]
    original = raw.get("orig") or {}
    if kind == "forward" and isinstance(original, dict):
        orig_modules = original.get("modules") or {}
        orig_author = orig_modules.get("module_author") or {}
        orig_detail = orig_modules.get("module_dynamic") or {}
        orig_major = orig_detail.get("major") or {}
        orig_opus = orig_major.get("opus") or {}
        orig_archive = orig_major.get("archive") or {}
        orig_text = str(
            (orig_detail.get("desc") or {}).get("text")
            or (orig_opus.get("summary") or {}).get("text")
            or orig_archive.get("title")
            or "原动态暂不可用"
        )
        text += (
            "\n\n转发自 "
            + str(orig_author.get("name") or "原作者")
            + "：\n"
            + orig_text
        )
        images = [
            str(p.get("src") or p.get("url") or "")
            for p in (
                (orig_major.get("draw") or {}).get("items")
                or orig_opus.get("pics")
                or []
            )
            if isinstance(p, dict)
        ]
        if not images and orig_archive.get("cover"):
            images = [str(orig_archive["cover"])]
    try:
        published = int(author.get("pub_ts") or 0)
    except (TypeError, ValueError):
        return None
    if published <= 0:
        return None
    stats = {}
    for key in ("like", "comment", "forward"):
        value = (modules.get("module_stat") or {}).get(key) or {}
        if isinstance(value.get("count"), int):
            stats[key] = value["count"]
    bvid = str(archive.get("bvid") or "")
    return {
        "id": dynamic_id,
        "uid": uid,
        "author": str(author.get("name") or uid),
        "avatar": str(author.get("face") or ""),
        "published": published,
        "kind": kind,
        "title": title[:500],
        "text": text[:12000],
        "images": images[:9],
        "image_count": len(images),
        "bvid": bvid if re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) else "",
        "stats": stats,
        "url": "https://t.bilibili.com/" + dynamic_id,
        "pinned": (modules.get("module_tag") or {}).get("text") == "置顶",
    }


class DynamicClient:
    def __init__(self, http: httpx.AsyncClient, cookie_getter=lambda: ""):
        self.http = http
        self.cookie_getter = cookie_getter
        self.lock = asyncio.Lock()
        self.last_request = 0.0

    async def request(self, path: str, params: dict) -> dict:
        async with self.lock:
            await asyncio.sleep(max(0, 3 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                # Credentials only accompany this fixed API host, never CDN images.
                cookie = self.cookie_getter()
                response = await self.http.get(
                    API_ROOT + path,
                    params=params,
                    headers={"Cookie": cookie} if cookie else {},
                )
                if response.status_code in {401, 403, 412, 429}:
                    raise BilibiliError(
                        f"动态接口受限（HTTP {response.status_code}），请稍后重试或在 Web 配置有效的 Bilibili Cookie"
                    )
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise BilibiliError("动态接口请求失败，稍后自动重试") from exc
            if not isinstance(payload, dict) or payload.get("code") != 0:
                code = payload.get("code") if isinstance(payload, dict) else "?"
                raise BilibiliError(
                    f"动态接口返回错误（{code}），请检查登录状态或稍后重试"
                )
            if not isinstance(payload.get("data"), dict):
                raise BilibiliError("动态接口数据不完整")
            return payload["data"]

    async def creator(self, uid: str) -> dict:
        data = await self.request("/x/web-interface/card", {"mid": parse_uid(uid)})
        card = data.get("card") or {}
        if str(card.get("mid")) != uid or not card.get("name"):
            raise BilibiliError("没有找到这个 UP 主")
        return {
            "uid": uid,
            "name": str(card["name"]),
            "avatar": str(card.get("face") or ""),
        }

    async def feed(
        self, uid: str, known: set[str], first_page_only=False
    ) -> tuple[list[dict], bool]:
        items, offset, offsets = {}, "", set()
        for _ in range(1 if first_page_only else 5):
            data = await self.request(
                "/x/polymer/web-dynamic/v1/feed/space",
                {"host_mid": uid, "offset": offset, "features": "itemOpusStyle"},
            )
            if not isinstance(data.get("items"), list):
                raise BilibiliError("动态列表缺失，未推进检查进度")
            page = [
                normalize_dynamic(raw, uid)
                for raw in data["items"]
                if isinstance(raw, dict)
            ]
            page = [item for item in page if item]
            items.update((item["id"], item) for item in page)
            if (
                first_page_only
                or not data.get("has_more")
                or any(item["id"] in known and not item["pinned"] for item in page)
            ):
                return sorted(
                    items.values(), key=lambda x: (x["published"], int(x["id"]))
                ), False
            offset = str(data.get("offset") or "")
            if not offset or offset in offsets:
                break
            offsets.add(offset)
        return sorted(
            items.values(), key=lambda x: (x["published"], int(x["id"]))
        ), True
