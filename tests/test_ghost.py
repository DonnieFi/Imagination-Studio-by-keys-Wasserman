"""The Voice panel (app/ghost.py): the voice agent's address, its page and socket on the Studio's own address behind
the access key, the headers that let the Studio's page frame it and hear the microphone, and the panel's script.

A stub agent (its page, /health and an echoing /ws, as Keys Ghost Machine's server has them) runs on a free port."""
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("STUDIO_HOME", str(Path(tempfile.mkdtemp()) / "TensorFoldStudio"))
sys.path.insert(0, str(ROOT / "app"))
import access  # noqa: E402
import ghost  # noqa: E402
import server  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

PAGE = (ROOT / "app/static/ghost.js").read_text(encoding="utf-8")
HTML = (ROOT / "app/static/index.html").read_text(encoding="utf-8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def agent():
    """A stand-in for the voice agent: its page, /health and a socket that echoes what it hears."""

    stub = FastAPI()

    @stub.get("/")
    def page():
        return HTMLResponse("<!doctype html><title>Boo</title><p>stub agent</p>")

    @stub.get("/health")
    def health():
        return {"ok": True, "profile": "studio", "brain": "bolt", "voice": "drama-3"}

    @stub.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        await sock.send_text(json.dumps({"type": "ready", "input_rate": 16000}))
        try:
            while True:
                message = await sock.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await sock.send_bytes(message["bytes"][::-1])
                else:
                    await sock.send_text(json.dumps({"type": "echo", "got": json.loads(message["text"])}))
        except WebSocketDisconnect:
            return

    port = free_port()
    one = uvicorn.Server(uvicorn.Config(stub, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=one.run, daemon=True).start()
    for _ in range(100):
        if one.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    one.should_exit = True


@pytest.fixture
def client():
    (ghost.HOME / ghost.FILE).unlink(missing_ok=True)
    return TestClient(server.app)


def test_the_address_is_kept_and_checked(client, agent):
    assert {k: v for k, v in client.get("/api/ghost").json().items() if k != "sockets"} == {"url": "", "configured": False, "reachable": None, "agent": None}
    said = client.post("/api/ghost", json={"url": agent + "/"}).json()
    assert said["url"] == agent and said["reachable"] is True and said["agent"]["profile"] == "studio"
    assert json.loads((ghost.HOME / ghost.FILE).read_text()) == {"url": agent}
    for bad in ("http://example.com:8095", "http://169.254.169.254", "http://127.0.0.1:8095/admin", "ftp://127.0.0.1:21", "studio:bolt"):
        assert client.post("/api/ghost", json={"url": bad}).status_code == 400, bad
    assert client.post("/api/ghost", json={"url": ""}).json()["configured"] is False


def test_the_address_is_set_from_this_computer_only(client):
    other = TestClient(server.app, base_url="http://192.168.1.10:7870")
    assert other.post("/api/ghost", json={"url": "http://127.0.0.1:8095"}, headers={"X-Studio-Key": server.ACCESS_KEY}).status_code == 403


def test_an_unreachable_agent_is_said(client):
    port = free_port()
    said = client.post("/api/ghost", json={"url": f"http://127.0.0.1:{port}"}).json()
    assert said["configured"] is True and said["reachable"] is False
    assert client.get("/ghost/").status_code == 502


def test_the_page_comes_through_the_studio(client, agent):
    client.post("/api/ghost", json={"url": agent})
    page = client.get("/ghost/")
    assert page.status_code == 200 and "stub agent" in page.text
    assert client.get("/ghost/health").json()["voice"] == "drama-3"
    assert client.get("/ghost", follow_redirects=False).headers["location"] == "/ghost/"
    # framed by the Studio's own page only, with the microphone; the Studio's own pages are never framed
    assert page.headers["x-frame-options"] == "SAMEORIGIN" and "frame-ancestors 'self'" in page.headers["content-security-policy"]
    assert "https://cdn.jsdelivr.net" in page.headers["content-security-policy"]
    studio = client.get("/")
    assert studio.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in studio.headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in studio.headers["content-security-policy"]
    assert studio.headers["permissions-policy"] == "camera=(self), microphone=(self), geolocation=()"


def test_nothing_of_the_studio_is_sent_on(client, agent):
    client.post("/api/ghost", json={"url": agent})
    assert ghost.PASS_UP == ("accept", "accept-language", "range") and "cookie" not in ghost.PASS_UP


def test_the_socket_goes_both_ways(client, agent):
    pytest.importorskip("websockets")  # uvicorn's socket library: requirements.lock, and STUDIO_PIN on a DGX Spark
    client.post("/api/ghost", json={"url": agent})
    with client.websocket_connect("/ghost/ws") as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "text", "text": "what's in the render queue?"}))
        assert json.loads(ws.receive_text()) == {"type": "echo", "got": {"type": "text", "text": "what's in the render queue?"}}
        ws.send_bytes(b"\x01\x02\x03\x04")
        assert ws.receive_bytes() == b"\x04\x03\x02\x01"


def test_a_page_of_another_site_cannot_open_the_socket(client, agent):
    client.post("/api/ghost", json={"url": agent})
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect("/ghost/ws", headers={"origin": "https://elsewhere.example"}) as ws:
            ws.receive_text()
    assert closed.value.code == 1008


def test_the_panel_is_behind_the_access_key():
    assert access.needs_key("/ghost/") and access.needs_key("/ghost/ws") and access.needs_key("/ghost")
    assert not access.needs_key("/ghost.js")


def test_the_header_has_the_panels_button_and_script():
    assert 'id="ghostbtn"' in HTML and '<script src="ghost.js"></script>' in HTML and 'href="ghost.css"' in HTML
    # the camera is never turned on by the panel; a take is played only from the studio's own folder
    assert "getUserMedia" not in PAGE and 'e.origin !== location.origin' in PAGE and "file(path)" in PAGE
    assert 'allow: "microphone; camera; autoplay"' in PAGE
