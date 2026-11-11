import http.client
import json
import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from app import Session, make_server
from audacity_adapter import Capture, audio_fingerprint
from pipe_transport import Pipe, checked_open


@pytest.fixture
def http_server():
    server, token = make_server(0, session=Session(adapter=object()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, token
    server.shutdown()
    server.server_close()
    thread.join()


def response(server, token, *, path="/api/reset", headers=(), body=b"{}"):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    client.putrequest("POST", path, skip_host=True)
    client.putheader("Host", f"127.0.0.1:{server.server_port}")
    client.putheader("X-Repaint-Token", token)
    client.putheader("Content-Type", "application/json")
    client.putheader("Content-Length", str(len(body)))
    for key, value in headers:
        client.putheader(key, value)
    client.endheaders(body)
    result = client.getresponse()
    status = result.status
    payload = result.read()
    client.close()
    return status, payload


@pytest.mark.parametrize(
    "headers",
    [
        [("Host", "external.invalid")],
        [("Origin", "https://external.invalid")],
        [("Origin", "http://127.0.0.1:1"), ("Origin", "http://127.0.0.1:1")],
        [("X-Repaint-Token", "duplicate")],
    ],
)
def test_reject_duplicate_or_cross_origin_headers(http_server, headers):
    server, token = http_server
    assert response(server, token, headers=headers)[0] == 403


def test_non_ascii_token_is_rejected_without_server_error(http_server):
    server, _ = http_server
    assert response(server, "é")[0] == 403


@pytest.mark.parametrize("body", [b"null", b"[]", b"not json"])
def test_reject_invalid_json_bodies(http_server, body):
    server, token = http_server
    assert response(server, token, body=body)[0] == 400


def test_reject_transfer_encoding_and_duplicate_length(http_server):
    server, token = http_server
    assert response(server, token, headers=[("Transfer-Encoding", "chunked")])[0] == 400
    assert response(server, token, headers=[("Content-Length", "2")])[0] == 400


def test_asset_allowlist_and_strict_content_security_policy(http_server):
    server, _ = http_server
    client = http.client.HTTPConnection("127.0.0.1", server.server_port)
    client.request("GET", "/app.js")
    result = client.getresponse()
    assert result.status == 200
    assert "'unsafe-inline'" not in result.getheader("Content-Security-Policy")
    assert result.getheader("Referrer-Policy") == "no-referrer"
    result.read()
    client.close()
    client = http.client.HTTPConnection("127.0.0.1", server.server_port)
    client.request("GET", "/../../app.py")
    result = client.getresponse()
    assert result.status == 403
    result.read()
    client.close()


def test_capture_response_does_not_include_audio_or_snapshots():
    capture = Capture(
        0,
        "sample",
        0,
        2,
        0.2,
        0.4,
        48000,
        2,
        np.zeros((100, 2), dtype=np.float32),
        "private-hash",
        "private-snapshot",
    )
    public = json.dumps(capture.public())
    assert "private-hash" not in public and "private-snapshot" not in public
    assert "audio" not in public and "fingerprint" not in public


def test_failed_capture_invalidates_previous_confirmation():
    class Adapter:
        def capture(self):
            raise ValueError("no selection")

    session = Session(Adapter())
    session.result = object()
    session.result_id = "old"
    with pytest.raises(ValueError):
        session.action("target", {})
    assert session.result is None and session.result_id is None


def test_busy_session_does_not_queue_commands():
    session = Session(adapter=object())
    with session.lock:
        with pytest.raises(RuntimeError, match="in progress"):
            session.action("reset", {})


@pytest.mark.parametrize(
    "settings",
    [
        {"candidates": 2.5},
        {"candidates": True},
        {"candidates": None},
        {"candidates": []},
        {"max_ms": None},
        {"max_ms": float("nan")},
        {"max_ms": float("inf")},
        {"method": "unknown"},
    ],
)
def test_bad_settings_rejected_before_audacity_calls(settings):
    class Adapter:
        def validate(self, *_):
            pytest.fail("invalid settings reached Audacity")

    session = Session(Adapter())
    session.reference = SimpleNamespace(track=0, clip_start=0, clip_end=2, rate=48000)
    session.target = SimpleNamespace(track=1, clip_start=0, clip_end=2, rate=48000)
    with pytest.raises(ValueError, match="Invalid analysis settings"):
        session.action("analyze", settings)


def fake_pipe(monkeypatch, *, trace_limit=0, payload=b""):
    pipe = Pipe.__new__(Pipe)
    pipe.w, pipe.r = 24, 23
    pipe.allow_lifecycle = False
    pipe.trace_limit, pipe.trace, pipe._has_reply = trace_limit, [], False
    monkeypatch.setattr("pipe_transport.os.write", lambda _, data: len(data))
    monkeypatch.setattr("pipe_transport.select.select", lambda *_: ([23], [], []))
    monkeypatch.setattr(
        "pipe_transport.os.read", lambda *_: payload + b"\nBatchCommand finished: OK\n\n"
    )
    return pipe


def test_production_transport_does_not_retain_responses(monkeypatch):
    pipe = fake_pipe(monkeypatch, payload=b"private project metadata")
    for _ in range(100):
        pipe.command("GetInfo: Type=Tracks")
    assert pipe.trace == []


def test_opt_in_diagnostics_are_bounded(monkeypatch):
    pipe = fake_pipe(monkeypatch, trace_limit=3, payload=b"x" * 9000)
    for _ in range(10):
        pipe.command("GetInfo: Type=Tracks")
    assert len(pipe.trace) == 3
    assert all(len(row["response"]) == 4096 and row["truncated"] for row in pipe.trace)


def test_response_size_limit_poisoned_before_parsing(monkeypatch):
    pipe = fake_pipe(monkeypatch, payload=b"x" * 100)
    poisoned = []
    monkeypatch.setattr("pipe_transport.MAX_RESPONSE_BYTES", 10)
    monkeypatch.setattr(pipe, "_poison", lambda: poisoned.append(True))
    with pytest.raises(RuntimeError, match="too much metadata"):
        pipe.command("GetInfo: Type=Tracks")
    assert poisoned == [True]


def test_refuse_regular_file_in_place_of_fifo(tmp_path):
    path = tmp_path / "wrong-pipe"
    path.write_text("preserve")
    path.chmod(0o600)
    with pytest.raises(RuntimeError, match="Unsafe"):
        checked_open(path, os.O_RDONLY, fifo=True)
    assert path.read_text() == "preserve"


def test_refuse_symlink_lock(tmp_path):
    target = tmp_path / "target"
    target.write_text("preserve")
    link = tmp_path / "lock"
    link.symlink_to(target)
    with pytest.raises(OSError):
        checked_open(link, os.O_RDWR, fifo=False)
    assert target.read_text() == "preserve"


def test_refuse_world_accessible_fifo(tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path, 0o600)
    path.chmod(0o666)
    with pytest.raises(RuntimeError, match="Unsafe"):
        checked_open(path, os.O_RDONLY, fifo=True)


def test_fingerprint_matches_original_bytes_without_copy():
    import hashlib

    audio = np.arange(30, dtype=np.float32).reshape(15, 2)
    assert audio_fingerprint(audio) == hashlib.sha256(audio.tobytes()).hexdigest()
