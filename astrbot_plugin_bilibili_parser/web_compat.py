"""Web request and response helpers for both AstrBot dashboard backends."""

try:
    from astrbot.api.web import error_response, json_response, request

    WEB_BACKEND = "fastapi"
except ModuleNotFoundError as exc:
    if exc.name != "astrbot.api.web":
        raise

    from quart import g, jsonify
    from quart import request as quart_request

    WEB_BACKEND = "quart"

    class LegacyRequest:
        """Expose the subset of the new PluginRequest API used by this plugin."""

        @property
        def username(self) -> str | None:
            return getattr(g, "username", None)

        async def json(self, default=None):
            payload = await quart_request.get_json(silent=True)
            return default if payload is None else payload

    request = LegacyRequest()

    def json_response(data, *, status_code=200, headers=None):
        response = jsonify(data)
        if headers:
            response.headers.update(headers)
        return response, status_code

    def error_response(message, *, status_code=400, data=None, headers=None):
        return json_response(
            {"status": "error", "message": message, "data": data},
            status_code=status_code,
            headers=headers,
        )


__all__ = ["WEB_BACKEND", "error_response", "json_response", "request"]
