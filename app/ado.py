"""Azure DevOps: parse the inbound PR service-hook, post the review back.

`parse_pull_request_event` is pure — it turns the ADO service-hook JSON for a
`git.pullrequest.created` / `git.pullrequest.updated` event into a `ReviewJob`.
`AdoClient.post_pr_review` is the one side-effect: it posts the coordinator's
final review as a pull-request thread comment using the stored PAT.

ASSUMPTIONS (isolated here — confirm against the target org / a captured live
payload): the `resource.*` field paths below, that `remoteUrl` is the clone
URL, the `refs/heads/...` ref format, the REST api-version, the threads
endpoint, and PAT Basic-auth (`base64(":" + pat)`).
"""

from __future__ import annotations

import base64
from typing import Optional

import httpx

from app.orm import Settings
from app.review.models import ReviewJob

SUPPORTED_EVENTS = ("git.pullrequest.created", "git.pullrequest.updated")
ADO_API_VERSION = "7.1"

# Prepended to every posted review. REVIEW_MARKER is a stable substring of the
# header used to find our own thread on a re-review so we update it instead of
# duplicating; keep it stable across header wording changes.
REVIEW_MARKER = "consensus review"
REVIEW_HEADER = (
    "**Agent🥊🤖🥊Fight - consensus review** — Claude + Codex reviewed this PR "
    "independently and debated to convergence.\n\n"
)


class UnsupportedEvent(ValueError):
    """The payload is well-formed but not a PR event we act on."""


class MalformedPayload(ValueError):
    """A required field is missing or the wrong type."""


def _strip_ref(ref: str) -> str:
    """refs/heads/feature-x -> feature-x (leave other ref shapes untouched)."""
    prefix = "refs/heads/"
    return ref[len(prefix):] if ref.startswith(prefix) else ref


def _require(mapping: dict, key: str, path: str):
    if not isinstance(mapping, dict) or key not in mapping or mapping[key] in (None, ""):
        raise MalformedPayload(f"missing required field: {path}")
    return mapping[key]


def parse_pull_request_event(payload: dict, settings: Settings) -> ReviewJob:
    """Parse an ADO PR service-hook payload into a ReviewJob.

    Non-payload identifiers (organization, and project as a fallback) come from
    the admin-configured `settings`, since the org is not always echoed in the
    hook body.
    """
    if not isinstance(payload, dict):
        raise MalformedPayload("payload is not an object")

    event_type = payload.get("eventType")
    if event_type not in SUPPORTED_EVENTS:
        raise UnsupportedEvent(f"unsupported eventType: {event_type!r}")

    resource = _require(payload, "resource", "resource")
    repository = _require(resource, "repository", "resource.repository")

    pr_id_raw = _require(resource, "pullRequestId", "resource.pullRequestId")
    try:
        pr_id = int(pr_id_raw)
    except (TypeError, ValueError):
        raise MalformedPayload(f"resource.pullRequestId is not an int: {pr_id_raw!r}")

    source_ref = _require(resource, "sourceRefName", "resource.sourceRefName")
    target_ref = _require(resource, "targetRefName", "resource.targetRefName")
    repo_id = _require(repository, "id", "resource.repository.id")
    repo_name = _require(repository, "name", "resource.repository.name")
    clone_url = _require(repository, "remoteUrl", "resource.repository.remoteUrl")

    project_obj = repository.get("project") or {}
    project = project_obj.get("name") or (settings.ado_project or "")

    pr_web_url = ""
    links = resource.get("_links") or {}
    web = links.get("web") or {}
    if isinstance(web, dict):
        pr_web_url = web.get("href", "") or ""

    # Source-branch tip commit (best-effort; absent on some payload shapes).
    src_commit = ""
    lmsc = resource.get("lastMergeSourceCommit")
    if isinstance(lmsc, dict):
        src_commit = str(lmsc.get("commitId") or "")

    return ReviewJob(
        event_type=event_type,
        pr_id=pr_id,
        repo_id=str(repo_id),
        repo_name=str(repo_name),
        repo_clone_url=str(clone_url),
        source_ref=str(source_ref),
        target_ref=str(target_ref),
        source_branch=_strip_ref(str(source_ref)),
        target_branch=_strip_ref(str(target_ref)),
        organization=settings.ado_organization or "",
        project=project,
        pr_web_url=pr_web_url,
        source_commit=src_commit,
    )


class AdoNotConfigured(RuntimeError):
    """The ADO PAT needed to post back is unset."""


