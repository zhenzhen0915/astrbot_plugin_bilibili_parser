"""Build self-contained template data for AstrBot's native HTML renderer."""

from __future__ import annotations

import asyncio
import base64
from io import BytesIO
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import httpx
import qrcode

from .bilibili_client import VideoInfo
from .formatter import _compact, _count

ASSETS = Path(__file__).parent / "assets"
TEMPLATE = Path(__file__).parent / "templates" / "video_card.html"


def local_card_assets(url: str) -> dict:
    """Embed the two local images and an actual QR code for either card type."""
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=5, border=4
    )
    qr.add_data(url)
    qr.make(fit=True)
    output = BytesIO()
    qr.make_image(fill_color="#132132", back_color="white").save(output, format="PNG")
    return {
        "qr": "data:image/png;base64,"
        + base64.b64encode(output.getvalue()).decode("ascii"),
        "character": "data:image/png;base64,"
        + base64.b64encode((ASSETS / "character.png").read_bytes()).decode("ascii"),
        "background": "data:image/png;base64,"
        + base64.b64encode((ASSETS / "background.png").read_bytes()).decode("ascii"),
    }


async def build_dynamic_card_data(
    item: dict, http: httpx.AsyncClient, test=False
) -> dict:
    images = await asyncio.gather(
        _image_uri(http, item["avatar"]),
        *(_image_uri(http, url) for url in item["images"][:9]),
    )
    labels = {
        "video": "视频投稿",
        "draw": "图文动态",
        "word": "文字动态",
        "forward": "转发动态",
        "article": "专栏分享",
        "other": "动态更新",
    }
    return {
        **local_card_assets(item["url"]),
        "dynamic": True,
        "live": item["kind"] in {"live_start", "live_end"},
        "is_test": test,
        "title": _compact(item["title"] or labels.get(item["kind"], "动态更新"), 120),
        "author": item["author"],
        "author_initial": item["author"][:1],
        "author_avatar": images[0],
        "gallery": [image for image in images[1:] if image],
        "published": datetime.fromtimestamp(item["published"])
        .astimezone()
        .strftime("%Y-%m-%d %H:%M %Z"),
        "description": (
            item["text"].strip()[:1400]
            + ("…" if len(item["text"].strip()) > 1400 else "")
        )
        or "打开原动态查看完整内容。",
        "url": item["url"],
        "more_images": item.get("image_count", 0) > 9,
        "stats": [
            {"icon": icon, "label": label, "value": _count(item["stats"][key])}
            for key, icon, label in (
                ("like", "like", "点赞"),
                ("comment", "comment", "评论"),
                ("forward", "share", "转发"),
            )
            if key in item["stats"]
        ],
    }


async def _image_uri(http: httpx.AsyncClient, url: str) -> str:
    """Fetch a bounded Bilibili CDN image for embedding in the HTML.

    Args:
        http: Shared client with Bilibili request headers.
        url: Cover or avatar URL returned by Bilibili.

    Returns:
        An image data URI, or an empty string if the asset is unavailable.
    """
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not (parsed.hostname or "").endswith(
        ".hdslb.com"
    ):
        return ""
    url = parsed._replace(scheme="https").geturl()
    try:
        async with http.stream("GET", url) as response:
            response.raise_for_status()
            mime = response.headers.get("content-type", "").split(";")[0].strip()
            if mime not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                return ""
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > 8 * 1024 * 1024:
                    return ""
        return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")
    except httpx.HTTPError:
        return ""


async def build_card_data(info: VideoInfo, http: httpx.AsyncClient) -> dict:
    """Prepare the complete card, including real QR data and remote images.

    Args:
        info: Parsed video information and selected popular comments.
        http: Shared Bilibili HTTP client.

    Returns:
        Template variables containing no remote image dependencies.
    """
    comments = info.comments[:2]
    images = await asyncio.gather(
        _image_uri(http, info.cover_url),
        _image_uri(http, info.author_avatar_url),
        *(_image_uri(http, comment.avatar_url) for comment in comments),
    )
    metrics = [
        ("play", "view", "播放"),
        ("like", "like", "点赞"),
        ("coin", "coin", "投币"),
        ("star", "favorite", "收藏"),
        ("danmaku", "danmaku", "弹幕"),
        ("comment", "reply", "评论"),
    ]
    return {
        **local_card_assets(info.ref.url),
        "title": _compact(info.title, 120),
        "author": _compact(info.author, 40),
        "author_initial": info.author[:1] or "UP",
        "duration": f"{info.duration // 60:02d}:{info.duration % 60:02d}",
        "bvid": info.ref.video_id,
        "page": info.ref.page,
        "description": _compact(info.description, 800)
        if info.description.strip() not in {"", "-"}
        else "暂无简介",
        "cover": images[0],
        "author_avatar": images[1],
        "url": info.ref.url,
        "tags": [_compact(tag, 24) for tag in info.tags[:10]],
        "stats": [
            {"icon": icon, "label": label, "value": _count(info.stats.get(key, 0))}
            for icon, key, label in metrics
        ],
        "comments": [
            {
                "author": _compact(comment.author, 32),
                "initial": comment.author[:1] or "评",
                "text": _compact(comment.text, 300),
                "likes": _count(comment.likes),
                "avatar": images[index + 2],
            }
            for index, comment in enumerate(comments)
        ],
        "comments_empty": "暂无评论" if info.comments_available else "评论暂不可用",
    }
