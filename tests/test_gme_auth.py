"""Checks for etl/gme_auth.py (the GME login and its retries).  Run: python -m pytest tests"""

import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "etl"))

import fetch_gme_units  # noqa: E402
import fetch_pun  # noqa: E402
import fetch_zonal  # noqa: E402
import gme_auth  # noqa: E402


class Answer:
    def __init__(self, status=200, payload=None):
        self.status_code, self._payload = status, payload or {"success": True, "token": "jwt"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._payload


def gme(monkeypatch, *answers):
    """GME answering each login in turn (an exception is raised); the calls and the waits."""
    calls, waits, queue = [], [], list(answers)

    def post(url, json, timeout):
        calls.append((url, json, timeout))
        answer = queue.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(gme_auth.requests, "post", post)
    return calls, waits


def test_a_timeout_and_a_server_error_are_tried_again(monkeypatch):
    calls, waits = gme(monkeypatch, requests.ConnectTimeout("timed out"), Answer(503), Answer())
    assert gme_auth.login("user", "secret", sleep=waits.append) == "jwt"
    assert len(calls) == 3 and waits == [15, 45]
    assert calls[0][1] == {"Login": "user", "Password": "secret"} and calls[0][2] == gme_auth.LOGIN_TIMEOUT


def test_wrong_credentials_fail_at_once(monkeypatch):
    calls, waits = gme(monkeypatch, Answer(401))
    with pytest.raises(requests.HTTPError):
        gme_auth.login("user", "wrong", sleep=waits.append)
    assert len(calls) == 1 and waits == []


def test_gme_refusing_the_login_fails_at_once(monkeypatch):
    calls, waits = gme(monkeypatch, Answer(payload={"success": False, "reason": "Invalid user"}))
    with pytest.raises(RuntimeError, match="Invalid user"):
        gme_auth.login("user", "wrong", sleep=waits.append)
    assert waits == []


def test_it_gives_up_after_the_last_attempt(monkeypatch):
    calls, waits = gme(monkeypatch, *[requests.ConnectionError("down")] * gme_auth.LOGIN_ATTEMPTS)
    with pytest.raises(requests.ConnectionError):
        gme_auth.login("user", "secret", sleep=waits.append)
    assert len(calls) == gme_auth.LOGIN_ATTEMPTS and waits == list(gme_auth.LOGIN_WAITS_SECONDS)


def test_the_three_get_tokens_log_in_through_it(monkeypatch):
    urls = []
    monkeypatch.setattr(gme_auth, "login", lambda login, password, url=gme_auth.AUTH_URL, sleep=None: urls.append(url) or "jwt")
    assert [module.get_token("user", "secret") for module in (fetch_gme_units, fetch_pun, fetch_zonal)] == ["jwt"] * 3
    assert set(urls) == {gme_auth.AUTH_URL}
