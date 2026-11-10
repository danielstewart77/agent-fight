"""Unattended recovery and durable failure evidence through production code."""
from dataclasses import replace
import asyncio

import pytest
from sqlalchemy import select

from app.container import ExecResult, FakeRunner
from app.coordinator import run_review
from app.orm import Review, ReviewFinding, ReviewTokenUsage
from app.review.ledger import FindingLedger
from app.review.models import Finding, ReviewFailure
from app.runner import run_and_record
from tests.test_coordinator import _job, _settings, _RecordingAdo
from tests.test_directed_review import ScriptedHandle, item
from tests.test_runner import _claude_usage_stdout, _codex_usage_stdout


@pytest.mark.parametrize('role,verdict,expected', [
    ('claude','issue','settled'), ('codex','agree','settled'),
    ('codex','disagree','open'), ('codex','resolved','open'),
])
def test_duplicate_hint_keeps_explicit_verdict_without_withdrawing_rivals(role,verdict,expected):
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak')],
                            'codex':[Finding('b.py',20,'missing guard')]})
    a,b='claude-0-1','codex-0-1'
    owner=replace(ledger.entries[a].proposal,verdict='issue')
    rival=replace(owner,verdict='agree')
    hint=replace(owner,verdict=verdict,duplicate_of=b)
    ledger.apply({'claude':[hint if role=='claude' else owner],
                  'codex':[hint if role=='codex' else rival]},1,
                 {'claude':[a,b],'codex':[a,b]})
    assert ledger.entries[a].state == expected
    assert ledger.entries[b].state == 'open'
    if expected == 'open':
        assert ledger.assigned('claude',2) == [a,b]
        assert ledger.assigned('codex',2) == []
    else:
        assert [f.claim for f in ledger.result().converged] == ['leak']


@pytest.mark.parametrize('target', ['missing', 'claude-0-1', 'codex-0-1'])
def test_duplicate_withdrawal_without_survivor_can_be_explicitly_clarified(target):
    ledger=FindingLedger({'claude':[Finding('a.py',10,'leak')],
                          'codex':[Finding('b.py',20,'missing guard')]})
    a,b='claude-0-1','codex-0-1'
    ledger.apply({'claude':[replace(ledger.entries[a].proposal,verdict='resolved',duplicate_of=target)],
                  'codex':[replace(ledger.entries[b].proposal,verdict='resolved')]},1,
                 {'claude':[a,b],'codex':[a,b]})
    assert not ledger.complete
    assert ledger.assigned('claude',2) == [a]
    assert [f.claim for f in ledger.result().unmatched] == ['leak']
    ledger.apply({'claude':[replace(ledger.entries[a].proposal,verdict='resolved')]},2,
                 {'claude':[a],'codex':[]})
    assert ledger.complete
    assert not ledger.result().converged and not ledger.result().unmatched


def test_duplicate_chain_preserves_live_terminal_claim():
    ledger=FindingLedger({'claude':[Finding('a.py',10,'first'),Finding('a.py',11,'second')],
                          'codex':[Finding('a.py',12,'survivor')]})
    a,b,c='claude-0-1','claude-0-2','codex-0-1'
    ledger.apply({'claude':[replace(ledger.entries[a].proposal,verdict='resolved',duplicate_of=b),
                            replace(ledger.entries[b].proposal,verdict='resolved',duplicate_of=c),
                            replace(ledger.entries[c].proposal,verdict='agree')],
                  'codex':[ledger.entries[c].proposal]},1,
                 {'claude':[a,b,c],'codex':[a,b,c]})
    assert ledger.complete
    assert [f.claim for f in ledger.result().converged] == ['survivor']


def test_multinode_duplicate_loop_and_its_feeder_preserve_every_claim():
    ledger=FindingLedger({'claude':[Finding('a.py',i,str(i)) for i in (1,2,3)],
                          'codex':[Finding('a.py',4,'4')]})
    ids=list(ledger.entries)
    targets=[ids[1],ids[3],ids[0],ids[0]]
    responses={'claude':[], 'codex':[]}
    for fid,target in zip(ids,targets):
        entry=ledger.entries[fid]
        responses[entry.owner].append(replace(entry.proposal,verdict='resolved',duplicate_of=target))
    ledger.apply(responses,1,{'claude':ids,'codex':ids})
    assert not ledger.complete
    assert {f.claim for f in ledger.result().unmatched} == {'1','2','3','4'}
    assert ledger.assigned('claude',2) == ids[:3]
    assert ledger.assigned('codex',2) == ids[3:]


def test_duplicate_metadata_cannot_withdraw_a_shared_claim_still_asserted_by_other_origin():
    common=Finding('a.py',10,'shared defect')
    ledger=FindingLedger({'claude':[common], 'codex':[common,Finding('b.py',20,'other')]})
    a,b='claude-0-1','codex-0-2'
    ledger.apply({'claude':[replace(ledger.entries[a].proposal,verdict='resolved',duplicate_of=b)],
                  'codex':[replace(ledger.entries[a].proposal,verdict='issue'),
                           replace(ledger.entries[b].proposal,verdict='resolved')]},1,
                 {'claude':[a,b],'codex':[a,b]})
    assert ledger.complete
    assert [f.claim for f in ledger.result().converged] == ['shared defect']


