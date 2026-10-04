"""The dashboard's web server: a few read-only JSON endpoints and one static page.

    python -m src.dashboard.server                 # http://127.0.0.1:8787
    python -m src.dashboard.server --port 9000

It listens on this machine only by default. `--host 0.0.0.0` would open it to the local network; there is no login,
so only do that on a network you trust. Every endpoint reads; none can change a book or place an order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from aiohttp import web

from src.dashboard.data import DashboardData

STATIC = Path(__file__).parent / "static"
COSTS_CONFIG = "config/portfolio.multi_paper.toml"
DATA_KEY = web.AppKey("data", DashboardData)


def _json(payload: Any) -> web.Response:
    return web.Response(text=json.dumps(payload, default=str), content_type="application/json", headers={"Cache-Control": "no-store"})


async def _read(request: web.Request, method: str, *args: Any, **kwargs: Any) -> Any:
    """Run a (blocking) database read in a worker thread, so one slow query can't stall the other requests."""
    return await asyncio.to_thread(getattr(request.app[DATA_KEY], method), *args, **kwargs)


async def books(request: web.Request) -> web.Response:
    return _json(await _read(request, "books"))


async def book(request: web.Request) -> web.Response:
    snapshot = await _read(request, "book", request.match_info["name"])
    if snapshot is None:
        raise web.HTTPNotFound(text="no such book")
    return _json(snapshot)


async def history(request: web.Request) -> web.Response:
    return _json(await _read(request, "history", request.match_info["name"]))


async def fills(request: web.Request) -> web.Response:
    return _json(await _read(request, "fills", request.match_info["name"]))


async def live(request: web.Request) -> web.Response:
    return _json(await _read(request, "live", request.match_info["name"]))


async def alerts(request: web.Request) -> web.Response:
    return _json(await _read(request, "alerts", request.query.get("book")))


async def system(request: web.Request) -> web.Response:
    health, research = await _read(request, "health"), await _read(request, "research")
    try:
        costs = await _read(request, "costs", COSTS_CONFIG)
    except Exception:  # noqa: BLE001 - the page still shows the rest
        costs = []
    return _json({"health": health, "research": research, "costs": costs})


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


def build_app(data: DashboardData) -> web.Application:
    """The aiohttp application over one `DashboardData` (tests pass one built on a temporary database)."""
    @web.middleware
    async def revalidate(request: web.Request, handler: Any) -> web.StreamResponse:
        """Make browsers check every file with the server before reusing it, so an updated page is seen at the next load."""
        response = await handler(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app = web.Application(middlewares=[revalidate])
    app[DATA_KEY] = data
    app.add_routes([web.get("/", index), web.get("/api/books", books), web.get("/api/book/{name}", book), web.get("/api/book/{name}/history", history),
                    web.get("/api/book/{name}/fills", fills), web.get("/api/book/{name}/live", live), web.get("/api/alerts", alerts), web.get("/api/system", system), web.static("/static", STATIC)])
    return app


def main() -> None:
    from config import settings

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    print(f"Dashboard on http://{args.host}:{args.port} (read-only; Ctrl-C to stop)", flush=True)
    web.run_app(build_app(DashboardData(settings.database_path, Path.cwd())), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
