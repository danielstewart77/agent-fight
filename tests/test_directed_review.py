"""Directed scheduling and final output through the real coordinator."""
import asyncio
import json

import pytest

from app.container import ExecResult, FakeHandle, FakeRunner
from app.coordinator import run_review
from app.review.models import ReviewFailure
from tests.test_coordinator import _job, _settings, _RecordingAdo


def item(fid, verdict='issue', file='a.py', line=10, claim='handle leaks', severity='major', **extra):
    return dict(finding_id=fid, file=file, line=line, claim=claim, severity=severity,
                verdict=verdict, evidence='a.py:10', suggestion='close the handle', **extra)


class ScriptedHandle(FakeHandle):
    """Only the external CLI/container boundary is scripted."""
    def __init__(self, scripts, barrier=False):
        super().__init__()
        self.scripts = scripts
        self.started = {}
        self.cancelled = False
        self.barrier = barrier

    async def exec(self, argv, **kwargs):
        if argv[0] not in ('claude', 'codex'):
            return await super().exec(argv, **kwargs)
        role = argv[0]
        prompt = kwargs['stdin']
        import re
        path = re.search(r'OUTPUT -- write JSON to (\S+)', prompt).group(1)
        rnd = 0 if 'independent.json' in path else int(re.search(r'round(\d+)', path).group(1))
        self.started.setdefault(rnd, set()).add(role)
        if self.barrier and rnd <= 1:
            async def both_started():
                while len(self.started[rnd]) != 2:
                    await asyncio.sleep(0)
            await asyncio.wait_for(both_started(), 0.2)
        script = self.scripts[(rnd, role)]
        if isinstance(script, Exception):
            raise script
        self.files[path] = json.dumps(script)
        return ExecResult(0, '')


async def test_round_zero_and_cross_review_run_concurrently():
    handle = ScriptedHandle({(0,'claude'):[item('claude-0-1')], (0,'codex'):[],
                             (1,'claude'):[item('claude-0-1')],
                             (1,'codex'):[item('claude-0-1','agree')]}, barrier=True)
    result = await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=None)
    assert result.status == 'converged'
    assert result.rounds_used == 1


async def test_only_challenged_author_runs_and_withdrawal_closes_old_objection():
    fid = 'claude-0-1'
    handle = ScriptedHandle({(0,'claude'):[item(fid)], (0,'codex'):[],
                             (1,'claude'):[item(fid)], (1,'codex'):[item(fid,'disagree',response='finally closes it')],
                             (2,'claude'):[item(fid,'resolved',response='the challenger is right')]})
    result = await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=None)
    assert result.status == 'converged' and result.rounds_used == 2
    assert result.disagreement_count == 0 and result.converged_count == 0
    assert 'handle leaks' not in result.review_md
    assert {(r.role, r.round_no) for r in result.raw_findings if r.round_no == 2} == {('claude', 2)}


async def test_defense_routes_back_to_challenger_and_preserves_settled_finding():
    disputed, settled = 'claude-0-1', 'codex-0-1'
    handle = ScriptedHandle({(0,'claude'):[item(disputed)], (0,'codex'):[item(settled,file='b.py',claim='missing guard')],
                             (1,'claude'):[item(disputed),item(settled,'agree',file='b.py',claim='missing guard')],
                             (1,'codex'):[item(disputed,'disagree',response='already closed'),item(settled,file='b.py',claim='missing guard')],
                             (2,'claude'):[item(disputed,'issue',response='early return bypasses finally')],
                             (3,'codex'):[item(disputed,'agree',response='early return confirms it')]})
    result = await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=None)
    assert result.status == 'converged' and result.rounds_used == 3
    assert result.converged_count == 2
    assert 'missing guard' in result.review_md
    assert {(r.role, r.round_no) for r in result.raw_findings if r.round_no > 1} == {('claude', 2), ('codex', 3)}


async def test_reciprocal_disputes_run_both_authors_then_both_challengers():
    left, right = 'claude-0-1', 'codex-0-1'
    handle = ScriptedHandle({
        (0,'claude'):[item(left)], (0,'codex'):[item(right,file='b.py',claim='missing guard')],
        (1,'claude'):[item(left),item(right,'disagree',file='b.py',claim='missing guard')],
        (1,'codex'):[item(left,'disagree'),item(right,file='b.py',claim='missing guard')],
        (2,'claude'):[item(left,response='early return leaks')],
        (2,'codex'):[item(right,file='b.py',claim='missing guard',response='zero input bypasses guard')],
        (3,'claude'):[item(right,'agree',file='b.py',claim='missing guard')],
        (3,'codex'):[item(left,'agree')],
    })
    result = await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=None)
    assert result.converged_count == 2 and result.status == 'converged'
    assert {(r.role,r.round_no) for r in result.raw_findings if r.round_no > 1} == {
        ('claude',2),('codex',2),('claude',3),('codex',3)}


async def test_authors_agree_to_own_claim_is_a_defense_not_challengers_acceptance():
    fid = 'claude-0-1'
    handle = ScriptedHandle({(0,'claude'):[item(fid)], (0,'codex'):[],
                             (1,'claude'):[item(fid)], (1,'codex'):[item(fid,'disagree')],
                             (2,'claude'):[item(fid,'agree',response='I still assert the defect')],
                             (3,'codex'):[item(fid,'disagree',response='the early return also disposes it')]})
    result = await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=None)
    assert result.status == 'capped' and result.disagreement_count == 1
    assert result.vote == -5


async def test_failed_parallel_review_cannot_post():
    handle = ScriptedHandle({(0,'claude'):RuntimeError('transport failure'), (0,'codex'):[]})
    ado = _RecordingAdo()
    with pytest.raises((RuntimeError, ReviewFailure)):
        await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=ado)
    assert ado.posted is None and handle.stopped


async def test_invalid_findings_envelope_cannot_appear_clean():
    handle = ScriptedHandle({(0,'claude'):{'unexpected':'not a review'}, (0,'codex'):[]})
    ado = _RecordingAdo()
    with pytest.raises(ReviewFailure):
        await run_review(_job(), _settings(), runner=FakeRunner(handle), ado_client=ado)
    assert ado.posted is None
