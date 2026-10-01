"""Durable inbox worker, with FIFO execution per PR and bounded concurrency.

Run exactly one application process against this SQLite database (the supplied
entrypoint does this). Pending/queued requests survive restarts. Interrupted
runs are reported as failures, never silently replayed after a possible post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from sqlalchemy import select, update

from app.ado import AdoClient, UnsupportedEvent
from app.db import SessionLocal
from app.orm import Review, ReviewRequest, Settings
from app.review.models import ReviewJob

logger = logging.getLogger("agent_fight.runner")

RunFn = Callable[[ReviewJob, Settings], Awaitable[None]]
ACTIVE = ("pending", "queued", "running", "interrupted")


class ReviewDispatcher:
    def __init__(self, run: Optional[RunFn] = None, *, sessions=None,
                 ado_factory=AdoClient, poll_seconds=1.0, max_parallel=2) -> None:
        self._run = run or _default_run
        self._sessions = sessions or SessionLocal
        self._ado_factory = ado_factory
        self._poll_seconds = poll_seconds
        self._max_parallel = max_parallel
        self._preparing: dict[int, asyncio.Task] = {}
        self._running: dict[int, asyncio.Task] = {}
        self._loop_task = None

    async def start(self):
        if self._loop_task is not None:
            raise RuntimeError("review worker already started")
        async with self._sessions() as session:
            await session.execute(update(ReviewRequest).where(
                ReviewRequest.status == "running").values(status="interrupted"))
            await session.execute(update(Review).where(Review.status == "running").values(
                status="failed", current_phase="failed",
                status_message="Review interrupted by a service restart. Request a new review to retry.",
                finished_at=datetime.now(timezone.utc).replace(tzinfo=None)))
            await session.commit()
        self._loop_task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        tasks = [*self._preparing.values(), *self._running.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._preparing.clear()
        self._running.clear()

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                logger.exception("review inbox poll failed")
            await asyncio.sleep(self._poll_seconds)

    def _spawn(self, tasks, request_id, coroutine):
        task = asyncio.create_task(coroutine)
        tasks[request_id] = task

        def done(finished):
            tasks.pop(request_id, None)
            if not finished.cancelled() and finished.exception():
                logger.error("review request %s task failed", request_id,
                             exc_info=finished.exception())
        task.add_done_callback(done)

    async def tick(self):
        async with self._sessions() as session:
            rows = (await session.scalars(select(ReviewRequest).where(
                ReviewRequest.status.in_(ACTIVE)).order_by(ReviewRequest.id))).all()
        seen_prs = set()
        for row in rows:
            job = ReviewJob(**json.loads(row.job_json))
            pr_key = (job.organization.lower(), job.repo_id.lower(), job.pr_id)
            first = pr_key not in seen_prs
            seen_prs.add(pr_key)
            # Acknowledge requests even while an earlier review of the PR runs.
            if (row.status in ("pending", "interrupted") and row.id not in self._preparing
                    and len(self._preparing) < 4):
                self._spawn(self._preparing, row.id, self._prepare(row.id))
            elif (first and row.status == "queued" and row.id not in self._running
                  and len(self._running) < self._max_parallel):
                self._spawn(self._running, row.id, self._execute(row.id))

    async def _load(self, request_id):
        async with self._sessions() as session:
            row = await session.get(ReviewRequest, request_id)
            settings = await session.get(Settings, 1)
            if settings is None:
                raise RuntimeError("ADO settings are missing")
            return row, settings, ReviewJob(**json.loads(row.job_json))

    async def _save(self, request_id, status, job=None):
        values = {"status": status}
        if job:
            values["job_json"] = json.dumps(asdict(job))
        async with self._sessions() as session:
            await session.execute(update(ReviewRequest).where(
                ReviewRequest.id == request_id).values(**values))
            await session.commit()

    async def _notify(self, ado, settings, job, message):
        if not job.comment_id:
            return
        try:
            await ado.request_status(settings, job, message)
        except Exception:
            logger.exception("could not update request status on PR %s", job.pr_id)

    async def _prepare(self, request_id):
        row, settings, job = await self._load(request_id)
        ado = self._ado_factory()
        try:
            if row.status == "interrupted":
                await self._notify(ado, settings, job,
                    "Review interrupted by a service restart. Post a new command to try again.")
                await self._save(request_id, "failed")
                return
            # A saved snapshot survives interruption while posting the acknowledgment.
            if not job.source_commit or not job.target_commit:
                job = await ado.prepare_request(settings, job)
                await self._save(request_id, "pending", job)
            if job.comment_id:
                await ado.request_status(settings, job,
                    f"Review queued for commit `{job.source_commit}`. Results will appear in a new review thread.")
            await self._save(request_id, "queued")
        except UnsupportedEvent:
            await self._notify(ado, settings, job, "Request skipped: the command changed or the PR is no longer active.")
            await self._save(request_id, "ignored")
        except Exception:
            logger.exception("could not prepare review request %s", request_id)
            await self._notify(ado, settings, job,
                "Could not queue the review. An operator can check the service logs; post a new command to retry.")
            await self._save(request_id, "failed")
        finally:
            await ado.aclose()

    async def _execute(self, request_id):
        _, settings, job = await self._load(request_id)
        ado = self._ado_factory()
        await self._save(request_id, "running")
        try:
            await ado.ensure_active(settings, job)
            await self._notify(ado, settings, job, f"Review running for commit `{job.source_commit}`.")
            await self._run(job, settings)
            # Commit completion before a best-effort notification, so notification
            # failure cannot cause an expensive rerun or erase a successful review.
            await self._save(request_id, "completed")
            await self._notify(ado, settings, job, f"Review completed for commit `{job.source_commit}`.")
        except UnsupportedEvent:
            await self._save(request_id, "ignored")
            await self._notify(ado, settings, job, "Review stopped: the PR is no longer active.")
        except Exception:
            logger.exception("review request %s failed", request_id)
            await self._save(request_id, "failed")
            await self._notify(ado, settings, job,
                "Review failed. An operator can check the service logs; post a new command to retry.")
        finally:
            await ado.aclose()


async def _default_run(job, settings):
    from app.runner import _default_run as run_and_record_review
    await run_and_record_review(job)
