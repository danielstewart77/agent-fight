"""The boxing-robot favicon is served (no auth) at both /favicon.svg and .ico."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", ["/favicon.svg", "/favicon.ico"])
async def test_favicon_served(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/svg+xml"
    assert resp.text.lstrip().startswith("<svg")


async def test_boxer_static_asset_served(client):
    resp = await client.get("/static/boxer.svg")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<svg")


async def test_login_page_flanks_title_with_two_boxers(client):
    resp = await client.get("/login")
    assert resp.status_code == 200
    # Two boxer marks (one mirrored) flanking the Agent Fight title.
    assert resp.text.count("/static/boxer.svg") == 2
    assert "boxer flip" in resp.text
