"""Shared fixtures.

`session` is a plain in-memory database built from the ORM metadata, for
unit tests against ORM/query logic directly.

`client` wires a full FastAPI app (via `create_app()`) to a temporary SQLite
file, so concurrent HTTP requests and worker sessions have independent
transactions, and exposes it over an httpx AsyncClient. httpx persists cookies across requests on the same client
instance, so logging in via POST /login and then hitting an admin route
carries the session cookie automatically — the same pattern a real browser
session would follow.
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.orm import Base, User
from app.security import hash_password

ADMIN_USERNAME = "daniel"
ADMIN_PASSWORD = "test-password"


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """A session against a fresh in-memory database, torn down per test."""
    engine = create_async_engine("sqlite+aiosqlite://", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def client(tmp_path):
    """An httpx AsyncClient wired to the app over a temporary file database,
    pre-seeded with one operator account (ADMIN_USERNAME / ADMIN_PASSWORD)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    # Seed the operator the login() helper authenticates as.
    async with maker() as s:
        s.add(User(username=ADMIN_USERNAME, password_hash=hash_password(ADMIN_PASSWORD)))
        await s.commit()

    async def _get_test_session():
        async with maker() as s:
            try:
                yield s
            except Exception:
                await s.rollback()
                raise

    from app.db import get_session
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = _get_test_session

    # Replace the real dispatcher (which would launch Docker in the background)
    # with a no-op recorder, and expose it on the client for assertions.
    recorder = RecordingDispatcher()
    app.state.review_dispatcher = recorder

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.dispatcher = recorder  # type: ignore[attr-defined]
        ac.sessions = maker
        yield ac

    await engine.dispose()


class RecordingDispatcher:
    """Stand-in for ReviewDispatcher: records dispatched jobs, launches nothing."""

    def __init__(self) -> None:
        self.jobs = []

    def dispatch(self, job) -> None:
        self.jobs.append(job)


async def login(ac: AsyncClient) -> None:
    resp = await ac.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code in (200, 303)


def sample_ado_payload(event_type: str = "git.pullrequest.created") -> dict:
    """A minimal but well-formed ADO PR service-hook payload for tests."""
    return {
        "eventType": event_type,
        "resource": {
            "pullRequestId": 42,
            "createdBy": {"displayName": "Ada Lovelace", "uniqueName": "ada@example.com"},
            "sourceRefName": "refs/heads/feature-x",
            "targetRefName": "refs/heads/main",
            "repository": {
                "id": "repo-guid-123",
                "name": "widgets",
                "remoteUrl": "https://dev.azure.com/contoso/widgets/_git/widgets",
                "project": {"name": "widgets-project"},
            },
            "_links": {"web": {"href": "https://dev.azure.com/contoso/_git/widgets/pullrequest/42"}},
        },
    }


def sample_comment_payload(content="/agent-fight review", comment_id=1, thread_id=5):
    pr = sample_ado_payload()["resource"]
    pr.update(status="active", lastMergeSourceCommit={"commitId": "a" * 40},
              lastMergeTargetCommit={"commitId": "b" * 40})
    return {
        "id": "event-123",
        "eventType": "ms.vss-code.git-pullrequest-comment-event",
        "resource": {
            "pullRequest": pr,
            "comment": {
                "id": comment_id, "content": content, "commentType": "text",
                "author": {"id": "developer-id"},
                "_links": {"self": {"href": (
                    "https://dev.azure.com/contoso/widgets-project/_apis/git/"
                    f"repositories/repo-guid-123/pullRequests/42/threads/{thread_id}/comments/{comment_id}"
                )}},
            },
        },
    }
