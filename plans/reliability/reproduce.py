"""Diagnostic reproduction against unmodified production logic; no services/DB writes."""
from dataclasses import replace

from app.review.ledger import FindingLedger
from app.review.models import Finding, ReviewFailure

for scenario in ('affirmative_hint', 'opponent_hint', 'mutual_withdrawal'):
    ledger = FindingLedger({'claude': [Finding('a.py', 10, 'leak')],
                            'codex': [Finding('a.py', 11, 'same leak')]})
    a, b = 'claude-0-1', 'codex-0-1'
    first, second = ledger.entries[a].proposal, ledger.entries[b].proposal
    if scenario == 'affirmative_hint':
        responses = {'claude': [replace(first, verdict='issue', duplicate_of=b)]}
    elif scenario == 'opponent_hint':
        responses = {'codex': [replace(first, verdict='agree', duplicate_of=b)]}
    else:
        responses = {'claude': [replace(first, verdict='resolved', duplicate_of=b)],
                     'codex': [replace(second, verdict='resolved', duplicate_of=a)]}
    try:
        ledger.apply(responses, 1, {'claude': [a, b], 'codex': [a, b]})
        print(scenario, 'completed')
    except ReviewFailure as exc:
        print(scenario, type(exc).__name__, str(exc))
