"""Extract only supported Bilibili video references from chat text."""

from __future__ import annotations

import re
import json
from collections.abc import Mapping
from dataclasses import dataclass
from html import unescape
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree


_URL_RE = re.compile(r"https?://[^\s<>\[\]()\"']+", re.IGNORECASE)
_BV_RE = re.compile(r"(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}(?![0-9A-Za-z])", re.IGNORECASE)
_AV_RE = re.compile(r"(?<![0-9A-Za-z])av[1-9][0-9]*(?![0-9A-Za-z])", re.IGNORECASE)
_VIDEO_PATH_RE = re.compile(
    r"^/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)(?:/|$)", re.IGNORECASE
)
_BILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
_SHORT_HOSTS = {"b23.tv", "www.b23.tv"}
_CARD_URL_KEYS = {"url", "qqdocurl", "jumpurl", "jump_url", "targeturl", "actiondata"}
_MAX_CARD_SIZE = 65536


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    page: int = 1

    @property
    def url(self) -> str:
        suffix = f"?p={self.page}" if self.page != 1 else ""
        return f"https://www.bilibili.com/video/{self.video_id}{suffix}"


def supported_host(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and (parsed.hostname or "").lower() in (_BILI_HOSTS | _SHORT_HOSTS)
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


def from_video_url(url: str) -> VideoRef | None:
    if not supported_host(url):
        return None
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _BILI_HOSTS:
        return None
    match = _VIDEO_PATH_RE.match(parsed.path)
    if not match:
        return None
    raw_id = match.group(1)
    video_id = "BV" + raw_id[2:] if raw_id[:2].lower() == "bv" else "av" + raw_id[2:]
    raw_page = parse_qs(parsed.query).get("p", ["1"])[0]
    page = int(raw_page) if raw_page.isdigit() else 1
    return VideoRef(video_id, max(1, min(page, 999)))


def find_reference(text: str) -> VideoRef | str | None:
    """Return VideoRef, a b23 short URL, or None. Never fetch arbitrary URLs."""
    url_matches = list(_URL_RE.finditer(text))
    for match in url_matches:
        url = match.group(0).rstrip(".,!?;:，。！？；：")
        if not supported_host(url):
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https":
            continue
        if host in _SHORT_HOSTS:
            return url
        ref = from_video_url(url)
        if ref:
            return ref
    # Do not treat an ID embedded in an unrelated URL as a bare ID.
    without_urls = _URL_RE.sub(" ", text)
    for pattern in (_BV_RE, _AV_RE):
        match = pattern.search(without_urls)
        if match:
            raw = match.group(0)
            return VideoRef(("BV" if pattern is _BV_RE else "av") + raw[2:])
    return None


def find_card_reference(payload: object) -> VideoRef | str | None:
    """Extract trusted link fields from a bounded QQ JSON or XML share card.

    Args:
        payload: OneBot's JSON/XML data string or an already decoded JSON object.

    Returns:
        A supported video reference or short URL. Titles and embedded replies
        are not interpreted as bare video IDs.
    """
    try:
        serialized = payload if isinstance(payload, str) else json.dumps(payload)
        if len(serialized) > _MAX_CARD_SIZE:
            return None
        if isinstance(payload, str):
            payload = payload.strip()
            if payload.startswith("<"):
                if "<!DOCTYPE" in payload.upper() or "<!ENTITY" in payload.upper():
                    return None
                root = ElementTree.fromstring(payload)
                payload = [
                    {
                        **node.attrib,
                        **(
                            {"url": node.text}
                            if node.tag.lower() in _CARD_URL_KEYS
                            else {}
                        ),
                    }
                    for node in root.iter()
                ]
            else:
                payload = json.loads(payload)
    except (ValueError, TypeError, RecursionError, ElementTree.ParseError):
        return None

    pending = [(payload, "", 0)]
    for _ in range(512):
        if not pending:
            break
        value, key, depth = pending.pop(0)
        if depth > 12:
            continue
        if isinstance(value, dict):
            pending.extend((v, str(k).lower(), depth + 1) for k, v in value.items())
        elif isinstance(value, list):
            pending.extend((v, key, depth + 1) for v in value)
        elif isinstance(value, str) and key in _CARD_URL_KEYS:
            url = unescape(value).replace("\\/", "/").strip()
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("http://"):
                url = "https://" + url[7:]
            elif not url.startswith("https://"):
                url = "https://" + url
            if supported_host(url):
                ref = from_video_url(url)
                if ref:
                    return ref
                if (urlparse(url).hostname or "").lower() in _SHORT_HOSTS:
                    return url
    return None


def find_message_reference(
    text: str, components: list, raw_message: object, parse_share_cards: bool = True
) -> VideoRef | str | None:
    """Read text plus OneBot cards, including XML omitted by AstrBot's adapter."""
    found = find_reference(text)
    if found or not parse_share_cards:
        return found
    segments = list(components or [])
    if isinstance(raw_message, Mapping):
        raw_parts = raw_message.get("message", [])
        if isinstance(raw_parts, list):
            segments = raw_parts + segments
        elif isinstance(raw_parts, str):
            for kind, data in re.findall(r"\[CQ:(json|xml),data=(.*?)\]", raw_parts):
                segments.append({"type": kind, "data": {"data": unescape(data)}})
    for segment in segments[:128]:
        if isinstance(segment, dict):
            kind = segment.get("type")
            data = segment.get("data", {})
            payload = data.get("data") if isinstance(data, dict) else None
        else:
            kind = getattr(segment, "type", "")
            kind = getattr(kind, "value", kind)
            payload = getattr(segment, "data", None)
        if str(kind).lower() in {"json", "xml"}:
            found = find_card_reference(payload)
            if found:
                return found
    return None
