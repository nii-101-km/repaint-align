import json
import threading
import urllib.error
import urllib.request

import pytest

from app import Session, make_server


def test_loopback_auth_and_origin():
    session = Session(adapter=object())
    server, token = make_server(0, session=session)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        assert urllib.request.urlopen(url + "/").status == 200
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(url + "/api/state")
        assert exc.value.code == 403
        request = urllib.request.Request(url + "/api/state", headers={"X-Repaint-Token": token})
        assert json.load(urllib.request.urlopen(request))["result"] is None
        request = urllib.request.Request(
            url + "/api/reset",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Repaint-Token": token,
                "Origin": "https://external.invalid",
            },
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request)
        assert exc.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_apply_requires_current_result():
    class Adapter:
        def apply(self, *args):
            raise AssertionError("must not be reached")

    session = Session(Adapter())
    with pytest.raises(ValueError, match="stale"):
        session.action("apply", {"result_id": "fake"})


def test_failed_apply_consumes_confirmation():
    class Result:
        total_seconds = 1

    class Adapter:
        def apply(self, *args):
            raise ValueError("stale")

    session = Session(Adapter())
    session.result = Result()
    session.result_id = "valid"
    with pytest.raises(ValueError, match="stale"):
        session.action("apply", {"result_id": "valid"})
    assert session.result_id is None
    with pytest.raises(ValueError, match="stale"):
        session.action("apply", {"result_id": "valid"})
