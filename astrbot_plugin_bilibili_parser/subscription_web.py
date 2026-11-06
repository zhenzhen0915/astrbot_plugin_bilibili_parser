"""Authenticated native AstrBot Plugin Page endpoints."""

import json

from .bilibili_client import BilibiliError
from .web_compat import error_response, json_response, request


class SubscriptionWeb:
    def __init__(self, context, service):
        self.service = service
        base = "/astrbot_plugin_bilibili_parser/subscriptions"
        context.register_web_api(
            base + "/state", self.state, ["GET"], "Subscription state"
        )
        context.register_web_api(
            base + "/groups", self.groups, ["GET"], "Available QQ groups"
        )
        context.register_web_api(
            base + "/action", self.action, ["POST"], "Manage dynamic subscriptions"
        )

    async def state(self):
        if not request.username:
            return error_response("请登录 AstrBot 管理后台", status_code=401)
        result = self.service.snapshot()
        result["settings"] = self.service.settings()
        result["deliveries"] = self.service.store.rows(
            "SELECT id,uid,group_id,dynamic_id,test,status,stage,card_status,video_status,error,receipts,created,updated FROM deliveries ORDER BY id DESC LIMIT 100"
        )
        for row in result["deliveries"]:
            row["receipts"] = json.loads(row["receipts"])
        return json_response(result)

    async def groups(self):
        if not request.username:
            return error_response("请登录 AstrBot 管理后台", status_code=401)
        return json_response({"groups": await self.service.targets()})

    async def action(self):
        if not request.username:
            return error_response("请登录 AstrBot 管理后台", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式错误")
        actor = "web:" + request.username
        action = payload.get("action")
        try:
            result = {"saved": True}
            if action == "add":
                target = payload.get("target")
                if not isinstance(target, dict):
                    raise ValueError("请选择目标群")
                result = await self.service.add(
                    target,
                    payload.get("uid", ""),
                    payload.get("types", "all"),
                    payload.get("send_video", True),
                    actor,
                    live_notify=payload.get("live_notify", False),
                    live_record=payload.get("live_record", False),
                )
            elif action == "live_history":
                sub = self.service.store.get(int(payload["id"]))
                result = self.service.live.history(sub["uid"])
                result["name"] = sub["name"]
            elif action == "resolve":
                from .dynamic_client import parse_uid

                result = await self.service.client.creator(
                    parse_uid(payload.get("uid", ""))
                )
            elif action == "edit":
                changes = payload.get("changes", {})
                if not isinstance(changes, dict):
                    raise ValueError("设置格式错误")
                self.service.edit(
                    int(payload["id"]), int(payload["revision"]), changes, actor
                )
            elif action == "delete":
                self.service.store.remove(
                    int(payload["id"]), int(payload["revision"]), actor
                )
            elif action in {"check", "test"}:
                result = {
                    "task_id": self.service.request(int(payload["id"]), action, actor)
                }
            elif action == "retry":
                self.service.store.retry(int(payload["id"]), actor)
            elif action == "resolve_delivery":
                self.service.store.resolve_delivery(
                    int(payload["id"]), payload.get("outcome", ""), actor
                )
            elif action == "settings":
                values = payload.get("values", {})
                if not isinstance(values, dict):
                    raise ValueError("设置格式错误")
                self.service.save_settings(values, actor)
                result = self.service.settings()
            else:
                raise ValueError("不支持的操作")
            return json_response(result)
        except (ValueError, TypeError, KeyError, BilibiliError) as exc:
            return error_response(str(exc), status_code=400)
