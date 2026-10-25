"""The Voice panel: Keys Ghost Machine (Boo, the hands-free voice agent) inside the Studio, as the Studio's voice director.

The voice agent runs on its own computer (a Mac, or a DGX Spark that is not the one leading the others), never inside
this app: it has its own speech recognizer, brain and voice. This module only

- keeps its address (``<studio folder>/ghost.json``, or ``STUDIO_GHOST_URL`` until one is saved), set from this
  computer or its Tailscale address, as the brains are (``agents.local``), and only an address on this computer, the
  network or the tailnet (``slate.nearby_endpoint``): the Studio fetches what is named here;
- serves the agent's page under ``/ghost/`` on the Studio's own address, its socket included, so a phone that reaches
  the Studio (its access key, ``tailscale serve`` for HTTPS) reaches the agent with no second address, certificate or
  key. ``/ghost/`` is behind the access key like ``/api/`` (``access.needs_key``), and its socket is refused when a page
  of another site opens it.

The agent itself acts on the Studio through the Studio's HTTP API with the access key in its own settings (its
``studio`` back agent): nothing here gives it more than the key does.

    GET  /api/ghost              {url, configured, reachable, agent}
    POST /api/ghost              {url}: "" to forget it
    GET  /ghost/, /ghost/health, /ghost/voices/<name>.wav   the agent's page and what it loads from beside itself
    WS   /ghost/ws               the agent's conversation socket: microphone audio up, events and speech down
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import urllib.error
import urllib.request
from fastapi import APIRouter, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, RedirectResponse, Response

HOME: Path | None = None
lock = threading.Lock()
FILE = "ghost.json"
PREFIX = "/ghost/"
TIMEOUT = 10  # seconds for the agent's page and health
# what a page's request carries on to the agent, and what of its answer comes back: never the Studio's cookie or key
PASS_UP = ("accept", "accept-language", "range")
PASS_DOWN = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


def path() -> Path | None:
    return HOME / FILE if HOME else None


def saved() -> str:
    """The agent's address: the one saved here (even "", forgotten on purpose), else STUDIO_GHOST_URL."""

    try:
        data = json.loads(path().read_text()) if path() and path().is_file() else None
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("url"), str):
        return data["url"]
    return os.environ.get("STUDIO_GHOST_URL", "").strip().rstrip("/")


def checked(url: str) -> str:
    """The agent's address as it is kept: http(s)://host:port, nothing after it, on this computer, the network or
    the tailnet. 400 otherwise."""

    import slate

    text = (url or "").strip().rstrip("/")
    if not text:
        return ""
    parts = urlsplit(text if "://" in text else "http://" + text)
    try:
        parts.port  # a port that is not a number ("studio:bolt") is no address
    except ValueError:
        raise HTTPException(400, "the voice agent's address is http://host:port, with nothing after it") from None
    if parts.scheme not in ("http", "https") or parts.path not in ("", "/") or parts.query or parts.fragment or not parts.hostname:
        raise HTTPException(400, "the voice agent's address is http://host:port, with nothing after it")
    try:
        slate.nearby_endpoint(text)
    except HTTPException:
        raise HTTPException(400, "the voice agent is on this computer, this network or the tailnet: http://host:port") from None
    return f"{parts.scheme}://{parts.netloc}"


def write(url: str) -> None:
    with lock:
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f"{FILE}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps({"url": url}, indent=2) + "\n")
        temp.replace(target)


def fetch(url: str, headers: dict | None = None, timeout: float = TIMEOUT) -> tuple[int, dict, bytes]:
    """(status, headers, body) of a GET, an error status included; OSError when nothing answers. Run in a thread."""

    request = urllib.request.Request(url, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # straight to the agent, never a proxy
    try:
        with opener.open(request, timeout=timeout) as reply:
            return reply.status, dict(reply.headers.items()), reply.read(32 << 20)
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), error.read(1 << 16)


async def health(url: str) -> dict | None:
    try:
        status, _, body = await asyncio.to_thread(fetch, url + "/health", None, 2)
        return json.loads(body) if status == 200 else None
    except (OSError, ValueError):
        return None


