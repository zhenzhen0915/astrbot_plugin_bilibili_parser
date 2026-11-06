"""QQ-friendly plain text formatting."""

from __future__ import annotations

import re

from .bilibili_client import VideoInfo


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(number: int) -> str:
    if number >= 100000000:
        return f"{number / 100000000:.1f}亿"
    return f"{number / 10000:.1f}万" if number >= 10000 else str(number)


def format_video(info: VideoInfo) -> str:
    duration = f"{info.duration // 60:02d}:{info.duration % 60:02d}"
    s = info.stats
    tags = "、".join(info.tags) if info.tags else "暂无"
    lines = [
        f"🎬 {_compact(info.title, 120)}",
        f"BV：{info.ref.video_id} · UP：{_compact(info.author, 40)} · 时长：{duration}",
        f"播放：{_count(s['view'])} · 弹幕：{_count(s['danmaku'])} · 点赞：{_count(s['like'])}",
        f"收藏：{_count(s['favorite'])} · 投币：{_count(s['coin'])} · 分享：{_count(s['share'])} · 评论：{_count(s['reply'])}",
        f"标签：{_compact(tags, 240)}",
        f"简介：{_compact(info.description, 800) if info.description else '暂无'}",
        "评论：",
    ]
    if info.comments:
        for index, comment in enumerate(info.comments, 1):
            lines.append(
                f"{index}. {_compact(comment.author, 32)}（赞 {_count(comment.likes)}）：{_compact(comment.text, 300)}"
            )
    else:
        lines.append("暂无评论" if info.comments_available else "评论暂不可用")
    if info.cover_url:
        lines.append(f"封面：{info.cover_url}")
    lines.append(f"链接：{info.ref.url}")
    return "\n".join(lines)
