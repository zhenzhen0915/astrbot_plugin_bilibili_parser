"""Download one Bilibili video part into AstrBot's plugin data directory."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from .parser import VideoRef

# Bilibili serves media from rotating PCDN nodes. A single node can answer 503
# for a video while its siblings serve the same file, and the extractor resolves
# the node once per call. Re-running the extraction mints a new node, so a failed
# media transfer is retried with a fresh URL instead of the dead node's URL.
# Each attempt is cheap because a dead node rejects the request in well under a
# second, so failures cost retry time, not download traffic.
DOWNLOAD_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2
RETRY_BACKOFF_CAP_SECONDS = 6


class DownloadError(Exception):
    def __init__(self, message: str, *, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def _is_transient_node_error(exc: BaseException) -> bool:
    """Report whether an error looks like a transient media-node rejection.

    Bilibili's PCDN nodes answer 502/503/504 on their own and the extractor
    reports those as a bare HTTP error. Such failures usually clear when the
    URL is minted again, so they are worth retrying.

    Args:
        exc: Exception raised by yt-dlp while transferring the media.

    Returns:
        True when the error is a transient node rejection worth retrying.
    """
    text = str(exc)
    if "503" in text or "Service Unavailable" in text:
        return True
    if "502" in text or "504" in text:
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("timed out", "timeout", "connection reset", "connection aborted")
    )


def cleanup_cache(cache_dir: Path, max_age_hours: int) -> None:
    if not cache_dir.is_dir():
        return
    cutoff = time.time() - max(1, max_age_hours) * 3600
    for path in cache_dir.glob("bili_*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def _download(ref: VideoRef, cache_dir: Path, max_bytes: int) -> Path:
    if shutil.which("ffmpeg") is None:
        raise DownloadError("未找到 ffmpeg，无法合并视频和音频", permanent=True)
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError as YtDlpDownloadError
    except ImportError as exc:
        raise DownloadError("未安装 yt-dlp", permanent=True) from exc

    cache_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"bili_{ref.video_id}_{ref.page}_{uuid.uuid4().hex[:12]}"
    oversized = False

    def limit_hook(progress: dict) -> None:
        nonlocal oversized
        if int(progress.get("downloaded_bytes") or 0) > max_bytes:
            oversized = True
            raise DownloadError("视频超过下载大小上限", permanent=True)

    options = {
        "format": "bestvideo[height<=480][vcodec^=avc1][ext=mp4]+bestaudio[acodec^=mp4a][ext=m4a]/best[height<=480][vcodec^=avc1][ext=mp4]",
        "merge_output_format": "mp4",
        "outtmpl": str(cache_dir / (prefix + ".%(ext)s")),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 15,
        "max_filesize": max_bytes,
        "progress_hooks": [limit_hook],
    }
    url = f"https://www.bilibili.com/video/{ref.video_id}?p={ref.page}"
    target = cache_dir / f"{prefix}.mp4"
    last_error: Exception | None = None
    try:
        # yt-dlp resolves the media URL once per call, so every attempt re-runs
        # the extraction to obtain a different CDN node.
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                with YoutubeDL(options) as ydl:
                    ydl.extract_info(url, download=True)
                if not target.is_file():
                    raise DownloadError(
                        "下载结果不是可发送的 MP4 文件，可能超过大小上限",
                        permanent=True,
                    )
                if target.stat().st_size > max_bytes:
                    raise DownloadError("视频超过下载大小上限", permanent=True)
                return target
            except Exception as exc:
                if oversized:
                    raise DownloadError("视频超过下载大小上限", permanent=True) from exc
                if isinstance(exc, DownloadError):
                    raise
                last_error = exc
                if (
                    not isinstance(exc, YtDlpDownloadError)
                    or not _is_transient_node_error(exc)
                    or attempt == DOWNLOAD_ATTEMPTS
                ):
                    break
                time.sleep(
                    min(RETRY_BACKOFF_SECONDS * attempt, RETRY_BACKOFF_CAP_SECONDS)
                )
        if isinstance(last_error, YtDlpDownloadError):
            raise DownloadError("视频下载失败，可能需要登录或当前画质不可用") from (
                last_error
            )
        raise DownloadError("视频下载或合并失败") from last_error
    finally:
        if not target.is_file():
            for path in cache_dir.glob(prefix + "*"):
                if path.is_file():
                    path.unlink(missing_ok=True)


async def download_video(ref: VideoRef, cache_dir: Path, max_video_mb: int) -> Path:
    max_bytes = max(1, max_video_mb) * 1024 * 1024
    return await asyncio.to_thread(_download, ref, cache_dir, max_bytes)