def sockets() -> bool:
    """Whether this app can carry the agent's socket: uvicorn needs the websockets package for it (requirements.lock;
    oneshot-setup-cuda.sh's STUDIO_PIN on a DGX Spark)."""

    try:
        import websockets  # noqa: F401
        return True
    except ImportError:
        return False


def same_site(headers) -> bool:
    """A socket opened by the Studio's own page (or by a tool that sends no Origin), not by a page of another site."""

    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        return urlsplit(origin).netloc.lower() == (headers.get("host") or "").lower()
    except ValueError:
        return False


def attach(home: Path, local) -> APIRouter:
    """``local(request, mutation)``: agents.local, the brains' own check (this computer or its Tailscale address)."""

    global HOME
    HOME = home
    router = APIRouter()

    @router.get("/api/ghost")
    async def state() -> dict:
        url = saved()
        agent = await health(url) if url else None
        return {"url": url, "configured": bool(url), "reachable": bool(agent) if url else None,
                "agent": {k: agent.get(k) for k in ("profile", "brain", "voice", "eyes")} if agent else None,
                "sockets": sockets()}

    @router.post("/api/ghost")
    async def save(body: dict, request: Request) -> dict:
        local(request, True)
        if not isinstance(body, dict) or not isinstance(body.get("url", ""), str):
            raise HTTPException(400, "url is the voice agent's address, http://host:port")
        url = checked(body.get("url", ""))
        write(url)
        return await state()

    @router.get("/ghost")
    def bare() -> Response:
        return RedirectResponse(PREFIX, status_code=307)  # the page's own requests are relative to /ghost/

    async def relay(rest: str, request: Request) -> Response:
        url = saved()
        if not url:
            return JSONResponse({"error": "No voice agent is set up: Voice panel → Set up."}, status_code=404)
        headers = {k: v for k, v in request.headers.items() if k.lower() in PASS_UP}
        try:
            status, said, body = await asyncio.to_thread(fetch, f"{url}/{rest}", headers)
        except OSError as error:
            return JSONResponse({"error": f"The voice agent at its address did not answer ({type(error).__name__})."}, status_code=502)
        kept = {k: v for k, v in said.items() if k.lower() in PASS_DOWN and k.lower() != "content-length"}
        return Response(body, status_code=status, headers={**kept, "Cache-Control": "no-store"})

    @router.get(PREFIX)
    async def page(request: Request) -> Response:
        return await relay("", request)

    @router.get(PREFIX + "health")
    async def agent_health(request: Request) -> Response:
        return await relay("health", request)

    @router.get(PREFIX + "voices/{name}.wav")
    async def sample(name: str, request: Request) -> Response:
        if not re.fullmatch(r"[\w-]{1,64}", name):
            raise HTTPException(404, "no sample")
        return await relay(f"voices/{name}.wav", request)

    @router.websocket(PREFIX + "ws")
    async def socket(browser: WebSocket) -> None:
        if not same_site(browser.headers):
            await browser.close(code=1008)
            return
        url = saved()
        if not url:
            await browser.close(code=1011)
            return
        from websockets.asyncio.client import connect  # uvicorn's own socket library
        from websockets.exceptions import ConnectionClosed

        target = re.sub(r"^http", "ws", url) + "/ws"
        try:
            upstream = await connect(target, max_size=2 ** 24, open_timeout=5, ping_interval=20, ping_timeout=20)
        except Exception:  # noqa: BLE001 - any failure to reach it closes the page's socket
            await browser.close(code=1011)
            return
        await browser.accept()

        async def up() -> None:  # the browser's microphone and choices to the agent
            while True:
                message = await browser.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
                elif message.get("text") is not None:
                    await upstream.send(message["text"])

        async def down() -> None:  # the agent's events and speech to the browser
            async for message in upstream:
                if isinstance(message, bytes):
                    await browser.send_bytes(message)
                else:
                    await browser.send_text(message)

        jobs = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        except ConnectionClosed:
            pass
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await upstream.close()
            try:
                await browser.close()
            except RuntimeError:
                pass  # already closed by the browser

    return router
