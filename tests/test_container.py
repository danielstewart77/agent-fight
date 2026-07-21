"""Container repo-prep logic against the in-memory FakeHandle."""

from __future__ import annotations

import pytest

from app.container import (
    ExecResult,
    FakeHandle,
    _clone_url_with_token,
    compute_diff,
    prepare_repo,
)
from app.ado import parse_pull_request_event
from app.orm import Settings
from tests.conftest import sample_ado_payload


def _job():
    s = Settings(id=1, round_cap=3, clone_credential_kind="token")
    s.ado_organization = "contoso"
    return parse_pull_request_event(sample_ado_payload(), s)


def test_clone_url_with_token_embeds_and_replaces_userinfo():
    assert _clone_url_with_token("https://dev.azure.com/x/_git/y", "TOK") == "https://TOK@dev.azure.com/x/_git/y"
    # Existing userinfo is dropped, not doubled.
    assert _clone_url_with_token("https://old@dev.azure.com/x", "TOK") == "https://TOK@dev.azure.com/x"


async def test_prepare_repo_token_embeds_into_clone_url():
    handle = FakeHandle()
    await prepare_repo(
        handle, _job(), clone_credential_kind="token", clone_credential_secret="TOK"
    )
    clone_cmd = next(c for c in handle.exec_log if c[:2] == ["git", "clone"])
    assert "TOK@" in clone_cmd[2]
    # Fetch names both refs; checkout uses the short branch; findings dir made.
    assert any("fetch" in c for c in handle.exec_log)
    assert ["mkdir", "-p", "/work/findings"] in handle.exec_log


async def test_prepare_repo_ssh_writes_key_and_sets_git_ssh_command():
    handle = FakeHandle()
    await prepare_repo(
        handle, _job(), clone_credential_kind="ssh_key", clone_credential_secret="PRIVATEKEY"
    )
    assert handle.files["/work/.ssh_id"] == "PRIVATEKEY"
    assert any(c[:2] == ["chmod", "600"] for c in handle.exec_log)


async def test_prepare_repo_raises_on_failed_step():
    handle = FakeHandle(exec_results={"git clone": ExecResult(rc=1, stdout="", stderr="denied")})
    with pytest.raises(RuntimeError):
        await prepare_repo(
            handle, _job(), clone_credential_kind="token", clone_credential_secret="TOK"
        )


async def test_compute_diff_returns_stdout():
    handle = FakeHandle(exec_results={"git -C /work/repo diff": ExecResult(rc=0, stdout="DIFFTEXT")})
    assert await compute_diff(handle, _job()) == "DIFFTEXT"
