"""Verify plugin Web helpers on both AstrBot dashboard backends."""

import builtins
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from quart import Quart, g

from astrbot_plugin_bilibili_parser.web_compat import WEB_BACKEND


class WebCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    def test_current_astrbot_uses_native_web_api(self):
        self.assertEqual(WEB_BACKEND, "fastapi")

    async def test_legacy_quart_fallback_loads_without_astrbot_api_web(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "astrbot_plugin_bilibili_parser"
            / "web_compat.py"
        )
        spec = importlib.util.spec_from_file_location("legacy_web_compat", path)
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def import_without_new_web(name, *args, **kwargs):
            if name == "astrbot.api.web":
                raise ModuleNotFoundError(
                    "No module named 'astrbot.api.web'", name="astrbot.api.web"
                )
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_new_web):
            spec.loader.exec_module(module)

        self.assertEqual(module.WEB_BACKEND, "quart")
        app = Quart(__name__)
        async with app.test_request_context(
            "/api/plug/plugin/subscriptions/action",
            method="POST",
            json={"action": "status"},
        ):
            g.username = "admin"
            self.assertEqual(module.request.username, "admin")
            self.assertEqual(
                await module.request.json(default={}), {"action": "status"}
            )
            response, status = module.json_response({"saved": True}, status_code=201)
            self.assertEqual(status, 201)
            self.assertEqual(await response.get_json(), {"saved": True})
            response, status = module.error_response("bad", status_code=400)
            self.assertEqual(status, 400)
            self.assertEqual((await response.get_json())["status"], "error")


if __name__ == "__main__":
    unittest.main()