class AdoClient:
    """Posts the final review back to the pull request as a thread comment."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None, timeout: float = 30.0) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @staticmethod
    def _basic_auth(pat: str) -> str:
        token = base64.b64encode(f":{pat}".encode()).decode()
        return f"Basic {token}"

    def _threads_url(self, job: ReviewJob, suffix: str = "") -> str:
        return (
            f"https://dev.azure.com/{job.organization}/{job.project}"
            f"/_apis/git/repositories/{job.repo_id}"
            f"/pullRequests/{job.pr_id}/threads{suffix}?api-version={ADO_API_VERSION}"
        )

    def _headers(self, settings: Settings) -> dict[str, str]:
        return {
            "Authorization": self._basic_auth(settings.ado_pat),
            "content-type": "application/json",
        }

    async def post_pr_review(self, settings: Settings, job: ReviewJob, review_markdown: str) -> None:
        """Create a new PR thread with the review (with the header/marker)."""
        if not settings.ado_pat:
            raise AdoNotConfigured("ado_pat is not configured — set it at /admin/settings")
        body = {
            "comments": [
                {"parentCommentId": 0, "content": REVIEW_HEADER + review_markdown, "commentType": 1}
            ],
            "status": 1,
        }
        resp = await self._http().post(
            self._threads_url(job), headers=self._headers(settings), json=body
        )
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO thread post failed {resp.status_code}: {resp.text[:500]}")

    async def _find_our_thread(self, settings: Settings, job: ReviewJob) -> tuple[int, int] | None:
        """(thread_id, comment_id) of our prior review thread, by marker, or None."""
        resp = await self._http().get(self._threads_url(job), headers=self._headers(settings))
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO thread list failed {resp.status_code}: {resp.text[:300]}")
        for thread in resp.json().get("value", []):
            for comment in thread.get("comments", []):
                if REVIEW_MARKER in (comment.get("content") or ""):
                    return thread["id"], comment["id"]
        return None

    async def post_or_update_pr_review(
        self, settings: Settings, job: ReviewJob, review_markdown: str
    ) -> str:
        """Update our existing review thread if present, else create one.

        Returns "updated" or "created". Keeps a PR to a single, evolving review
        comment across re-runs rather than a pile of duplicates."""
        if not settings.ado_pat:
            raise AdoNotConfigured("ado_pat is not configured — set it at /admin/settings")
        found = await self._find_our_thread(settings, job)
        content = REVIEW_HEADER + review_markdown
        if found is None:
            await self.post_pr_review(settings, job, review_markdown)
            return "created"
        thread_id, comment_id = found
        url = self._threads_url(job, suffix=f"/{thread_id}/comments/{comment_id}")
        resp = await self._http().patch(url, headers=self._headers(settings), json={"content": content})
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO comment update failed {resp.status_code}: {resp.text[:500]}")
        return "updated"

    async def _reviewer_id(self, settings: Settings, job: ReviewJob) -> str:
        """The authenticated PAT user's identity id — the reviewer we vote as.

        NOTE: connectionData is a preview-only resource — it 400s on a plain
        `api-version=7.1` ("must supply -preview"), unlike the GA git endpoints,
        so this one call uses the preview suffix."""
        url = (
            f"https://dev.azure.com/{job.organization}"
            f"/_apis/connectionData?api-version={ADO_API_VERSION}-preview"
        )
        resp = await self._http().get(url, headers=self._headers(settings))
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO connectionData failed {resp.status_code}: {resp.text[:300]}")
        user = resp.json().get("authenticatedUser") or {}
        rid = user.get("id")
        if not rid:
            raise RuntimeError("ADO connectionData returned no authenticatedUser.id")
        return str(rid)

    async def set_pr_vote(self, settings: Settings, job: ReviewJob, vote: int) -> None:
        """Cast the PAT user's reviewer vote on the PR (10/5/-5/-10; 0 skips).

        Adds the PAT identity as a reviewer with the given vote (idempotent —
        PUT replaces any prior vote). ADO vote values: 10 approve, 5 approve with
        suggestions, 0 no vote, -5 wait for author, -10 reject."""
        if not settings.ado_pat:
            raise AdoNotConfigured("ado_pat is not configured — set it at /admin/settings")
        if not vote:
            return  # 0 = no vote; nothing to cast
        reviewer_id = await self._reviewer_id(settings, job)
        url = (
            f"https://dev.azure.com/{job.organization}/{job.project}"
            f"/_apis/git/repositories/{job.repo_id}"
            f"/pullRequests/{job.pr_id}/reviewers/{reviewer_id}?api-version={ADO_API_VERSION}"
        )
        resp = await self._http().put(url, headers=self._headers(settings), json={"vote": vote})
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO vote failed {resp.status_code}: {resp.text[:300]}")

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