def test_later_duplicate_loop_remains_answerable_instead_of_erasing_both_claims():
    ledger=FindingLedger({'claude':[Finding('a.py',10,'first')],
                          'codex':[Finding('a.py',11,'second')]})
    a,b='claude-0-1','codex-0-1'
    ledger.apply({'claude':[ledger.entries[a].proposal,replace(ledger.entries[b].proposal,verdict='disagree')],
                  'codex':[ledger.entries[b].proposal,replace(ledger.entries[a].proposal,verdict='disagree')]},1,
                 {'claude':[a,b],'codex':[a,b]})
    ledger.apply({'claude':[replace(ledger.entries[a].proposal,verdict='resolved',duplicate_of=b)],
                  'codex':[replace(ledger.entries[b].proposal,verdict='resolved',duplicate_of=a)]},2,
                 {'claude':[a],'codex':[b]})
    assert ledger.assigned('claude',3) == [a] and ledger.assigned('codex',3) == [b]
    assert {f.claim for f in ledger.result().unmatched} == {'first','second'}


def cycle_scripts():
    a,b='claude-0-1','codex-0-1'
    return {(0,'claude'):[item(a)], (0,'codex'):[item(b,line=11,claim='same leak')],
            (1,'claude'):[item(a,'resolved',duplicate_of=b)],
            (1,'codex'):[item(b,'resolved',line=11,claim='same leak',duplicate_of=a)]}


async def test_duplicate_cycle_at_cap_publishes_preserved_concerns_with_wait_vote():
    result=await run_review(_job(),_settings(round_cap=1),
                            runner=FakeRunner(ScriptedHandle(cycle_scripts())),ado_client=None)
    assert result.status == 'capped' and result.vote == -5
    assert result.unmatched_count == 2
    assert 'handle leaks' in result.review_md and 'same leak' in result.review_md
    assert len(result.raw_findings) == 4


async def test_duplicate_cycle_recovers_through_directed_clarification_and_acceptance():
    a,b='claude-0-1','codex-0-1'
    scripts=cycle_scripts()
    scripts.update({(2,'claude'):[item(a)],
                    (2,'codex'):[item(b,'resolved',line=11,claim='same leak',duplicate_of=a)],
                    (3,'codex'):[item(a,'agree')]})
    result=await run_review(_job(),_settings(round_cap=3),
                            runner=FakeRunner(ScriptedHandle(scripts)),ado_client=None)
    assert result.status == 'converged' and result.converged_count == 1
    assert result.rounds_used == 3
    assert 'handle leaks' in result.review_md and 'same leak' not in result.review_md
    assert {(r.role,r.round_no) for r in result.raw_findings if r.round_no==3} == {('codex',3)}


class UsageHandle(ScriptedHandle):
    async def exec(self,argv,**kwargs):
        result=await super().exec(argv,**kwargs)
        if argv[0] in ('claude','codex'):
            return ExecResult(result.rc,_claude_usage_stdout() if argv[0]=='claude' else _codex_usage_stdout())
        return result


@pytest.mark.parametrize('failure', ['ledger','parse','transport'])
async def test_failed_review_retains_completed_raw_responses_and_usage(session,failure):
    a='claude-0-1'
    scripts={(0,'claude'):[item(a)],(0,'codex'):[],
             (1,'claude'):[item(a)],(1,'codex'):[item(a,'agree')]}
    if failure=='ledger':
        scripts[(1,'codex')]=[item(a,'agree'),item(a,'disagree')]
    elif failure=='parse':
        scripts[(1,'codex')]={'unexpected':'invalid envelope'}
    else:
        scripts[(1,'codex')]=RuntimeError('transport failed')
    ado=_RecordingAdo()
    with pytest.raises((ReviewFailure,RuntimeError)):
        await run_and_record(session,_job(),_settings(),runner=FakeRunner(UsageHandle(scripts)),ado_client=ado)
    review=(await session.execute(select(Review))).scalars().one()
    raw=(await session.execute(select(ReviewFinding))).scalars().all()
    usage=(await session.execute(select(ReviewTokenUsage))).scalars().all()
    expected={('claude',0),('codex',0),('claude',1)}
    if failure!='transport':
        expected.add(('codex',1))
    assert {(r.role,r.round_no) for r in raw} == expected
    assert {(r.role,r.round_no) for r in usage} == expected
    assert all(r.input_tokens == (100 if r.role=='claude' else 200) for r in usage)
    if failure=='parse':
        assert next(r.raw for r in raw if r.role=='codex' and r.round_no==1) == '{"unexpected": "invalid envelope"}'
    assert review.status == 'failed' and review.final_review_md is None
    assert ado.posted is None


async def test_cancelled_review_retains_previous_round_evidence_and_failed_status(session):
    ready=asyncio.Event()
    class BlockingHandle(UsageHandle):
        async def exec(self,argv,**kwargs):
            if argv[0] in ('claude','codex') and 'round1.json' in kwargs['stdin']:
                ready.set()
                await asyncio.Future()
            return await super().exec(argv,**kwargs)
    a='claude-0-1'
    handle=BlockingHandle({(0,'claude'):[item(a)],(0,'codex'):[]})
    task=asyncio.create_task(run_and_record(session,_job(),_settings(),
                                           runner=FakeRunner(handle),ado_client=None))
    await asyncio.wait_for(ready.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    review=(await session.execute(select(Review))).scalars().one()
    raw=(await session.execute(select(ReviewFinding))).scalars().all()
    usage=(await session.execute(select(ReviewTokenUsage))).scalars().all()
    assert review.status == 'failed' and review.final_review_md is None
    assert {(r.role,r.round_no) for r in raw} == {('claude',0),('codex',0)}
    assert {(r.role,r.round_no) for r in usage} == {('claude',0),('codex',0)}
