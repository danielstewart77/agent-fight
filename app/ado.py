"""Azure DevOps: parse the inbound PR service-hook, read the PR, post back.

`parse_pull_request_event` is pure — it turns the ADO service-hook JSON for a
`git.pullrequest.created` / `git.pullrequest.updated` event into a `ReviewJob`.
The side effects are `AdoClient.fetch_pr_context` (read the PR's description and
every comment thread, including the author's replies to our previous review) and
`post_pr_review` / `set_pr_vote` (write the result back), both using the stored
PAT. Every review — first or re-review — lands as its own new thread.

ASSUMPTIONS (isolated here — confirm against the target org / a captured live
payload): the `resource.*` field paths below, that `remoteUrl` is the clone
URL, the `refs/heads/...` ref format, the REST api-version, the threads
endpoint, and PAT Basic-auth (`base64(":" + pat)`).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import replace
import html
import logging
import re
from typing import Optional
from urllib.parse import quote, urlparse

import httpx

from app.orm import Settings
from app.review.models import ReviewJob

logger = logging.getLogger("agent_fight.ado")

SUPPORTED_EVENTS = ("git.pullrequest.created", "git.pullrequest.updated")
ADO_API_VERSION = "7.1"
COMMENT_EVENT = "ms.vss-code.git-pullrequest-comment-event"
REVIEW_COMMAND = "/agent-fight review"

# Prepended to every posted review. REVIEW_MARKER is a stable substring of the
# header used to recognise our own threads on a PR — to count them (so a
# re-review is numbered) and to flag them for the reviewers reading the PR
# context. It must appear verbatim in EVERY header below; keep it stable across
# header wording changes, since threads posted by older versions still carry it.
REVIEW_MARKER = "consensus review"
REVIEW_HEADER = (
    "**Agent🥊🤖🥊Fight - consensus review** — Claude + Codex reviewed this PR "
    "independently and debated to convergence.\n\n"
)


def review_header(revision: int) -> str:
    """Header for the `revision`-th review of a pull request (1 = the first).

    Every review lands as its OWN new thread, so each one needs a header that
    says where it sits in the sequence. We used to edit the first review's
    comment in place, which broke down in practice: the author resolves that
    comment once they have pushed a fix, and a resolved thread is the last place
    anyone looks for the next review."""
    if revision <= 1:
        return REVIEW_HEADER
    return (
        f"**Agent🥊🤖🥊Fight - consensus review (re-review #{revision})** — the PR "
        "was submitted for another review; Claude + Codex reviewed it independently and debated "
        "to convergence. This supersedes our earlier review comment(s) here.\n\n"
    )


class UnsupportedEvent(ValueError):
    """The payload is well-formed but not a PR event we act on."""


class MalformedPayload(ValueError):
    """A required field is missing or the wrong type."""


def _object(value, path):
    if not isinstance(value, dict):
        raise MalformedPayload(f"{path} must be an object")
    return value


def is_review_command(comment: dict) -> bool:
    return (
        not comment.get("isDeleted")
        and comment.get("commentType") in (1, "text")
        and isinstance(comment.get("content"), str)
        and comment["content"].strip() == REVIEW_COMMAND
    )


def request_key(job: ReviewJob) -> str:
    # Event IDs can change on comment edits; a comment is a request only once.
    identity = f"{job.organization.lower()}/{job.repo_id.lower()}/{job.pr_id}/{job.thread_id}/{job.comment_id}"
    return hashlib.sha256(identity.encode()).hexdigest()


def parse_comment_event(payload: dict, settings: Settings) -> ReviewJob:
    _object(payload, "payload")
    if payload.get("eventType") != COMMENT_EVENT:
        raise UnsupportedEvent("only PR comment commands request reviews")
    resource = _object(payload.get("resource"), "resource")
    comment = _object(resource.get("comment"), "resource.comment")
    if not is_review_command(comment):
        raise UnsupportedEvent("not a review command")
    pr = _object(resource.get("pullRequest"), "resource.pullRequest")
    if pr.get("status") != "active":
        raise UnsupportedEvent("pull request is not active")
    job = parse_pull_request_event({"eventType": "git.pullrequest.created", "resource": pr}, settings)
    author = _object(comment.get("author"), "resource.comment.author")
    requester = _require(author, "id", "resource.comment.author.id")
    links = _object(comment.get("_links"), "resource.comment._links")
    self_link = _object(links.get("self"), "resource.comment._links.self")
    href = self_link.get("href")
    if not isinstance(href, str):
        raise MalformedPayload("comment self link is missing")
    # Extract identifiers only. Never send credentials to a webhook-supplied link.
    match = re.search(r"/repositories/([^/]+)/pullRequests/(\d+)/threads/(\d+)/comments/(\d+)/?$",
                      urlparse(href).path, re.IGNORECASE)
    if (not match or match[1].lower() != job.repo_id.lower()
            or int(match[2]) != job.pr_id or str(comment.get("id")) != match[4]
            or int(match[3]) <= 0 or int(match[4]) <= 0):
        raise MalformedPayload("comment self link does not identify this PR and comment")
    return replace(job, event_type=COMMENT_EVENT, event_id=str(payload.get("id") or ""),
                   thread_id=int(match[3]), comment_id=int(match[4]), requester_id=str(requester))


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

    resource = _object(_require(payload, "resource", "resource"), "resource")
    repository = _object(_require(resource, "repository", "resource.repository"), "resource.repository")

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
    created_by_obj = resource.get("createdBy") or {}
    created_by = str(created_by_obj.get("displayName") or created_by_obj.get("uniqueName") or "")

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
        created_by=created_by,
        source_commit=src_commit,
    )


def pr_api_base(job: ReviewJob) -> str:
    """The REST base for this PR, path-encoded so it is safe in a shell.

    Shared by the client below and by the URL handed to the review harnesses
    (see harness.ado_env) so both hit exactly the same resource. Project names
    routinely contain spaces ("IT Development"), which httpx tolerates but
    `curl` inside the container does not."""
    org = quote(job.organization or "", safe="")
    project = quote(job.project or "", safe="")
    return (
        f"https://dev.azure.com/{org}/{project}"
        f"/_apis/git/repositories/{job.repo_id}/pullRequests/{job.pr_id}"
    )


def basic_auth_header(pat: str) -> str:
    """ADO PAT Basic-auth header value: base64(":" + pat)."""
    return f"Basic {base64.b64encode(f':{pat}'.encode()).decode()}"


_TAG_RE = re.compile(r"<[^>]+>")


def _detag(text: str) -> str:
    """ADO work-item descriptions are HTML; flatten to something readable."""
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text or "")
    return html.unescape(_TAG_RE.sub("", text)).strip()


class AdoNotConfigured(RuntimeError):
    """The ADO PAT needed to post back is unset."""


class AdoClient:
    """Posts the final review back to the pull request as a thread comment."""

    def __init__(self, *, client: Optional[httpx.AsyncClient] = None, timeout: float = 30.0) -> None:
        self._client = client
        self._owns_client = client is None
        self._timeout = timeout
        self._identity = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    @staticmethod
    def _basic_auth(pat: str) -> str:
        return basic_auth_header(pat)

    def _threads_url(self, job: ReviewJob, suffix: str = "") -> str:
        return f"{pr_api_base(job)}/threads{suffix}?api-version={ADO_API_VERSION}"

    def _headers(self, settings: Settings) -> dict[str, str]:
        return {
            "Authorization": self._basic_auth(settings.ado_pat),
            "content-type": "application/json",
        }

    async def _get_json(self, settings: Settings, url: str, what: str) -> dict:
        resp = await self._http().get(url, headers=self._headers(settings))
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO {what} failed {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _pr_url(self, job: ReviewJob, suffix: str = "") -> str:
        return (
            f"https://dev.azure.com/{quote(job.organization, safe='')}/{quote(job.project, safe='')}"
            f"/_apis/git/repositories/{quote(job.repo_id, safe='')}"
            f"/pullRequests/{job.pr_id}{suffix}?api-version={ADO_API_VERSION}"
        )


    async def _get(self, settings: Settings, url: str) -> dict:
        response = await self._http().get(url, headers=self._headers(settings))
        response.raise_for_status()
        return response.json()


    async def _authenticated_id(self, settings: Settings) -> str:
        cached = self._identity
        if cached and cached[:2] == (settings.ado_organization, settings.ado_pat):
            return cached[2]
        data = await self._get(settings,
            f"https://dev.azure.com/{quote(settings.ado_organization, safe='')}"
            "/_apis/connectionData?api-version=7.1-preview.1")
        identity = data["authenticatedUser"]["id"]
        self._identity = (settings.ado_organization, settings.ado_pat, identity)
        return identity


    async def prepare_request(self, settings: Settings, job: ReviewJob) -> ReviewJob:
        """Verify the live command and resolve the exact commits before acknowledging."""
        if job.comment_id:
            comment = await self._get(settings, self._threads_url(
                job, f"/{job.thread_id}/comments/{job.comment_id}"))
            if (not is_review_command(comment)
                    or (comment.get("author") or {}).get("id") != job.requester_id):
                raise UnsupportedEvent("request comment was deleted or changed")
        pr = await self._get(settings, self._pr_url(job))
        if pr.get("status") != "active":
            raise UnsupportedEvent("pull request is no longer active")
        source = (pr.get("lastMergeSourceCommit") or {}).get("commitId", "")
        target = (pr.get("lastMergeTargetCommit") or {}).get("commitId", "")
        if not all(isinstance(sha, str) and re.fullmatch(r"[a-fA-F0-9]{40}", sha)
                   for sha in (source, target)):
            raise RuntimeError("ADO did not return source and target commit IDs")
        current = parse_pull_request_event({"eventType": "git.pullrequest.created", "resource": pr}, settings)
        if current.pr_id != job.pr_id or current.repo_id.lower() != job.repo_id.lower():
            raise RuntimeError("ADO returned a different pull request")
        return replace(current, event_type=COMMENT_EVENT, event_id=job.event_id,
                       thread_id=job.thread_id, comment_id=job.comment_id,
                       requester_id=job.requester_id, source_commit=source, target_commit=target)


    async def ensure_active(self, settings: Settings, job: ReviewJob) -> None:
        pr = await self._get(settings, self._pr_url(job))
        if pr.get("status") != "active":
            raise UnsupportedEvent("pull request is no longer active")


    async def request_status(self, settings: Settings, job: ReviewJob, message: str) -> None:
        """One evolving reply to the command, recoverable after a service restart."""
        marker = f"<!-- agent-fight-request:{request_key(job)} -->"
        author_id = await self._authenticated_id(settings)
        thread = await self._get(settings, self._threads_url(job, f"/{job.thread_id}"))
        content = f"**Agent Fight** — {message}\n\n{marker}"
        existing = next((c for c in thread.get("comments", [])
                         if not c.get("isDeleted") and (c.get("author") or {}).get("id") == author_id
                         and (c.get("content") or "").startswith("**Agent Fight** — ")
                         and (c.get("content") or "").endswith(marker)), None)
        if existing:
            response = await self._http().patch(
                self._threads_url(job, f"/{job.thread_id}/comments/{existing['id']}"),
                headers=self._headers(settings), json={"content": content})
        else:
            response = await self._http().post(
                self._threads_url(job, f"/{job.thread_id}/comments"), headers=self._headers(settings),
                json={"parentCommentId": job.comment_id, "content": content, "commentType": 1})
        response.raise_for_status()


    async def fetch_pull_request(self, settings: Settings, pr_id: int) -> dict:
        """Fetch the current PR resource used to build a manual-retry job."""
        if not settings.ado_organization or not settings.ado_project or not settings.ado_pat:
            raise AdoNotConfigured("Azure DevOps organization, project, and PAT are required")
        org = quote(settings.ado_organization, safe="")
        project = quote(settings.ado_project, safe="")
        return await self._get_json(
            settings,
            f"https://dev.azure.com/{org}/{project}/_apis/git/pullrequests/{pr_id}"
            f"?api-version={ADO_API_VERSION}",
            "PR detail",
        )

    async def _work_items_section(self, settings: Settings, job: ReviewJob) -> list[str]:
        """Linked work items with title + description, or [] if unavailable.

        Best-effort: work items are enrichment, and a project without the Boards
        service (or a PAT lacking work-item scope) must not fail a review."""
        try:
            refs = await self._get_json(
                settings,
                f"{pr_api_base(job)}/workitems?api-version={ADO_API_VERSION}",
                "PR work items",
            )
            ids = [str(w.get("id")) for w in refs.get("value", []) if w.get("id")]
            if not ids:
                return []
            org = quote(job.organization or "", safe="")
            detail = await self._get_json(
                settings,
                f"https://dev.azure.com/{org}/_apis/wit/workitems"
                f"?ids={','.join(ids)}"
                "&fields=System.WorkItemType,System.Title,System.Description"
                f"&api-version={ADO_API_VERSION}",
                "work item detail",
            )
        except Exception as exc:  # noqa: BLE001 - enrichment only
            logger.warning("PR %s: could not read linked work items: %s", job.pr_id, exc)
            return []

        lines = ["## Linked work items", ""]
        for item in detail.get("value", []):
            fields = item.get("fields") or {}
            kind = fields.get("System.WorkItemType") or "Work item"
            lines.append(f"### {kind} #{item.get('id')} — {fields.get('System.Title') or ''}")
            body = _detag(fields.get("System.Description") or "")
            lines.append(body or "_(no description)_")
            lines.append("")
        return lines

    async def fetch_pr_context(self, settings: Settings, job: ReviewJob) -> str:
        """The whole pull request as markdown, for the reviewers to read.

        Title, description, linked work items, and EVERY comment thread —
        including our own previous review and, crucially, the author's replies
        to it. A human rebuttal ("your reviewer was wrong, here is why") is
        evidence, and re-posting a finding the author already refuted is the
        worst thing this service can do. The reviewers can also hit the live PR
        API themselves (see harness.ado_env); this file is the guarantee that
        they cannot miss the conversation even if they never call it.

        The PR and its threads are required — a failure here fails the review
        (fail-closed, same as any other missing input). Work items are optional.
        """
        if not settings.ado_pat:
            raise AdoNotConfigured("ado_pat is not configured — set it at /admin/settings")

        base = pr_api_base(job)
        pr = await self._get_json(settings, f"{base}?api-version={ADO_API_VERSION}", "PR detail")
        threads = await self._get_json(
            settings, f"{base}/threads?api-version={ADO_API_VERSION}", "PR thread list"
        )

        created_by = (pr.get("createdBy") or {}).get("displayName") or "unknown"
        lines = [
            f"# Pull request !{job.pr_id} — {pr.get('title') or ''}",
            "",
            f"- Repository: `{job.repo_name}` ({job.organization} / {job.project})",
            f"- Branch: `{job.source_branch}` -> `{job.target_branch}`",
            f"- Author: {created_by}",
            f"- Status: {pr.get('status') or 'unknown'}",
        ]
        if job.pr_web_url:
            lines.append(f"- Web: {job.pr_web_url}")
        lines += [
            f"- REST base: `{base}` (append e.g. `/threads?api-version={ADO_API_VERSION}`)",
            "",
            "## Description",
            "",
            (pr.get("description") or "_(no description)_").strip(),
            "",
        ]
        lines += await self._work_items_section(settings, job)

        rendered: list[str] = []
        thread_count = 0
        for thread in threads.get("value", []):
            comments = [
                c for c in (thread.get("comments") or [])
                if (c.get("commentType") or "text") != "system"
            ]
            if not comments:
                continue
            thread_count += 1
            ctx = thread.get("threadContext") or {}
            where = ctx.get("filePath") or ""
            line_no = ((ctx.get("rightFileStart") or {}) or {}).get("line")
            anchor = f" on `{where}`" + (f":{line_no}" if line_no else "") if where else ""
            head = f"### Thread {thread.get('id')}{anchor} (status: {thread.get('status') or 'active'})"
            if any(REVIEW_MARKER in (c.get("content") or "") for c in comments):
                head += (
                    "\n\n> This is agent-fight's own previous review of this pull "
                    "request. Any replies below are the author answering it — treat "
                    "them as evidence, not as noise."
                )
            rendered.append(head)
            for c in comments:
                who = (c.get("author") or {}).get("displayName") or "unknown"
                when = c.get("publishedDate") or ""
                reply = " (reply)" if c.get("parentCommentId") else ""
                rendered.append(f"\n**{who}**{reply} — {when}\n")
                rendered.append((c.get("content") or "").strip())
            rendered.append("")

        lines.append(f"## Comment threads ({thread_count})")
        lines.append("")
        lines += rendered or ["_(no comments yet)_"]
        return "\n".join(lines).rstrip() + "\n"

    async def _create_review_thread(self, settings: Settings, job: ReviewJob, content: str) -> None:
        """POST `content` as a new, active comment thread on the PR."""
        body = {
            "comments": [{"parentCommentId": 0, "content": content, "commentType": 1}],
            "status": 1,
        }
        resp = await self._http().post(
            self._threads_url(job), headers=self._headers(settings), json=body
        )
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO thread post failed {resp.status_code}: {resp.text[:500]}")

    async def _count_our_threads(self, settings: Settings, job: ReviewJob) -> int:
        """How many review threads of ours are already on this PR (by marker)."""
        resp = await self._http().get(self._threads_url(job), headers=self._headers(settings))
        if resp.status_code // 100 != 2:
            raise RuntimeError(f"ADO thread list failed {resp.status_code}: {resp.text[:300]}")
        return sum(
            1
            for thread in resp.json().get("value", [])
            if any(REVIEW_MARKER in (c.get("content") or "") for c in thread.get("comments", []))
        )

    async def post_pr_review(
        self, settings: Settings, job: ReviewJob, review_markdown: str
    ) -> int:
        """Post the review as a NEW thread on the PR; returns its revision number.

        Always a new thread, never an edit of the previous one: the author
        resolves our comment when they act on it, and a re-review appended to a
        resolved thread goes unread (and reads as a resolved comment reopening
        itself). Counting our prior threads is best-effort — if the thread list
        is unreadable we still post, just unnumbered, rather than lose a review
        that took several rounds to produce."""
        if not settings.ado_pat:
            raise AdoNotConfigured("ado_pat is not configured — set it at /admin/settings")
        try:
            revision = await self._count_our_threads(settings, job) + 1
        except Exception as exc:  # noqa: BLE001 - numbering is cosmetic, the review is not
            logger.warning("PR %s: could not count prior review threads: %s", job.pr_id, exc)
            revision = 1
        if job.event_type in (COMMENT_EVENT, "manual.retry"):
            await self.ensure_active(settings, job)
        await self._create_review_thread(settings, job, review_header(revision) + review_markdown)
        return revision

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
