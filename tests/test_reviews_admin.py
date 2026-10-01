"""Admin reviews pages render (empty state) and the dashboard shows metric tiles."""

from __future__ import annotations
from sqlalchemy import select

from tests.conftest import login
from app.orm import Review, ReviewEvent, ReviewFinding, Settings
from tests.conftest import sample_ado_payload


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


async def test_running_review_detail_shows_progress_timeline(client):
    await login(client)
    # Use the application's overridden session dependency.
    async with client.sessions() as session:
        review = Review(pr_id=7, repo_name="repo", status="running",
                        current_phase="independent_review", status_message="Claude reviewing")
        session.add(review); await session.flush()
        session.add(ReviewEvent(review_id=review.id, kind="agent_started",
                                phase="independent_review", message="Claude reviewing"))
        await session.commit(); rid = review.id
    resp = await client.get(f"/admin/reviews/{rid}")
    assert resp.status_code == 200
    assert "Progress" in resp.text and "Claude reviewing" in resp.text
    assert f"/admin/reviews/{rid}/events" in resp.text


async def test_failed_review_shows_clean_summary_and_collapsed_traceback(client):
    await login(client)
    async with client.sessions() as session:
        review = Review(pr_id=8, repo_name="repo", status="failed", created_by="Ada Lovelace",
                        status_message="Source: Claude review agent\nWhat happened: Connection dropped.\nNext step: Retry.",
                        error="Traceback: noisy internals")
        session.add(review); await session.commit(); rid = review.id
    resp = await client.get(f"/admin/reviews/{rid}")
    assert resp.status_code == 200
    assert "Source: Claude review agent" in resp.text
    assert "Ada Lovelace" in resp.text
    assert "Nothing was posted to the pull request" in resp.text
    assert "<summary>Technical details</summary>" in resp.text
    assert "Retry review" in resp.text


async def test_review_detail_renders_posted_markdown_as_sanitized_html(client):
    await login(client)
    async with client.sessions() as session:
        review = Review(
            pr_id=9,
            repo_name="repo",
            status="converged",
            final_review_md=(
                "## Finding\n\nUse **safe output** and `code`.\n\n"
                "[bad](javascript:alert(1))<script>alert('xss')</script>"
            ),
        )
        session.add(review)
        await session.commit()
        rid = review.id

    resp = await client.get(f"/admin/reviews/{rid}")

    assert resp.status_code == 200
    assert '<div class="review-html"><h2>Finding</h2>' in resp.text
    assert "<strong>safe output</strong>" in resp.text
    assert "<code>code</code>" in resp.text
    assert "<script" not in resp.text
    assert "javascript:" not in resp.text


async def test_agent_findings_render_as_a_structured_chat_thread(client):
    await login(client)
    async with client.sessions() as session:
        review = Review(pr_id=10, repo_name="repo", status="converged")
        session.add(review)
        await session.flush()
        session.add_all([
            ReviewFinding(
                review_id=review.id,
                role="claude",
                round_no=0,
                raw='[{"file":"app.py","line":12,"severity":"high","verdict":"issue",'
                    '"claim":"A bug exists.","evidence":"The value is null.",'
                    '"suggestion":"Add a guard."}]',
            ),
            ReviewFinding(review_id=review.id, role="codex", round_no=0, raw="[]"),
            ReviewFinding(
                review_id=review.id,
                role="codex",
                round_no=1,
                raw='[{"file":"app.py","line":12,"verdict":"agree",'
                    '"claim":"Confirmed.","response":"Claude is correct."}]',
            ),
        ])
        await session.commit()
        rid = review.id

    resp = await client.get(f"/admin/reviews/{rid}")

    assert resp.status_code == 200
    assert "Independent review" in resp.text
    assert "Fight round 1" in resp.text
    assert 'class="agent-message claude"' in resp.text
    assert 'class="agent-message codex"' in resp.text
    assert "app.py:12" in resp.text
    assert "A bug exists." in resp.text
    assert "Suggested fix" in resp.text
    assert "Claude is correct." in resp.text
    assert "No findings in this round." in resp.text


async def test_manual_retry_fetches_current_pr_and_dispatches(client, monkeypatch):
    await login(client)
    async with client.sessions() as session:
        session.add(Settings(id=1, ado_organization="contoso", ado_project="widgets", ado_pat="pat"))
        review = Review(pr_id=42, repo_name="repo", status="failed", error="boom")
        session.add(review); await session.commit(); rid = review.id

    payload = sample_ado_payload()
    payload["resource"]["status"] = "active"

    async def fake_fetch(self, settings, pr_id):
        assert pr_id == 42
        return payload["resource"]

    monkeypatch.setattr("app.admin.reviews.AdoClient.fetch_pull_request", fake_fetch)
    resp = await client.post(f"/admin/reviews/{rid}/retry", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("?retry=queued")
    import json
    from app.orm import ReviewRequest
    async with client.sessions() as session:
        queued = (await session.scalars(select(ReviewRequest))).one()
        job = json.loads(queued.job_json)
    assert job["created_by"] == "Ada Lovelace"
    assert job["event_type"] == "manual.retry"
