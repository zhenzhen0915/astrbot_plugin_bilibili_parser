"""Transactional subscriptions, feed baselines and per-target delivery stages."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class SubscriptionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        if self.db.execute("PRAGMA user_version").fetchone()[0] > 3:
            self.db.close()
            raise RuntimeError("订阅数据库来自更新版本，请升级插件后加载")
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS creators (
                uid TEXT PRIMARY KEY, name TEXT NOT NULL, avatar TEXT NOT NULL DEFAULT '',
                checked REAL NOT NULL DEFAULT 0, success REAL NOT NULL DEFAULT 0,
                next_check REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', gap INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL, group_name TEXT NOT NULL, uid TEXT NOT NULL,
                types TEXT NOT NULL, send_video INTEGER NOT NULL DEFAULT 1,
                enabled INTEGER NOT NULL DEFAULT 1, blocked INTEGER NOT NULL DEFAULT 0,
                initialized INTEGER NOT NULL DEFAULT 0, baseline REAL NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                UNIQUE(platform, bot_id, group_id, uid));
            CREATE TABLE IF NOT EXISTS dynamic_items (
                id TEXT PRIMARY KEY, uid TEXT NOT NULL, published INTEGER NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS items_uid ON dynamic_items(uid);
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER, platform TEXT NOT NULL,
                bot_id TEXT NOT NULL, group_id TEXT NOT NULL, uid TEXT NOT NULL,
                dynamic_id TEXT NOT NULL, dedup_key TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL, test INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending', stage TEXT NOT NULL DEFAULT 'card',
                card_status TEXT NOT NULL DEFAULT 'pending', video_status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', receipts TEXT NOT NULL DEFAULT '[]',
                created REAL NOT NULL, updated REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS deliveries_ready ON deliveries(status,next_attempt);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT, subscription_id INTEGER NOT NULL,
                kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                detail TEXT NOT NULL, created REAL NOT NULL);
        """)
        from .live import migrate_live

        migrate_live(self.db)
        with self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS qq_bindings (
                platform TEXT NOT NULL, bot_id TEXT NOT NULL, group_id TEXT NOT NULL,
                qq_id TEXT NOT NULL, uid TEXT NOT NULL, updated REAL NOT NULL,
                PRIMARY KEY(platform, bot_id, group_id, qq_id))""")
            self.db.execute("PRAGMA user_version=3")
        with self.db:
            # A crash while preparing is safe to retry; an interrupted send is ambiguous.
            self.db.execute(
                "UPDATE deliveries SET status='pending' WHERE status='preparing'"
            )
            self.db.execute(
                "UPDATE deliveries SET status='uncertain', error='进程在发送期间退出，请核对群记录后处理' WHERE status='sending'"
            )
            self.db.execute(
                "UPDATE requests SET status='pending' WHERE status='running'"
            )

    def rows(self, query: str, params=()) -> list[dict]:
        return [dict(row) for row in self.db.execute(query, params).fetchall()]

    def audit(self, actor: str, action: str, detail: str):
        self.db.execute(
            "INSERT INTO audit_log(actor,action,detail,created) VALUES(?,?,?,?)",
            (actor[:100], action, detail[:500], time.time()),
        )

    def subscriptions(self, target=None) -> list[dict]:
        query = "SELECT s.*, c.name,c.avatar,c.checked,c.success,c.error,c.gap FROM subscriptions s JOIN creators c ON c.uid=s.uid"
        params = ()
        if target:
            query += " WHERE s.platform=? AND s.bot_id=? AND s.group_id=?"
            params = (target["platform"], target["bot_id"], target["group_id"])
        result = self.rows(query + " ORDER BY s.id DESC", params)
        for row in result:
            row["types"] = json.loads(row["types"])
        return result

    def get(self, subscription_id: int) -> dict:
        for row in self.subscriptions():
            if row["id"] == subscription_id:
                return row
        raise ValueError("订阅不存在或已取消")

    def bound_uid(self, target: dict, qq_id: str) -> str | None:
        rows = self.rows(
            "SELECT uid FROM qq_bindings WHERE platform=? AND bot_id=? AND group_id=? AND qq_id=?",
            (target["platform"], target["bot_id"], target["group_id"], qq_id),
        )
        return rows[0]["uid"] if rows else None

    def bind_qq(self, target: dict, qq_id: str, uid: str, actor: str):
        if not qq_id.isdigit() or not uid.isdigit():
            raise ValueError("QQ 号或主播 UID 格式不正确")
        if not any(row["uid"] == uid for row in self.subscriptions(target)):
            raise ValueError("请先在本群订阅该主播，再绑定群成员")
        with self.db:
            self.db.execute(
                "INSERT INTO qq_bindings(platform,bot_id,group_id,qq_id,uid,updated) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(platform,bot_id,group_id,qq_id) DO UPDATE SET uid=excluded.uid,updated=excluded.updated",
                (
                    target["platform"],
                    target["bot_id"],
                    target["group_id"],
                    qq_id,
                    uid,
                    time.time(),
                ),
            )
            self.audit(actor, "bind_qq", f"{target['group_id']}:{qq_id}:{uid}")

    def unbind_qq(self, target: dict, qq_id: str, actor: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM qq_bindings WHERE platform=? AND bot_id=? AND group_id=? AND qq_id=?",
                (target["platform"], target["bot_id"], target["group_id"], qq_id),
            )
            if cursor.rowcount:
                self.audit(actor, "unbind_qq", f"{target['group_id']}:{qq_id}")
        return bool(cursor.rowcount)

    def add(
        self, target: dict, creator: dict, types: list[str], video: bool, actor: str
    ) -> tuple[int, bool]:
        now = time.time()
        with self.db:
            self.db.execute(
                "INSERT INTO creators(uid,name,avatar) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name,avatar=excluded.avatar",
                (creator["uid"], creator["name"], creator["avatar"]),
            )
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO subscriptions(platform,bot_id,group_id,group_name,uid,types,send_video,created) VALUES(?,?,?,?,?,?,?,?)",
                (
                    target["platform"],
                    target["bot_id"],
                    target["group_id"],
                    target.get("group_name", target["group_id"]),
                    creator["uid"],
                    json.dumps(types),
                    int(video),
                    now,
                ),
            )
            created = cursor.rowcount == 1
            row = self.db.execute(
                "SELECT id FROM subscriptions WHERE platform=? AND bot_id=? AND group_id=? AND uid=?",
                (
                    target["platform"],
                    target["bot_id"],
                    target["group_id"],
                    creator["uid"],
                ),
            ).fetchone()
            if created:
                self.db.execute(
                    "UPDATE creators SET next_check=0 WHERE uid=?", (creator["uid"],)
                )
                self.audit(actor, "subscribe", str(row["id"]))
        return row["id"], created

    def edit(self, subscription_id: int, revision: int, changes: dict, actor: str):
        row = self.get(subscription_id)
        if row["revision"] != revision:
            raise ValueError("此订阅已被其他操作修改，请刷新后重试")
        allowed = {
            k: v
            for k, v in changes.items()
            if k in {"enabled", "types", "send_video", "live_notify", "live_record"}
        }
        if not allowed:
            raise ValueError("没有可保存的变更")
        if "types" in allowed:
            allowed["types"] = json.dumps(allowed["types"])
        if "enabled" in allowed:
            allowed.update(initialized=0, baseline=0)
        with self.db:
            sql = ",".join(f"{key}=?" for key in allowed)
            cursor = self.db.execute(
                f"UPDATE subscriptions SET {sql}, revision=revision+1 WHERE id=? AND revision=?",
                (*allowed.values(), subscription_id, revision),
            )
            if cursor.rowcount != 1:
                raise ValueError("订阅已改变，请刷新")
            if "enabled" in allowed:
                self.db.execute(
                    "UPDATE deliveries SET status='cancelled',error='订阅设置已改变' WHERE subscription_id=? AND status IN ('pending','retry','preparing')",
                    (subscription_id,),
                )
            elif "types" in allowed:
                self.db.execute(
                    "UPDATE deliveries SET status='cancelled',error='动态类型已改变' WHERE subscription_id=? AND dynamic_id NOT LIKE 'live:%' AND status IN ('pending','retry','preparing')",
                    (subscription_id,),
                )
            if allowed.get("live_notify") is False:
                self.db.execute(
                    "UPDATE deliveries SET status='cancelled',error='直播通知已关闭' WHERE subscription_id=? AND dynamic_id LIKE 'live:%' AND status IN ('pending','retry','preparing')",
                    (subscription_id,),
                )
            self.db.execute(
                "UPDATE live_state SET next_check=0 WHERE uid=?", (row["uid"],)
            )
            self.db.execute(
                "UPDATE creators SET next_check=0 WHERE uid=?", (row["uid"],)
            )
            self.audit(actor, "edit", str(subscription_id))

    def remove(self, subscription_id: int, revision: int, actor: str):
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM subscriptions WHERE id=? AND revision=?",
                (subscription_id, revision),
            )
            if not cursor.rowcount:
                raise ValueError("订阅已改变，请刷新")
            self.db.execute(
                "UPDATE deliveries SET status='cancelled',error='订阅已取消' WHERE subscription_id=? AND status IN ('pending','retry','preparing')",
                (subscription_id,),
            )
            self.audit(actor, "unsubscribe", str(subscription_id))

    def sync_allowed(self, allowed):
        with self.db:
            for row in self.subscriptions():
                blocked = int(not allowed(row))
                if blocked != row["blocked"]:
                    self.db.execute(
                        "UPDATE subscriptions SET blocked=?,initialized=0,baseline=0 WHERE id=?",
                        (blocked, row["id"]),
                    )
                    self.db.execute(
                        "UPDATE deliveries SET status='cancelled',error='群过滤或动态总开关已关闭' WHERE subscription_id=? AND dynamic_id NOT LIKE 'live:%' AND status IN ('pending','retry','preparing')",
                        (row["id"],),
                    )

    def known(self, uid: str) -> set[str]:
        return {
            r["id"]
            for r in self.rows("SELECT id FROM dynamic_items WHERE uid=?", (uid,))
        }

    def enqueue(
        self, sub: dict, item: dict, *, test=False, token="", status="pending"
    ) -> int:
        key = f"{sub['platform']}:{sub['bot_id']}:{sub['group_id']}:{item['id']}" + (
            ":test:" + token if test else ""
        )
        now = time.time()
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO deliveries(subscription_id,platform,bot_id,group_id,uid,dynamic_id,dedup_key,payload,test,status,video_status,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sub["id"],
                sub["platform"],
                sub["bot_id"],
                sub["group_id"],
                sub["uid"],
                item["id"],
                key,
                json.dumps(item, ensure_ascii=False),
                int(test),
                status,
                "pending"
                if sub["send_video"]
                and item.get("kind") == "video"
                and item.get("bvid")
                else "skipped",
                now,
                now,
            ),
        )
        return cursor.lastrowid if cursor.rowcount else 0

    def ingest(self, uid: str, items: list[dict], gap: bool, next_check: float):
        """Commit new items, target jobs and the feed checkpoint together.

        Args:
            uid: Creator identity.
            items: Bounded normalized feed result.
            gap: Whether the pagination limit was reached.
            next_check: Next scheduled check time.
        """
        now, known = time.time(), self.known(uid)
        new = [item for item in items if item["id"] not in known]
        with self.db:
            for item in items:
                self.db.execute(
                    "INSERT INTO dynamic_items VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                    (
                        item["id"],
                        uid,
                        item["published"],
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
            for sub in self.subscriptions():
                if sub["uid"] != uid or not sub["enabled"] or sub["blocked"]:
                    continue
                if not sub["initialized"]:
                    self.db.execute(
                        "UPDATE subscriptions SET initialized=1,baseline=? WHERE id=?",
                        (int(now), sub["id"]),
                    )
                    continue
                candidates = [
                    item
                    for item in new
                    if item["published"] >= sub["baseline"]
                    and item["published"] >= now - 86400
                    and item["kind"] in sub["types"]
                ]
                backlog = self.db.execute(
                    "SELECT count(*) FROM deliveries WHERE platform=? AND bot_id=? AND group_id=? AND test=0 AND status IN ('pending','retry','preparing','sending')",
                    (sub["platform"], sub["bot_id"], sub["group_id"]),
                ).fetchone()[0]
                limit = max(0, 10 - backlog)
                chosen = (
                    {item["id"] for item in candidates[-limit:]} if limit else set()
                )
                for item in candidates:
                    identity = self.enqueue(
                        sub,
                        item,
                        status="pending" if item["id"] in chosen else "skipped",
                    )
                    if identity and item["id"] not in chosen:
                        self.db.execute(
                            "UPDATE deliveries SET error='超过每群 10 条待推送上限' WHERE id=?",
                            (identity,),
                        )
            self.db.execute(
                "UPDATE creators SET checked=?,success=?,next_check=?,failures=0,error='',gap=? WHERE uid=?",
                (now, now, next_check, int(gap), uid),
            )

    def request(self, sub: dict, kind: str, actor: str) -> int:
        now = time.time()
        last = self.db.execute(
            "SELECT created FROM requests WHERE subscription_id=? ORDER BY id DESC LIMIT 1",
            (sub["id"],),
        ).fetchone()
        if last and now - last[0] < 60:
            raise ValueError("该订阅刚刚执行过检查或测试，请等 60 秒后重试")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO requests(subscription_id,kind,created,updated) VALUES(?,?,?,?)",
                (sub["id"], kind, now, now),
            )
            self.audit(actor, kind, str(sub["id"]))
        return cursor.lastrowid

    def retry(self, delivery_id: int, actor: str):
        row = self.rows("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not row or row[0]["status"] != "failed":
            raise ValueError("仅明确失败的任务可重试；结果不明的发送请先核对群记录")
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET status='retry',attempts=0,next_attempt=0,error='',updated=? WHERE id=?",
                (time.time(), delivery_id),
            )
            self.audit(actor, "retry", str(delivery_id))

    def resolve_delivery(self, delivery_id: int, outcome: str, actor: str):
        if outcome not in {"sent", "not_sent"}:
            raise ValueError("请确认该阶段已经发送或确实没有发送")
        rows = self.rows("SELECT * FROM deliveries WHERE id=?", (delivery_id,))
        if not rows or rows[0]["status"] != "uncertain":
            raise ValueError("仅待核对的发送可以人工确认，请刷新记录")
        row = rows[0]
        stage = row["stage"]
        if stage not in {"card", "video"}:
            raise ValueError("发送阶段无效")
        receipts = json.loads(row["receipts"])
        if outcome == "sent":
            receipts.append({"stage": stage, "message_id": "人工确认已发送"})
        stage_status = "sent" if outcome == "sent" else "pending"
        done = outcome == "sent" and (
            stage == "video" or row["video_status"] in {"sent", "skipped"}
        )
        with self.db:
            self.db.execute(
                f"UPDATE deliveries SET status=?,{stage}_status=?,attempts=0,next_attempt=0,error='',receipts=?,updated=? WHERE id=? AND status='uncertain'",
                (
                    "done" if done else "pending",
                    stage_status,
                    json.dumps(receipts),
                    time.time(),
                    delivery_id,
                ),
            )
            self.audit(actor, "resolve_delivery", f"{delivery_id}:{stage}:{outcome}")

    def close(self):
        self.db.close()

    def prune(self):
        cutoff = time.time() - 30 * 86400
        with self.db:
            self.db.execute(
                "DELETE FROM live_sessions WHERE ended IS NOT NULL AND ended<?",
                (cutoff,),
            )
            self.db.execute(
                "UPDATE dynamic_items SET payload='{}' WHERE published<? AND payload<>'{}'",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM deliveries WHERE updated<? AND status IN ('done','cancelled','skipped','failed')",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM requests WHERE updated<? AND status IN ('done','failed')",
                (cutoff,),
            )
            self.db.execute("DELETE FROM audit_log WHERE created<?", (cutoff,))
