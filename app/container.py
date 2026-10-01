"""Per-review container lifecycle.

Modeled on frame-main's DockerProvisioner (see plans/code-review-consensus.md):
one ephemeral container per PR review. The coordinator drives it — clone once,
fetch the target + PR refs, checkout the PR branch, run the round loop, then
tear the container down. A review is one-shot; there is no idle persistence.

The side-effectful Docker driver (`DockerRunner`) sits behind the
`ContainerRunner` / `ContainerHandle` protocols so the coordinator can be
exercised end-to-end against `FakeRunner` without Docker, the CLIs, or a live
proxy. `DockerRunner` shells out via stdlib asyncio subprocess — no docker SDK
dependency — and guarantees teardown in the context manager's `finally`.

ASSUMPTIONS (isolated to DockerRunner): the `docker` CLI is on PATH, the image
has git + the harness CLIs installed, and the clone-credential embedding used
below (token-in-URL / SSH key + GIT_SSH_COMMAND) matches the target host.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

from app.review.models import ReviewJob

logger = logging.getLogger("agent_fight.container")

WORKDIR = "/work/repo"
FINDINGS_DIR = "/work/findings"
# The review surface and the pull request itself, materialized as files the
# harnesses can read directly (they are also told how to hit the live PR API).
DIFF_PATH = "/work/pr.diff"
PR_CONTEXT_PATH = "/work/pr-context.md"
# HOME in the review image (Dockerfile.review), where Codex reads its config.
# The image runs as the non-root `node` user (Claude refuses
# --dangerously-skip-permissions as root).
CONTAINER_HOME = "/home/node"


@dataclass
class ExecResult:
    rc: int
    stdout: str
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


@runtime_checkable
class ContainerHandle(Protocol):
    """A running container the coordinator issues commands against."""

    async def exec(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ExecResult: ...

    async def read_file(self, path: str) -> str: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def stop(self) -> None: ...


class ContainerRunner(Protocol):
    """Starts a container for a job, yielding a handle and guaranteeing teardown."""

    def start(self, job: ReviewJob): ...  # -> AsyncContextManager[ContainerHandle]


# --------------------------------------------------------------------------- #
# Real Docker driver
# --------------------------------------------------------------------------- #


async def _run(
    argv: list[str], *, stdin: Optional[str] = None, timeout: Optional[float] = None
) -> ExecResult:
    """Run a process, treating ``timeout`` as maximum output inactivity.

    The clock resets whenever stdout or stderr yields bytes. This lets a
    productive long-running agent continue while still killing a process that
    has gone completely silent.
    """
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    feed = stdin.encode("utf-8") if stdin is not None else None
    stdout = bytearray()
    stderr = bytearray()
    tasks: dict[asyncio.Task, tuple[str, object]] = {}
    try:
        if feed is not None and proc.stdin is not None:
            proc.stdin.write(feed)
            await proc.stdin.drain()
            proc.stdin.close()

        assert proc.stdout is not None and proc.stderr is not None
        tasks[asyncio.create_task(proc.stdout.read(65536))] = ("stdout", proc.stdout)
        tasks[asyncio.create_task(proc.stderr.read(65536))] = ("stderr", proc.stderr)
        wait_task = asyncio.create_task(proc.wait())
        tasks[wait_task] = ("process", proc)

        while tasks:
            done, _ = await asyncio.wait(
                tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not done:
                raise asyncio.TimeoutError
            for task in done:
                kind, stream = tasks.pop(task)
                result = task.result()
                if kind == "process":
                    continue
                data = result
                if data:
                    (stdout if kind == "stdout" else stderr).extend(data)
                    tasks[asyncio.create_task(stream.read(65536))] = (kind, stream)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        for task in tasks:
            task.cancel()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return ExecResult(
        rc=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
    )


class DockerHandle:
    """A ContainerHandle backed by `docker exec` against a running container."""

    def __init__(self, container_id: str) -> None:
        self._cid = container_id

    async def exec(
        self,
        argv: list[str],
        *,
        env: Optional[dict[str, str]] = None,
        cwd: Optional[str] = None,
        stdin: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> ExecResult:
        cmd = ["docker", "exec"]
        if stdin is not None:
            cmd.append("-i")  # keep STDIN open so the prompt can be piped in
        for key, value in (env or {}).items():
            cmd += ["-e", f"{key}={value}"]
        if cwd:
            cmd += ["-w", cwd]
        cmd.append(self._cid)
        cmd += argv
        return await _run(cmd, stdin=stdin, timeout=timeout)

    async def read_file(self, path: str) -> str:
        res = await self.exec(["cat", path])
        if not res.ok:
            raise FileNotFoundError(f"read_file {path!r} failed: {res.stderr.strip()}")
        return res.stdout

    async def write_file(self, path: str, content: str) -> None:
        # Pipe content through `sh -c 'cat > path'` to avoid quoting the whole
        # body on the argv.
        quoted = shlex.quote(path)
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            self._cid,
            "sh",
            "-c",
            f"mkdir -p \"$(dirname {quoted})\" && cat > {quoted}",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate(content.encode("utf-8"))
        if proc.returncode != 0:
            raise RuntimeError(f"write_file {path!r} failed: {err.decode('utf-8', 'replace').strip()}")

    async def stop(self) -> None:
        await _run(["docker", "rm", "-f", self._cid])


def _docker_run_argv(image: str, mounts: list[tuple[str, str]]) -> list[str]:
    """Build the `docker run` argv, adding a read-only bind mount per skill.

    Pure so it can be unit-tested without Docker (mirrors
    frame-main/sandbox/provision.py's `-v host:container:ro`)."""
    argv = ["docker", "run", "-d", "--rm"]
    for host_path, container_path in mounts:
        argv += ["-v", f"{host_path}:{container_path}:ro"]
    argv += [image, "sleep", "infinity"]
    return argv


class DockerRunner:
    """Starts one ephemeral container per review via `docker run`.

    `mounts` are (host_path, container_path) pairs bind-mounted read-only — used
    to surface the operator-selected skills at the harness's skills dir (see
    app/skills.py::selected_skill_mounts)."""

    def __init__(
        self,
        image: str,
        *,
        mounts: Optional[list[tuple[str, str]]] = None,
        run_timeout: float = 60.0,
    ) -> None:
        self._image = image
        self._mounts = mounts or []
        self._run_timeout = run_timeout

    @asynccontextmanager
    async def start(self, job: ReviewJob) -> AsyncIterator[ContainerHandle]:
        res = await _run(
            _docker_run_argv(self._image, self._mounts),
            timeout=self._run_timeout,
        )
        if not res.ok:
            raise RuntimeError(f"docker run failed: {res.stderr.strip()}")
        container_id = res.stdout.strip()
        handle = DockerHandle(container_id)
        try:
            yield handle
        finally:
            # Teardown is guaranteed even if the review raised.
            try:
                await handle.stop()
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                logger.exception("failed to tear down container %s", container_id)


# --------------------------------------------------------------------------- #
# Repo preparation (single clone, no second worktree — per the plan)
# --------------------------------------------------------------------------- #


def _clone_url_with_token(clone_url: str, token: str) -> str:
    """Embed a token into an https ADO clone URL."""
    for scheme in ("https://", "http://"):
        if clone_url.startswith(scheme):
            rest = clone_url[len(scheme):]
            # Drop any pre-existing userinfo.
            if "@" in rest.split("/", 1)[0]:
                rest = rest.split("@", 1)[1]
            return f"{scheme}{token}@{rest}"
    return clone_url


async def prepare_repo(
    handle: ContainerHandle,
    job: ReviewJob,
    *,
    clone_credential_kind: str,
    clone_credential_secret: Optional[str],
) -> None:
    """Clone once, fetch target + PR refs, checkout the PR branch.

    `git diff origin/<target>...<source>` then gives the review surface, and
    full file content is present for any harness that needs more than the diff.
    """
    env: dict[str, str] = {}
    clone_url = job.repo_clone_url

    if clone_credential_kind == "token" and clone_credential_secret:
        clone_url = _clone_url_with_token(clone_url, clone_credential_secret)
    elif clone_credential_kind == "ssh_key" and clone_credential_secret:
        key_path = "/work/.ssh_id"
        await handle.write_file(key_path, clone_credential_secret)
        await handle.exec(["chmod", "600", key_path])
        env["GIT_SSH_COMMAND"] = f"ssh -i {key_path} -o StrictHostKeyChecking=no"

    # Prefer the immutable source SHA carried by the webhook.  ADO commonly
    # deletes the source branch when a PR completes, but the commit remains
    # fetchable; reviewing by SHA also prevents a force-push between webhook
    # receipt and checkout from changing the code under review.
    source = job.source_commit or job.source_ref
    checkout_argv = (
        ["git", "-C", WORKDIR, "checkout", "--detach", job.source_commit]
        if job.source_commit
        else ["git", "-C", WORKDIR, "checkout", job.source_branch]
    )
    steps = [
        (["git", "clone", clone_url, WORKDIR], None),
        (["git", "-C", WORKDIR, "fetch", "origin", job.target_ref, source], None),
        (checkout_argv, None),
        (["mkdir", "-p", FINDINGS_DIR], None),
    ]
    for argv, _ in steps:
        res = await handle.exec(argv, env=env or None)
        if not res.ok:
            raise RuntimeError(f"repo prep step {argv!r} failed: {res.stderr.strip()}")


async def provision_harnesses(handle: ContainerHandle, settings) -> None:
    """Write the per-harness proxy config into the container.

    Claude is configured purely through env vars at exec time (see
    app/harness.py::harness_env), so nothing to write for it. Codex reads
    ~/.codex/auth.json and ~/.codex/config.toml on every call, so those must
    exist before `codex` runs. Skipped for Codex when its proxy is unconfigured.
    """
    from app.harness import codex_auth_json, codex_base_url, codex_config_toml

    if settings.codex_proxy_url and settings.codex_proxy_key:
        await handle.write_file(
            f"{CONTAINER_HOME}/.codex/auth.json",
            codex_auth_json(settings.codex_proxy_key),
        )
        await handle.write_file(
            f"{CONTAINER_HOME}/.codex/config.toml",
            codex_config_toml(codex_base_url(settings.codex_proxy_url), settings.codex_model),
        )


async def compute_diff(handle: ContainerHandle, job: ReviewJob) -> str:
    """The target..source diff the harnesses review.

    Also written to `DIFF_PATH` in the container: the prompt inlines only a
    bounded excerpt, so the file is what a harness re-reads on a large PR or in
    a later fight round.
    """
    res = await handle.exec(
        ["git", "-C", WORKDIR, "diff", f"origin/{job.target_branch}...{job.source_commit or job.source_branch}"],
    )
    if not res.ok:
        raise RuntimeError(f"git diff failed: {res.stderr.strip()}")
    await handle.write_file(DIFF_PATH, res.stdout)
    return res.stdout


async def write_pr_context(handle: ContainerHandle, markdown: str) -> None:
    """Drop the rendered pull request (description, work items, every comment
    thread and reply) into the container at `PR_CONTEXT_PATH`."""
    await handle.write_file(PR_CONTEXT_PATH, markdown)


# --------------------------------------------------------------------------- #
# Fake driver for tests
# --------------------------------------------------------------------------- #


@dataclass
class FakeHandle:
    """In-memory ContainerHandle. Tests pre-seed `files` (path -> content) that
    `read_file` returns and record every `exec`/`write_file` for assertions."""

    files: dict[str, str] = field(default_factory=dict)
    exec_log: list[list[str]] = field(default_factory=list)
    stdin_log: list[Optional[str]] = field(default_factory=list)
    env_log: list[dict[str, str]] = field(default_factory=list)
    exec_results: dict[str, ExecResult] = field(default_factory=dict)
    stopped: bool = False

    async def exec(self, argv, *, env=None, cwd=None, stdin=None, timeout=None) -> ExecResult:
        self.exec_log.append(list(argv))
        self.stdin_log.append(stdin)
        self.env_log.append(dict(env or {}))
        key = " ".join(argv)
        for pattern, result in self.exec_results.items():
            if pattern in key:
                return result
        return ExecResult(rc=0, stdout="")

    async def read_file(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def stop(self) -> None:
        self.stopped = True


class FakeRunner:
    """A ContainerRunner yielding a pre-seeded FakeHandle and recording teardown."""

    def __init__(self, handle: Optional[FakeHandle] = None) -> None:
        self.handle = handle or FakeHandle()

    @asynccontextmanager
    async def start(self, job: ReviewJob) -> AsyncIterator[ContainerHandle]:
        try:
            yield self.handle
        finally:
            await self.handle.stop()
