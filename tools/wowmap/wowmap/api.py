"""The FastAPI shell of wowmap (ADR 0012, #323).

    GET /api/v2/players   online players, typed by the Player model (the OpenAPI spec)
    GET /api/v2/stream    SSE: `event: players` at once, then every WOWMAP_SSE_INTERVAL_S seconds
    any other GET/POST    the old route table (routes.dispatch), answered with the same bytes
                          app.Handler sent, so the existing pages keep working on the same port

app.py serves `app` with uvicorn. The game-database calls are plain sync functions, so
FastAPI runs them in the threadpool and the event loop never waits for MySQL.
"""
import io
import json
import os
import time
from collections.abc import Iterator
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

import players
import routes
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


class PlayersResponse(BaseModel):
    players: list[Player]


@app.get("/api/v2/players", response_model=PlayersResponse)
def api_v2_players() -> dict:
    return {"players": players.fetch_players()}


def _interval() -> float:
    return float(os.environ.get("WOWMAP_SSE_INTERVAL_S", "2"))


def _players_events() -> Iterator[str]:
    while True:
        data = json.dumps({"players": players.fetch_players()})
        yield f"event: players\ndata: {data}\n\n"
        time.sleep(_interval())


@app.get("/api/v2/stream")
def api_v2_stream() -> StreamingResponse:
    # A sync generator: Starlette iterates it in the threadpool, so a slow query does not block the loop.
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


@app.api_route("/{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def old_routes(request: Request) -> Response:
    body = await request.body()
    resp = await run_in_threadpool(routes.dispatch, _old_request(request, body))
    return _answer(resp)
