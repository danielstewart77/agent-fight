"""Exercise real durable queue scheduling with controlled external operations."""
import asyncio
import json
from dataclasses import asdict, replace

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ado import UnsupportedEvent, parse_comment_event, request_key
from app.orm import Base, ReviewRequest, Settings
from app.review_queue import ReviewDispatcher
from tests.conftest import sample_comment_payload


class Ado:
    def __init__(self):
        self.messages = []
        self.reviews = []
        self.closed = False
        self.fail_post = False

    async def prepare_request(self, settings, job):
        return replace(job, source_commit="a" * 40, target_commit="b" * 40)

    async def request_status(self, settings, job, message):
        self.messages.append((job.comment_id, message))

    async def ensure_active(self, settings, job):
        if self.closed:
            raise UnsupportedEvent("closed")

    async def post_pr_review(self, settings, job, markdown):
        if self.fail_post:
            raise RuntimeError("post failed")
        self.reviews.append((job.comment_id, markdown))

    async def aclose(self):
        pass


@pytest_asyncio.fixture
async def queue(tmp_path):
    # File DB, independent connections: avoids masking transaction races in memory.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        s.add(Settings(id=1, ado_organization="contoso", ado_pat="pat"))
        await s.commit()
    yield sessions
    await engine.dispose()


async def add(sessions, comment_id=1, pr_id=42, status="pending"):
    settings = Settings(ado_organization="contoso")
    job = replace(parse_comment_event(sample_comment_payload(comment_id=comment_id), settings), pr_id=pr_id)
    if status != "pending":
        job = replace(job, source_commit="a" * 40, target_commit="b" * 40)
    async with sessions() as s:
        row = ReviewRequest(request_key=request_key(job), job_json=json.dumps(asdict(job)), status=status)
        s.add(row)
        await s.commit()
        return row.id


async def statuses(sessions):
    async with sessions() as s:
        return list((await s.scalars(select(ReviewRequest.status).order_by(ReviewRequest.id))).all())


async def advance(worker):
    await worker.tick()
    await asyncio.gather(*list(worker._preparing.values()))


async def finish(worker):
    await worker.tick()
    await asyncio.gather(*list(worker._running.values()))


async def test_same_pr_is_fifo_other_pr_can_run_and_waiting_request_is_acknowledged(queue):
    await add(queue)
    await add(queue, comment_id=2)
    await add(queue, comment_id=3, pr_id=99)
    ado = Ado()
    started = []
    release = asyncio.Event()

    async def run(job, settings):
        started.append(job.comment_id)
        await release.wait()
        await ado.post_pr_review(settings, job, f"review-{job.comment_id}")

    worker = ReviewDispatcher(run, sessions=queue, ado_factory=lambda: ado)
    try:
        await advance(worker)
        assert await statuses(queue) == ["queued"] * 3
        assert len(ado.messages) == 3
        await worker.tick()
        # Wait on actual database transitions rather than sleeping for a review.
        async def wait_started():
            while len(started) < 2:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait_started(), 2)
        assert set(started) == {1, 3}
        await worker.tick()
        assert 2 not in started
        release.set()
        await asyncio.gather(*list(worker._running.values()))
        await finish(worker)
        assert started[-1] == 2
        assert await statuses(queue) == ["completed"] * 3
        assert [c for c, _ in ado.reviews if c != 3] == [1, 2]
    finally:
        release.set()
        await worker.stop()


async def test_restart_recovers_queued_but_does_not_replay_interrupted_review(queue):
    await add(queue, status="running")
    await add(queue, comment_id=2, status="queued")
    ado = Ado()
    ran = []
    async def run(job, settings):
        ran.append(job.comment_id)
        await ado.post_pr_review(settings, job, "review")
    worker = ReviewDispatcher(run, sessions=queue, ado_factory=lambda: ado, poll_seconds=.01)
    await worker.start()
    try:
        async def wait_done():
            while await statuses(queue) != ["failed", "completed"]:
                await asyncio.sleep(.01)
        await asyncio.wait_for(wait_done(), 3)
        assert ran == [2]
        assert any("interrupted" in msg for _, msg in ado.messages)
    finally:
        await worker.stop()


async def test_failure_releases_pr_for_next_request_and_reports_failure(queue):
    await add(queue)
    await add(queue, comment_id=2)
    ado = Ado()
    async def run(job, settings):
        if job.comment_id == 1:
            raise RuntimeError("secret diagnostic that must not appear in PR")
        await ado.post_pr_review(settings, job, "second review")
    worker = ReviewDispatcher(run, sessions=queue, ado_factory=lambda: ado)
    await advance(worker)
    await finish(worker)
    await finish(worker)
    assert await statuses(queue) == ["failed", "completed"]
    assert any("Review failed" in msg for _, msg in ado.messages)
    assert all("secret diagnostic" not in msg for _, msg in ado.messages)
    assert ado.reviews == [(2, "second review")]


async def test_pr_closed_during_review_does_not_receive_results(queue):
    await add(queue)
    ado = Ado()
    async def run(job, settings):
        ado.closed = True
        await ado.ensure_active(settings, job)
        await ado.post_pr_review(settings, job, "obsolete results")
    worker = ReviewDispatcher(run, sessions=queue, ado_factory=lambda: ado)
    await advance(worker)
    await finish(worker)
    assert await statuses(queue) == ["ignored"]
    assert ado.reviews == []


async def test_failed_result_post_is_not_reported_as_completed(queue):
    await add(queue)
    ado = Ado()
    ado.fail_post = True
    async def run(job, settings):
        await ado.post_pr_review(settings, job, "results")
    worker = ReviewDispatcher(run, sessions=queue, ado_factory=lambda: ado)
    await advance(worker)
    await finish(worker)
    assert await statuses(queue) == ["failed"]
    assert not any("Review completed" in msg for _, msg in ado.messages)
