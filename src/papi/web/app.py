"""FastAPI application factory for the local dashboard and the ``/v1`` API (07, 08 §1).

Security model (08 §1, 09):
- Bound to loopback by default; then no auth, but the ``Host`` header must be a loopback name (blocks DNS
  rebinding) and state-changing requests must not come from a foreign ``Origin`` (blocks CSRF).
- Any other bind address requires a bearer token (``papi.core.config.get_web_token``) on **every**
  route. Browsers can authenticate once with ``?token=…``; papi then sets an HttpOnly cookie and
  redirects to the clean URL (EventSource and plain navigation cannot send headers).
"""

from __future__ import annotations

import ipaddress
import mimetypes
import secrets
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from papi import __version__
from papi.core.config import get_settings, get_web_token

WEB_DIR = Path(__file__).resolve().parent
TOKEN_COOKIE = "papi_token"
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "testserver"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_loopback(host: str) -> bool:
    name = host.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _host_name(host_header: str) -> str:
    if host_header.startswith("["):
        return host_header[1:].split("]", 1)[0].lower()
    return host_header.rsplit(":", 1)[0].lower() if host_header.count(":") == 1 else host_header.lower()


class GuardMiddleware:
    """Bearer-token enforcement (non-loopback binds), Host allow-list and Origin check (loopback binds)."""

    def __init__(self, app: ASGIApp, *, token: str | None, loopback: bool) -> None:
        self.app = app
        self.token = token
        self.loopback = loopback

    def _token_ok(self, candidate: str | None) -> bool:
        return bool(candidate) and self.token is not None and secrets.compare_digest(candidate, self.token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if self.loopback:
            host = _host_name(request.headers.get("host", ""))
            if host and host not in LOOPBACK_NAMES and not is_loopback(host):
                await PlainTextResponse("Geçersiz Host başlığı", status_code=400)(scope, receive, send)
                return
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlsplit(origin).netloc != request.headers.get("host", ""):
                await PlainTextResponse("Başka kaynaktan gelen istek reddedildi", status_code=403)(
                    scope, receive, send
                )
                return
        if self.token is not None and not self.loopback:
            auth = request.headers.get("authorization", "")
            bearer = auth[7:].strip() if auth[:7].lower() == "bearer " else None
            if self._token_ok(bearer) or self._token_ok(request.cookies.get(TOKEN_COOKIE)):
                await self.app(scope, receive, send)
                return
            query_token = request.query_params.get("token")
            if request.method == "GET" and self._token_ok(query_token):
                rest = [
                    (k, v) for k, v in parse_qsl(request.url.query, keep_blank_values=True) if k != "token"
                ]
                target = request.url.path + (f"?{urlencode(rest)}" if rest else "")
                resp = RedirectResponse(target, status_code=303)
                resp.set_cookie(TOKEN_COOKIE, query_token, httponly=True, samesite="strict", path="/")
                await resp(scope, receive, send)
                return
            resp = JSONResponse(
                {"detail": "Yetkisiz: bearer token gerekli"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await resp(scope, receive, send)
            return
        await self.app(scope, receive, send)


UI_MISSING_HTML = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>papi UI</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:40rem;margin:4rem auto;padding:0 1rem}
code{background:#eee;padding:.1rem .3rem;border-radius:3px}</style></head><body>
<h1>Arayüz derlenmemiş / UI not built</h1>
<p>Yeni arayüz henüz derlenmedi. Derlemek için:</p>
<p>The new UI has not been built yet. To build it:</p>
<p><code>cd frontend &amp;&amp; npm ci &amp;&amp; npm run build</code></p>
<p><a href="/">Eski pano / Old dashboard</a></p>
</body></html>"""


def _ui_routes(app: FastAPI, ui_dir: Path) -> None:
    def index_response() -> Response:
        index = ui_dir / "index.html"
        if not index.is_file():
            return HTMLResponse(UI_MISSING_HTML, status_code=503, headers={"Cache-Control": "no-store"})
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/app", include_in_schema=False)
    def ui_root_redirect() -> Response:
        return RedirectResponse("/app/", status_code=307)

    @app.get("/app/", include_in_schema=False)
    def ui_root() -> Response:
        return index_response()

    @app.get("/app/{path:path}", include_in_schema=False)
    def ui_file(path: str) -> Response:
        if not ui_dir.is_dir():
            return index_response()
        root = ui_dir.resolve()
        try:
            target = (root / path).resolve()
            target.relative_to(root)
        except (ValueError, OSError):
            return index_response()
        if path.startswith("assets/") and not target.is_file():
            return PlainTextResponse("Not found", status_code=404)
        if not path or not target.is_file() or target.name == "index.html":
            return index_response()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
        return FileResponse(target, media_type=ctype, headers={"Cache-Control": cache})


def create_app(
    *,
    host: str | None = None,
    ui_dir: Path | None = None,
    token: str | None = None,
    stream_poll_seconds: float = 1.0,
    stream_max_seconds: float = 60.0,
) -> FastAPI:
    """Build the dashboard app.

    ``host`` is the address the server will bind (default ``web.host``); when it is not loopback a
    token is mandatory (``token`` argument, else ``get_web_token()``) and enforced on all routes.
    ``stream_max_seconds`` bounds one ``/v1/stream`` SSE connection; API clients reconnect with
    ``Last-Event-ID``. The dashboard itself short-polls ``/stream/poll`` (no long-lived connections).
    """
    settings = get_settings()
    bind = host or settings.web.host
    loopback = is_loopback(bind)
    tok = token if token is not None else get_web_token()
    if not loopback and not tok:
        raise RuntimeError(
            f"web.host={bind} loopback değil: bearer token gerekli "
            "(PAPI_WEB_TOKEN, keyring papi/web_token ya da web.token)."
        )

    from papi.web import agents, api, pages, stream, theses

    app = FastAPI(
        title="papi",
        version=__version__,
        docs_url=None,  # Swagger UI loads assets from a CDN; pages must make no external requests
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )
    app.state.bind_host = bind
    app.state.stream_poll_seconds = stream_poll_seconds
    app.state.stream_max_seconds = stream_max_seconds
    app.add_middleware(GuardMiddleware, token=tok if not loopback else None, loopback=loopback)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(stream.router)
    app.include_router(api.router)
    app.include_router(pages.router)
    app.include_router(agents.router)
    app.include_router(theses.router)
    _ui_routes(app, ui_dir if ui_dir is not None else WEB_DIR / "ui")
    return app
