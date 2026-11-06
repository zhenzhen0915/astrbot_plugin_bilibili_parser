"""Small async client for public Bilibili video data."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlparse

import httpx

from .parser import VideoRef, from_video_url, supported_host


API_ROOT = "https://api.bilibili.com"
_MIXIN_TABLE = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


class BilibiliError(Exception):
    """A user-facing fetch or parsing failure."""


@dataclass(frozen=True)
class Comment:
    author: str
    text: str
    likes: int
    avatar_url: str = ""


@dataclass(frozen=True)
class VideoInfo:
    ref: VideoRef
    aid: int
    title: str
    description: str
    author: str
    duration: int
    cover_url: str
    stats: dict[str, int]
    tags: tuple[str, ...]
    comments: tuple[Comment, ...]
    comments_available: bool
    author_avatar_url: str = ""


def sign_wbi(
    params: dict[str, str | int], img_key: str, sub_key: str, now: int | None = None
) -> dict[str, str | int]:
    """Sign a public web API query using the current nav image keys."""
    raw = img_key + sub_key
    if len(raw) < 64:
        raise BilibiliError("评论签名信息不完整")
    mixin = "".join(raw[index] for index in _MIXIN_TABLE)[:32]
    signed: dict[str, str | int] = {
        **params,
        "wts": int(now if now is not None else time.time()),
    }
    clean = {key: re.sub(r"[!'()*]", "", str(value)) for key, value in signed.items()}
    query = urlencode(sorted(clean.items()))
    signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return signed


class BilibiliClient:
    def __init__(self, comment_sort: str = "likes") -> None:
        self.comment_sort = (
            comment_sort if comment_sort in {"likes", "hot"} else "likes"
        )
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0),
            follow_redirects=False,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Referer": "https://www.bilibili.com/",
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
            },
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def resolve_short_url(self, url: str) -> VideoRef:
        current = url
        for _ in range(4):
            if not supported_host(current):
                raise BilibiliError("短链接跳转到了不受支持的域名")
            ref = from_video_url(current)
            if ref:
                return ref
            parsed = urlparse(current)
            if parsed.hostname not in {"b23.tv", "www.b23.tv"}:
                raise BilibiliError("短链接不是视频地址")
            try:
                response = await self.http.get(current)
            except httpx.HTTPError as exc:
                raise BilibiliError("短链接请求失败") from exc
            if response.status_code not in {301, 302, 303, 307, 308}:
                raise BilibiliError("短链接没有返回视频跳转")
            location = response.headers.get("location", "")
            if not location:
                raise BilibiliError("短链接缺少跳转地址")
            current = urljoin(current, location)
        raise BilibiliError("短链接跳转次数过多")

    async def _get_data(
        self,
        path: str,
        params: dict[str, str | int],
        accepted_codes: tuple[int, ...] = (0,),
    ) -> object:
        try:
            response = await self.http.get(API_ROOT + path, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BilibiliError("Bilibili 接口请求失败") from exc
        if not isinstance(payload, dict) or payload.get("code") not in accepted_codes:
            code = payload.get("code") if isinstance(payload, dict) else "?"
            raise BilibiliError(f"Bilibili 接口返回错误（{code}）")
        return payload.get("data")

    async def _get_tags(self, aid: int) -> tuple[str, ...]:
        data = await self._get_data("/x/tag/archive/tags", {"aid": aid})
        if not isinstance(data, list):
            return ()
        names = [
            str(item.get("tag_name", "")).strip()
            for item in data
            if isinstance(item, dict)
        ]
        return tuple(name for name in names if name)[:20]

    async def _get_wbi_keys(self) -> tuple[str, str]:
        # Anonymous nav responses carry usable WBI keys despite code -101.
        data = await self._get_data(
            "/x/web-interface/nav", {}, accepted_codes=(0, -101)
        )
        if not isinstance(data, dict):
            raise BilibiliError("无法获取评论签名")
        wbi = data.get("wbi_img") or {}
        if not isinstance(wbi, dict):
            raise BilibiliError("无法获取评论签名")
        img = (
            urlparse(str(wbi.get("img_url", ""))).path.rsplit("/", 1)[-1].split(".")[0]
        )
        sub = (
            urlparse(str(wbi.get("sub_url", ""))).path.rsplit("/", 1)[-1].split(".")[0]
        )
        return img, sub

    async def _get_comments(self, aid: int) -> tuple[Comment, ...]:
        # First try the lightweight first page used by yt-dlp's Bilibili extractor.
        try:
            data = await self._get_data(
                "/x/v2/reply", {"pn": 1, "ps": 20, "oid": aid, "type": 1, "sort": 2}
            )
        except BilibiliError:
            img, sub = await self._get_wbi_keys()
            params = sign_wbi(
                {"type": 1, "oid": aid, "mode": 3, "next": 0, "web_location": 1315875},
                img,
                sub,
            )
            data = await self._get_data("/x/v2/reply/wbi/main", params)
        if not isinstance(data, dict):
            return ()
        # Pinned entries are not necessarily popular. Only use ranked lists.
        replies = []
        for field in ("hots", "replies"):
            if isinstance(data.get(field), list):
                replies.extend(data[field])
        comments: list[Comment] = []
        seen = set()
        for reply in replies:
            if not isinstance(reply, dict):
                continue
            member = reply.get("member") or {}
            content = reply.get("content") or {}
            if not isinstance(member, dict) or not isinstance(content, dict):
                continue
            message = str(content.get("message", "")).strip()
            identity = str(reply.get("rpid_str") or reply.get("rpid") or "")
            if not identity:
                identity = (str(member.get("mid") or member.get("uname")), message)
            if message and identity not in seen:
                seen.add(identity)
                try:
                    likes = max(0, int(reply.get("like") or 0))
                except (TypeError, ValueError):
                    likes = 0
                comments.append(
                    Comment(
                        str(member.get("uname", "匿名")),
                        message,
                        likes,
                        str(member.get("avatar") or ""),
                    )
                )
        if self.comment_sort == "likes":
            comments.sort(key=lambda comment: comment.likes, reverse=True)
        return tuple(comments[:2])

    async def get_video(self, ref: VideoRef) -> VideoInfo:
        query_key = "bvid" if ref.video_id.startswith("BV") else "aid"
        query_value = ref.video_id if query_key == "bvid" else ref.video_id[2:]
        data = await self._get_data("/x/web-interface/view", {query_key: query_value})
        if not isinstance(data, dict) or not data.get("bvid") or not data.get("aid"):
            raise BilibiliError("视频不存在或暂时无法访问")
        pages = data.get("pages") or []
        if isinstance(pages, list) and ref.page > len(pages):
            raise BilibiliError("视频分 P 不存在")
        normalized_ref = VideoRef(str(data["bvid"]), ref.page)
        aid = int(data["aid"])
        tag_result, comment_result = await asyncio.gather(
            self._get_tags(aid), self._get_comments(aid), return_exceptions=True
        )
        stats = data.get("stat") or {}
        owner = data.get("owner") or {}
        if not isinstance(stats, dict):
            stats = {}
        if not isinstance(owner, dict):
            owner = {}
        cover_url = str(data.get("pic") or "")
        if cover_url.startswith("//"):
            cover_url = "https:" + cover_url
        elif cover_url.startswith("http://") and (
            urlparse(cover_url).hostname or ""
        ).endswith(".hdslb.com"):
            cover_url = "https://" + cover_url[7:]
        stat_keys = ("view", "danmaku", "reply", "favorite", "coin", "share", "like")
        return VideoInfo(
            ref=normalized_ref,
            aid=aid,
            title=str(data.get("title") or "未命名视频"),
            description=str(data.get("desc") or ""),
            author=str(owner.get("name") or "未知 UP 主"),
            duration=int(data.get("duration") or 0),
            cover_url=cover_url,
            stats={key: int(stats.get(key) or 0) for key in stat_keys},
            tags=tag_result if isinstance(tag_result, tuple) else (),
            comments=comment_result if isinstance(comment_result, tuple) else (),
            comments_available=isinstance(comment_result, tuple),
            author_avatar_url=str(owner.get("face") or ""),
        )
