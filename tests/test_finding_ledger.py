"""Real ledger transitions preserve review substance and obligations."""
from dataclasses import replace

import pytest

from app.coordinator import synthesize_final_review
from app.review.ledger import FindingLedger
from app.review.models import Finding, ReviewFailure


def response(ledger, fid, verdict, **changes):
    return replace(ledger.entries[fid].proposal, verdict=verdict, **changes)


def test_distinct_defects_on_same_line_survive_final_synthesis():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak'),Finding('a.py',10,'wrong ID')], 'codex':[]})
    ids = ledger.assigned('codex',1)
    ledger.apply({'claude':[], 'codex':[response(ledger,f,'agree') for f in ids]},1,
                 {'claude':ids,'codex':ids})
    result = ledger.result()
    md = synthesize_final_review(result.converged,result.disagreements,result.unmatched)
    assert 'leak' in md and 'wrong ID' in md
    assert len(result.converged) == 2


def test_severity_revision_requires_author_acceptance_before_settling():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak',severity='minor')], 'codex':[]})
    fid='claude-0-1'
    ledger.apply({'claude':[response(ledger,fid,'issue')],
                  'codex':[response(ledger,fid,'revised',severity='major')]},1,
                 {'claude':[fid],'codex':[fid]})
    assert not ledger.complete
    assert ledger.assigned('claude',2) == [fid] and ledger.assigned('codex',2) == []
    ledger.apply({'claude':[response(ledger,fid,'agree')]},2,{'claude':[fid],'codex':[]})
    assert ledger.complete and ledger.result().converged[0].severity == 'major'


def test_unanswered_finding_routes_to_reviewer_who_missed_it():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak')], 'codex':[]})
    ledger.apply({'claude':[], 'codex':[]},1,{'claude':['claude-0-1'],'codex':['claude-0-1']})
    assert ledger.assigned('codex',2) == ['claude-0-1']
    assert ledger.assigned('claude',2) == []
    ledger.apply({'codex':[response(ledger,'claude-0-1','agree')]},2,
                 {'claude':[],'codex':['claude-0-1']})
    assert ledger.complete


def test_new_finding_in_partial_round_requires_opponents_response():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak')], 'codex':[]})
    fid='claude-0-1'
    ledger.apply({'claude':[], 'codex':[response(ledger,fid,'disagree')]},1,
                 {'claude':[fid],'codex':[fid]})
    ledger.apply({'claude':[response(ledger,fid,'resolved'), Finding('b.py',20,'missing guard')]},2,
                 {'claude':[fid],'codex':[]})
    pending=ledger.assigned('codex',3)
    assert len(pending) == 1 and pending[0] != fid
    ledger.apply({'codex':[response(ledger,pending[0],'agree')]},3,{'claude':[],'codex':pending})
    assert ledger.complete and ledger.result().converged[0].claim == 'missing guard'


def test_duplicate_response_id_fails_instead_of_overwriting():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak')], 'codex':[]})
    fid='claude-0-1'
    with pytest.raises(ReviewFailure):
        ledger.apply({'codex':[response(ledger,fid,'agree'),response(ledger,fid,'disagree')]},1,
                     {'claude':[fid],'codex':[fid]})


def test_author_can_withdraw_duplicate_at_another_anchor():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak')], 'codex':[Finding('a.py',11,'same leak')]})
    left,right='claude-0-1','codex-0-1'
    ledger.apply({'claude':[response(ledger,left,'resolved',duplicate_of=right),response(ledger,right,'agree')],
                  'codex':[response(ledger,left,'agree'),response(ledger,right,'issue')]},1,
                 {'claude':[left,right],'codex':[left,right]})
    assert ledger.complete
    assert [f.claim for f in ledger.result().converged] == ['same leak']


def test_shared_original_finding_drops_when_one_withdraws_and_other_rejects():
    finding = Finding('a.py',10,'leak')
    ledger = FindingLedger({'claude':[finding],'codex':[finding]})
    fid='claude-0-1'
    ledger.apply({'claude':[response(ledger,fid,'resolved')],
                  'codex':[response(ledger,fid,'disagree')]},1,
                 {'claude':[fid],'codex':[fid]})
    assert ledger.complete
    result=ledger.result()
    assert not result.converged and not result.disagreements and not result.unmatched


def test_mutual_duplicate_withdrawals_remain_open_for_author_clarification():
    ledger = FindingLedger({'claude':[Finding('a.py',10,'leak')], 'codex':[Finding('a.py',11,'same leak')]})
    left,right='claude-0-1','codex-0-1'
    ledger.apply({'claude':[response(ledger,left,'resolved',duplicate_of=right)],
                  'codex':[response(ledger,right,'resolved',duplicate_of=left)]},1,
                 {'claude':[left,right],'codex':[left,right]})
    assert not ledger.complete
    assert ledger.assigned('claude',2) == [left]
    assert ledger.assigned('codex',2) == [right]
    assert {f.claim for f in ledger.result().unmatched} == {'leak', 'same leak'}
    # Explicitly keep one claim and consolidate the other, then require the
    # challenger to accept the retained claim against the new snapshot.
    ledger.apply({'claude':[response(ledger,left,'issue')],
                  'codex':[response(ledger,right,'resolved',duplicate_of=left)]},2,
                 {'claude':[left],'codex':[right]})
    assert ledger.assigned('codex',3) == [left]
    ledger.apply({'codex':[response(ledger,left,'agree')]},3,
                 {'claude':[],'codex':[left]})
    assert ledger.complete
    assert [f.claim for f in ledger.result().converged] == ['leak']
