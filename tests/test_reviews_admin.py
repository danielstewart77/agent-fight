"""Admin reviews pages render (empty state) and the dashboard shows metric tiles."""

from __future__ import annotations

from tests.conftest import login


async def test_reviews_list_empty_state(client):
    await login(client)
    resp = await client.get("/admin/reviews")
    assert resp.status_code == 200
    assert "No reviews yet" in resp.text


async def test_dashboard_renders_metric_tiles(client):
    await login(client)
    resp = await client.get("/admin/")
    assert resp.status_code == 200
    # Tiles present and the "{:,}".format(...) token tile renders with zero data.
    assert "Review metrics" in resp.text
    assert "tokens" in resp.text
    assert "View all reviews" in resp.text


async def test_review_detail_404_when_missing(client):
    await login(client)
    resp = await client.get("/admin/reviews/999")
    assert resp.status_code == 404


async def test_reviews_requires_login(client):
    # No login -> redirected to /login (require_login raises a redirect).
    resp = await client.get("/admin/reviews", follow_redirects=False)
    assert resp.status_code in (302, 303, 307)
    assert resp.headers["location"].endswith("/login")
