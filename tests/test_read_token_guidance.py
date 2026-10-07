"""`xhs read`: missing-token guidance and short-link resolve fallback (no browser).

2026-10-07, two failures on one note:
  1. `xhs read <xhslink>` died on a single in-browser Page.goto timeout; the identical
     command succeeded minutes later.
  2. The fallback that followed stripped the URL's query (`/discovery/item/<id>`): no
     xsec_token -> verification wall 3/3, reported as "may need a human QR re-login",
     while the session was logged in and the full URL read fine at the same moment.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from click.testing import CliRunner

import xhs_cli.cli as cli_module
from xhs_cli.cli import cli
from xhs_cli.exceptions import LoginError

NID = "6ac45117000000001b01fa94"
FULL = (f"https://www.xiaohongshu.com/discovery/item/{NID}"
        "?app_platform=ios&xsec_token=TOK123%3D&xsec_source=app_share")


class _Client:
    def __init__(self, resolve_fails=0, detail_exc=None):
        self.resolve_fails = resolve_fails
        self.resolve_calls = 0
        self.detail_exc = detail_exc
        self.detail_args = None

    def resolve_share_link(self, url):
        self.resolve_calls += 1
        if self.resolve_calls <= self.resolve_fails:
            raise TimeoutError("Page.goto: Timeout 25000ms exceeded.")
        return FULL

    def harvest_xsec_token(self):
        return ""

    def get_note_detail(self, note_id, xsec_token, xsec_source="pc_feed", resolved_url=""):
        self.detail_args = (note_id, xsec_token, xsec_source, resolved_url)
        if self.detail_exc:
            raise self.detail_exc
        return {"note": {"noteId": note_id, "title": "t", "desc": "d"}}


@pytest.fixture
def runner():
    return CliRunner()


def _use(monkeypatch, client, http_url=""):
    @contextmanager
    def ctx():
        yield client
    monkeypatch.setattr(cli_module, "_get_client", ctx)
    monkeypatch.setattr(cli_module, "load_xsec_token", lambda _nid: "")
    monkeypatch.setattr(cli_module, "_http_final_url", lambda _ref: http_url, raising=False)


def _text(result):
    out = result.output
    try:
        out += result.stderr
    except ValueError:
        pass
    return out


def test_bare_id_wall_names_missing_token_and_exits_3(runner, monkeypatch):
    c = _Client(detail_exc=LoginError("Blocked by security verification while loading note"))
    _use(monkeypatch, c)
    r = runner.invoke(cli, ["read", f"https://www.xiaohongshu.com/discovery/item/{NID}", "--json"])
    assert r.exit_code == 3, _text(r)
    assert "no xsec_token" in _text(r)            # up-front warning
    assert "Cause:" in _text(r)                   # and the cause on failure


def test_wall_with_a_token_is_not_relabelled(runner, monkeypatch):
    """Control: a token WAS supplied -> the wall is something else; keep exit 1."""
    c = _Client(detail_exc=LoginError("Blocked by security verification"))
    _use(monkeypatch, c)
    r = runner.invoke(cli, ["read", FULL, "--json"])
    assert r.exit_code == 1, _text(r)
    assert "Cause:" not in _text(r)
    assert "no xsec_token" not in _text(r)


def test_full_url_keeps_its_token(runner, monkeypatch):
    c = _Client()
    _use(monkeypatch, c)
    r = runner.invoke(cli, ["read", FULL, "--json"])
    assert r.exit_code == 0, _text(r)
    assert c.detail_args[0] == NID and c.detail_args[1] == "TOK123="


def test_short_link_transient_timeout_retries_in_browser(runner, monkeypatch):
    c = _Client(resolve_fails=1)
    _use(monkeypatch, c, http_url="")
    r = runner.invoke(cli, ["read", "https://xhslink.cn/o/8JaWNY2MqoI", "--json"])
    assert r.exit_code == 0, _text(r)
    assert c.resolve_calls == 2
    assert c.detail_args[1] == "TOK123=" and c.detail_args[2] == "app_share"


def test_short_link_falls_back_to_http_url_verbatim(runner, monkeypatch):
    c = _Client(resolve_fails=2)
    _use(monkeypatch, c, http_url=FULL)
    r = runner.invoke(cli, ["read", "https://xhslink.cn/o/8JaWNY2MqoI", "--json"])
    assert r.exit_code == 0, _text(r)
    assert c.resolve_calls == 2
    assert c.detail_args[3] == FULL               # whole URL passed on, params intact
    assert c.detail_args[1] == "TOK123="


def test_short_link_all_routes_fail_still_fails(runner, monkeypatch):
    c = _Client(resolve_fails=5)
    _use(monkeypatch, c, http_url="")
    r = runner.invoke(cli, ["read", "https://xhslink.cn/o/8JaWNY2MqoI", "--json"])
    assert r.exit_code != 0
