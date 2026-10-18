"""The FastAPI shell of wowmap (ADR 0012, #323).

    GET /api/v2/players   online players, then recently logged-out ones, typed by the Player model (the OpenAPI spec)
    GET /api/v2/stream    SSE: `event: players` at once, then every WOWMAP_SSE_INTERVAL_S seconds
    any other GET/POST    the old route table (routes.dispatch), answered with the same bytes
                          app.Handler sent, so the existing pages keep working on the same port

app.py serves `app` with uvicorn. The game-database calls are plain sync functions, so
FastAPI runs them in the threadpool and the event loop never waits for MySQL.
"""
import asyncio
import io
import json
import os
from collections.abc import AsyncIterator
from urllib.parse import parse_qs

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

import players
import routes
import state
import webio

app = FastAPI(title="wowmap", version="2")


class MapCoords(BaseModel):
    x: float
    y: float


class Player(BaseModel):
    """One row of players.fetch_players()."""
    model_config = ConfigDict(populate_by_name=True)

    name: str
    level: int
    class_: int = Field(alias="class")
    class_name: str
    class_color: str
    race: int
    race_name: str
    map: int
    zone: int | None
    zone_name: str
    x: float
    y: float
    z: float
    orientation: float
    instance: int
    in_world: bool
    norm_x: float | None
    norm_y: float | None
    continent: str | None
    cont_x: float | None
    cont_y: float | None
    playtime_seconds: int
    continent_name: str
    subzone: int | None
    subzone_name: str | None
    map_coords: MapCoords | None
    online: bool = True
    last_seen: int | None = None   # logout epoch, offline rows only


class PlayersResponse(BaseModel):
    players: list[Player]


class ActivityEvent(BaseModel):
    t: float
    kind: str
    text: str
    detail: dict
    source: str
    inferred: bool


class ActivityResponse(BaseModel):
    character: str
    agent: bool
    events: list[ActivityEvent]
    sources: dict


class ErrorResponse(BaseModel):
    error: str


def _all_players() -> list[dict]:
    """Online players, then the recently logged out ones (an offline row of an online name is dropped)."""
    online = players.fetch_players()
    names = {p["name"] for p in online}
    return [{**p, "online": True, "last_seen": None} for p in online] + \
           [p for p in players.fetch_offline() if p["name"] not in names]


@app.get("/api/v2/players", response_model=PlayersResponse)
def api_v2_players() -> dict:
    return {"players": _all_players()}


@app.get("/api/v2/characters/{name}/activity", response_model=ActivityResponse,
         responses={503: {"model": ErrorResponse}})
def api_v2_activity(name: str, limit: int = Query(50, ge=1, le=200)):
    if state.activity is None:
        return JSONResponse({"error": "activity feed disabled"}, status_code=503)
    return state.activity.feed(name, limit)


def _interval() -> float:
    return float(os.environ.get("WOWMAP_SSE_INTERVAL_S", "2"))


async def _players_events() -> AsyncIterator[str]:
    # Async, so a waiting stream holds no threadpool worker: only the query runs there.
    while True:
        data = json.dumps({"players": await run_in_threadpool(_all_players)})
        yield f"event: players\ndata: {data}\n\n"
        await asyncio.sleep(_interval())


@app.get("/api/v2/stream")
def api_v2_stream() -> StreamingResponse:
    return StreamingResponse(_players_events(), media_type="text/event-stream",
                             headers={"Cache-Control": webio.NO_STORE})


def _old_request(request: Request, body: bytes) -> webio.Request:
    """The webio.Request app.Handler built: the raw path and query as received, the body
    read from a buffer so read_body(n) behaves like the socket's rfile.read(n)."""
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode("latin-1") if raw_path else request.url.path
    query = parse_qs(request.scope["query_string"].decode("latin-1"))
    client = request.client.host if request.client else ""
    return webio.Request(request.method, path, query, request.headers, io.BytesIO(body).read, client)


def _answer(resp: tuple) -> Response:
    """routes.dispatch's (status, body, ctype, cache), sent as app.Handler._send did."""
    status, body, ctype, cache = resp
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode()
    elif isinstance(body, str):
        body = body.encode()
    headers = {"Content-Type": ctype}
    if cache:
        headers["Cache-Control"] = cache
    # media_type stays None: Starlette would add "; charset=utf-8" to text/* types, which the old server never sent.
    return Response(content=body, status_code=status, headers=headers)


class SpaFiles(StaticFiles):
    """The SvelteKit build (#324). A path that is not a file and has no extension is a client
    route (/v2/some/deep/link), so index.html answers it; a missing asset stays a 404."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except Exception as e:   # starlette's HTTPException; fastapi does not re-export that one
            if getattr(e, "status_code", None) != 404 or "." in path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)


# The Dockerfile copies web/build here. Absent in a source checkout (build it in web/): /v2 then fails loudly.
WEB_BUILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
web = SpaFiles(directory=WEB_BUILD, html=True, check_dir=False)
# Before the catch-all below, which would otherwise answer /v2/... first.
app.mount("/v2", web)


@app.api_route("/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def old_routes(request: Request) -> Response:
    body = await request.body()
    resp = await run_in_threadpool(routes.dispatch, _old_request(request, body))
    return _answer(resp)
