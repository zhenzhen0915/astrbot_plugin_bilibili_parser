"""Live-room polling, durable sessions and shared notification payloads."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from .bilibili_client import BilibiliError

CHINA = timezone(timedelta(hours=8))


def stamp(value):
    return (
        datetime.fromtimestamp(value, CHINA).strftime("%Y-%m-%d %H:%M:%S")
        if value
        else "—"
    )


def duration(value):
    seconds = max(0, int(value))
    return f"{seconds // 3600}小时{seconds % 3600 // 60:02d}分{seconds % 60:02d}秒"


def migrate_live(db):
    with db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(subscriptions)")}
        for name in ("live_notify", "live_record"):
            if name not in columns:
                db.execute(
                    f"ALTER TABLE subscriptions ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        db.execute("""CREATE TABLE IF NOT EXISTS live_state (
            uid TEXT PRIMARY KEY, session_id INTEGER, checked REAL NOT NULL DEFAULT 0,
            next_check REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', offline_since REAL NOT NULL DEFAULT 0)""")
        db.execute("""CREATE TABLE IF NOT EXISTS live_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL,
            room_id TEXT NOT NULL, started REAL NOT NULL, ended REAL,
            last_live REAL NOT NULL, start_estimated INTEGER NOT NULL,
            recorded INTEGER NOT NULL DEFAULT 0, payload TEXT NOT NULL,
            history TEXT NOT NULL DEFAULT '[]')""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS live_sessions_uid ON live_sessions(uid,started)"
        )
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            db.execute("PRAGMA user_version=2")


class LiveClient:
    def __init__(self, http, cookie_getter=lambda: ""):
        self.http = http
        self.cookie_getter = cookie_getter

    async def status(self, uid):
        headers = {"Referer": "https://live.bilibili.com/"}
        if self.cookie_getter():
            headers["Cookie"] = self.cookie_getter()
        response = await self.http.get(
            "https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
            params={"uids[]": uid},
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != 0:
            raise BilibiliError(f"直播接口暂不可用（{data.get('code')}）")
        raw = data.get("data")
        raw = raw.get(uid) if isinstance(raw, dict) else None
        # Missing entries are not evidence of ending a stream.
        if not isinstance(raw, dict) or str(raw.get("uid")) != uid:
            raise BilibiliError("直播接口未返回该主播的房间状态，请确认已开通直播间")
        try:
            status = int(raw["live_status"])
            room = int(raw["room_id"])
            started = int(raw.get("live_time") or 0)
        except (KeyError, ValueError, TypeError):
            raise BilibiliError("直播接口返回格式异常") from None
        if status not in {0, 1, 2} or room <= 0:
            raise BilibiliError("直播间状态无效")
        return {
            "uid": uid,
            "room_id": str(room),
            "live": status == 1,
            "started": started,
            "author": str(raw.get("uname") or uid),
            "avatar": str(raw.get("face") or ""),
            "title": str(raw.get("title") or "未设置标题")[:500],
            "area": " / ".join(
                filter(
                    None,
                    [
                        str(raw.get("area_v2_parent_name") or ""),
                        str(raw.get("area_v2_name") or ""),
                    ],
                )
            )[:200],
            "cover": str(raw.get("cover_from_user") or raw.get("keyframe") or ""),
        }


def live_item(session, ended=False):
    payload = json.loads(session["payload"])
    end = session["ended"]
    text = f"直播内容：{payload['title']}\n分区：{payload['area'] or '未分类'}\n开播：{stamp(session['started'])}"
    if ended:
        text += f"\n结束（检测估计）：{stamp(end)}\n直播时长（约）：{duration(end - session['started'])}"
        text += f"\n结束时间范围：{stamp(session['last_live'])} ～ {stamp(end)}"
    else:
        text += f"\n已播（约）：{duration(time.time() - session['started'])}"
    if session["start_estimated"]:
        text += "\n开播时间为首次检测时间，时长仅统计已观察部分。"
    return {
        "id": f"live:{session['uid']}:{session['id']}:{'end' if ended else 'start'}",
        "uid": session["uid"],
        "kind": "live_end" if ended else "live_start",
        "author": payload["author"],
        "avatar": payload["avatar"],
        "title": ("直播结束 · " if ended else "正在直播 · ") + payload["title"],
        "text": text,
        "images": [payload["cover"]] if payload["cover"] else [],
        "published": int(end or session["started"]),
        "stats": {},
        "url": "https://live.bilibili.com/" + session["room_id"],
    }


class LiveTracker:
    def __init__(self, service, http):
        self.service = service
        self.store = service.store
        self.client = LiveClient(http, lambda: service.cookie)
        self.error = ""

    def active(self, uid):
        return [
            s
            for s in self.store.subscriptions()
            if s["uid"] == uid
            and s["enabled"]
            and self.service.live_allowed(s)
            and (s["live_notify"] or s["live_record"])
        ]

    def ingest(self, uid, info, now=None):
        now = time.time() if now is None else now
        active = self.active(uid)
        if not active:
            return
        db = self.store.db
        with db:
            db.execute("INSERT OR IGNORE INTO live_state(uid) VALUES(?)", (uid,))
            state = self.store.rows("SELECT * FROM live_state WHERE uid=?", (uid,))[0]
            rows = self.store.rows(
                "SELECT * FROM live_sessions WHERE id=? AND ended IS NULL",
                (state["session_id"],),
            )
            session = rows[0] if rows else None
            valid_start = 946684800 < info["started"] <= now
            # A different authoritative start identifies a restart missed between polls.
            restarted = (
                session
                and info["live"]
                and valid_start
                and info["started"]
                > (
                    session["last_live"]
                    if session["start_estimated"]
                    else session["started"] + 2
                )
            )
            if session and (restarted or (not info["live"] and state["offline_since"])):
                ended = (
                    min(now, info["started"]) if restarted else state["offline_since"]
                )
                ended = max(session["last_live"], ended)
                db.execute(
                    "UPDATE live_sessions SET ended=? WHERE id=?",
                    (ended, session["id"]),
                )
                session["ended"] = ended
                db.execute(
                    "UPDATE deliveries SET status='cancelled',error='该场直播已结束' WHERE dynamic_id=? AND status IN ('pending','retry','preparing')",
                    (live_item(session)["id"],),
                )
                self.enqueue(session, active, ended=True)
                db.execute(
                    "UPDATE live_state SET session_id=NULL,offline_since=0 WHERE uid=?",
                    (uid,),
                )
                session = None
            if info["live"]:
                if session and session["start_estimated"] and valid_start:
                    db.execute(
                        "UPDATE live_sessions SET started=?,start_estimated=0 WHERE id=?",
                        (info["started"], session["id"]),
                    )
                if session is None:
                    started = info["started"] if valid_start else now
                    cursor = db.execute(
                        "INSERT INTO live_sessions(uid,room_id,started,last_live,start_estimated,recorded,payload) VALUES(?,?,?,?,?,?,?)",
                        (
                            uid,
                            info["room_id"],
                            started,
                            now,
                            int(not valid_start),
                            int(any(s["live_record"] for s in active)),
                            json.dumps(info, ensure_ascii=False),
                        ),
                    )
                    identity = cursor.lastrowid
                    db.execute(
                        "UPDATE live_state SET session_id=? WHERE uid=?",
                        (identity, uid),
                    )
                    session = self.store.rows(
                        "SELECT * FROM live_sessions WHERE id=?", (identity,)
                    )[0]
                history = json.loads(session["history"])
                content = {"title": info["title"], "area": info["area"]}
                if not history or any(history[-1][k] != v for k, v in content.items()):
                    history.append({"at": now, **content})
                db.execute(
                    "UPDATE live_sessions SET last_live=?,payload=?,history=?,recorded=max(recorded,?) WHERE id=?",
                    (
                        now,
                        json.dumps(info, ensure_ascii=False),
                        json.dumps(history[-200:], ensure_ascii=False),
                        int(any(s["live_record"] for s in active)),
                        session["id"],
                    ),
                )
                session = self.store.rows(
                    "SELECT * FROM live_sessions WHERE id=?", (session["id"],)
                )[0]
                self.enqueue(session, active)
                db.execute("UPDATE live_state SET offline_since=0 WHERE uid=?", (uid,))
            elif session and not state["offline_since"]:
                db.execute(
                    "UPDATE live_state SET offline_since=? WHERE uid=?", (now, uid)
                )
            db.execute(
                "UPDATE live_state SET checked=?,next_check=?,failures=0,error='' WHERE uid=?",
                (
                    now,
                    now + max(30, int(self.service.config.get("live_interval", 60))),
                    uid,
                ),
            )

    def enqueue(self, session, active, ended=False):
        item = live_item(session, ended)
        for sub in active:
            if sub["live_notify"]:
                self.store.enqueue(sub, item)

    def history(self, uid):
        now = time.time()
        rows = self.store.rows(
            "SELECT * FROM live_sessions WHERE uid=? AND recorded=1 AND (ended IS NULL OR ended>=?) ORDER BY started DESC",
            (uid, now - 30 * 86400),
        )
        result = []
        for row in rows:
            info = json.loads(row["payload"])
            result.append(
                {
                    "id": row["id"],
                    "start_ts": row["started"],
                    "end_ts": row["ended"],
                    "observed_end_ts": row["ended"]
                    if row["ended"] is not None
                    else row["last_live"],
                    "title": info["title"],
                    "area": info["area"],
                    "started": stamp(row["started"]),
                    "ended": stamp(row["ended"]) if row["ended"] else "尚未检测到下播",
                    "duration": duration(
                        (row["ended"] or row["last_live"]) - row["started"]
                    ),
                    "seconds": max(
                        0, (row["ended"] or row["last_live"]) - row["started"]
                    ),
                    "start_estimated": bool(row["start_estimated"]),
                    "last_live": stamp(row["last_live"]),
                    "history": [
                        {**h, "at": stamp(h["at"])} for h in json.loads(row["history"])
                    ],
                }
            )
        from .live_calendar import build_calendars

        return {
            "uid": uid,
            "rows": result,
            "generated": stamp(now),
            "total": duration(sum(row["seconds"] for row in result)),
            "calendars": build_calendars(result, now),
        }

    async def loop(self):
        while not self.service.stopping:
            try:
                active = [
                    s
                    for s in self.store.subscriptions()
                    if s["enabled"]
                    and self.service.live_allowed(s)
                    and (s["live_notify"] or s["live_record"])
                ]
                with self.store.db:
                    for sub in active:
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO live_state(uid) VALUES(?)",
                            (sub["uid"],),
                        )
                wanted = {s["uid"] for s in active}
                due = [
                    r
                    for r in self.store.rows(
                        "SELECT * FROM live_state WHERE next_check<=? ORDER BY next_check",
                        (time.time(),),
                    )
                    if r["uid"] in wanted
                ]
                if due:
                    row = due[0]
                    try:
                        info = await self.client.status(row["uid"])
                        self.ingest(row["uid"], info)
                    except Exception:
                        failures = row["failures"] + 1
                        # Do not expose transport errors that may contain credentials.
                        with self.store.db:
                            self.store.db.execute(
                                "UPDATE live_state SET checked=?,next_check=?,failures=?,error=? WHERE uid=?",
                                (
                                    time.time(),
                                    time.time() + min(3600, 60 * 2 ** min(failures, 5)),
                                    failures,
                                    "直播检查失败，将自动重试；请检查网络或主播直播间",
                                    row["uid"],
                                ),
                            )
                            self.store.db.execute(
                                "UPDATE live_state SET offline_since=0 WHERE uid=?",
                                (row["uid"],),
                            )
                self.error = ""
            except asyncio.CancelledError:
                raise
            except Exception:
                self.error = "直播采集任务异常，将自动重试"
            await asyncio.sleep(2)
