"""Coordinator-owned findings and explicit response obligations.

Agents submit partial responses; settled findings are never reconstructed from
the latest two files. A pending item records exactly one next responder.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.review.models import ConvergenceResult, Disagreement, Finding, ReviewFailure


def anchor(f: Finding):
    return (f.file.replace('\\', '/').removeprefix('./'), f.line)


def substantive(f: Finding):
    return (f.claim.strip(), f.severity.strip().lower())


@dataclass
class Entry:
    owner: str
    proposal: Finding
    positions: dict[str, Finding] = field(default_factory=dict)
    origins: set[str] = field(default_factory=set)
    pending: str | None = None
    state: str = 'open'
    reason: str = 'cross-review'


class FindingLedger:
    def __init__(self, independent: dict[str, list[Finding]]):
        self.roles = tuple(independent)
        self.entries: dict[str, Entry] = {}
        exact = {}
        for role, findings in independent.items():
            for index, finding in enumerate(findings, 1):
                key = (anchor(finding), substantive(finding))
                fid = exact.get(key) or f'{role}-0-{index}'
                finding = replace(finding, finding_id=fid, verdict='issue')
                if fid not in self.entries:
                    self.entries[fid] = Entry(role, finding)
                    exact[key] = fid
                entry = self.entries[fid]
                entry.positions[role] = finding
                entry.origins.add(role)

    def other(self, role):
        return next(r for r in self.roles if r != role)

    def assigned(self, role, round_no):
        return [fid for fid, e in self.entries.items()
                if e.state == 'open' and (round_no == 1 or e.pending == role)]

    def review(self, role):
        return [e.positions[role] for e in self.entries.values() if role in e.positions]

    def payload(self, role, ids):
        from dataclasses import asdict
        return [dict(finding_id=fid, owner=e.owner, reason=e.reason,
                     finding=asdict(e.proposal),
                     your_previous=asdict(e.positions[role]) if role in e.positions else None,
                     their_previous=asdict(e.positions[self.other(role)])
                         if self.other(role) in e.positions else None)
                for fid in ids for e in [self.entries[fid]]]

    def apply(self, responses: dict[str, list[Finding]], round_no: int,
              assignments: dict[str, list[str]]):
        # Normalize the entire batch before changing state, so parallel answers
        # are interpreted against the same opening snapshot.
        normalized = {}
        for role, findings in responses.items():
            normalized[role] = {}
            for index, finding in enumerate(findings, 1):
                fid = finding.finding_id
                if not fid:
                    candidates = [key for key in assignments[role]
                                  if anchor(self.entries[key].proposal) == anchor(finding)]
                    if len(candidates) > 1:
                        candidates = [key for key in candidates
                                      if self.entries[key].proposal.claim == finding.claim]
                        if len(candidates) != 1:
                            raise ReviewFailure('ambiguous finding anchor; supply finding_id', harness=role, round_no=round_no)
                    if len(candidates) == 1:
                        fid = candidates[0]
                    elif candidates:
                        raise ReviewFailure('ambiguous finding anchor; supply finding_id', harness=role, round_no=round_no)
                    else:
                        fid = f'{role}-{round_no}-{index}'
                if fid not in self.entries:
                    if not fid.startswith(f'{role}-{round_no}-'):
                        raise ReviewFailure(f'unknown finding_id {fid}', harness=role, round_no=round_no)
                    if finding.verdict not in ('issue', 'revised'):
                        raise ReviewFailure('a new finding must assert an issue', harness=role, round_no=round_no)
                    proposed = replace(finding, finding_id=fid)
                    self.entries[fid] = Entry(role, proposed, positions={role: proposed},
                                              origins={role}, pending=self.other(role),
                                              reason='respond to newly discovered finding')
                elif fid not in assignments[role]:
                    raise ReviewFailure(f'response to unassigned finding {fid}', harness=role, round_no=round_no)
                if fid in normalized[role]:
                    raise ReviewFailure(f'duplicate response for {fid}', harness=role, round_no=round_no)
                if finding.duplicate_of and (
                    role != self.entries[fid].owner or finding.verdict != 'resolved'
                ):
                    # Optional relationship metadata is not withdrawal authority.
                    # Keep the explicit verdict; the exact hint remains in raw output.
                    finding = replace(finding, duplicate_of='')
                if round_no == 1 and role == self.entries[fid].owner and finding.verdict == 'issue':
                    if substantive(finding) != substantive(self.entries[fid].proposal):
                        finding = replace(finding, verdict='revised')
                normalized[role][fid] = replace(finding, finding_id=fid)

        unbacked = self._unbacked_duplicates(normalized)

        for fid, entry in self.entries.items():
            updates = {r: fs[fid] for r, fs in normalized.items() if fid in fs}
            if entry.state != 'open' or not updates and round_no > 1:
                continue
            if fid in unbacked:
                entry.positions.update(updates)
                entry.pending, entry.reason = entry.owner, 'clarify duplicate withdrawal'
                continue
            if round_no == 1:
                entry.positions.update(updates)
                self._cross_review(entry, updates)
            else:
                responder = entry.pending
                if responder not in updates:
                    continue  # omission retains the obligation; silence is no verdict
                response = updates[responder]
                entry.positions[responder] = response
                self._respond(entry, responder, response)

    def _unbacked_duplicates(self, normalized):
        """Check simultaneous withdrawals without inventing a surviving claim.

        Only effective authored withdrawals form links. Shared original claims
        with another affirming origin survive. Historical withdrawn duplicates
        may lead to a live terminal, but an ordinary withdrawn endpoint cannot.
        Loops, including feeders into loops, remain answerable by their authors.
        """
        links, withdrawn = {}, set()
        for fid, entry in self.entries.items():
            positions = dict(entry.positions)
            if entry.state == 'open':
                positions.update({role: findings[fid] for role, findings in normalized.items()
                                  if fid in findings})
            own = positions.get(entry.owner)
            other = self.other(entry.owner)
            shared_survivor = (other in entry.origins and other in positions
                               and positions[other].verdict in ('issue', 'agree', 'revised'))
            if entry.state == 'withdrawn' or (
                entry.state == 'open' and own and own.verdict == 'resolved' and not shared_survivor
            ):
                withdrawn.add(fid)
                if own and own.verdict == 'resolved' and own.duplicate_of:
                    links[fid] = own.duplicate_of

        unbacked = set()
        for fid in links:
            seen, current = set(), fid
            while current in links and current not in seen:
                seen.add(current)
                current = links[current]
            if current in seen or current not in self.entries or current in withdrawn:
                unbacked.add(fid)
        return unbacked

    def _withdraw(self, entry, role):
        if role == entry.owner:
            other = self.other(role)
            if other in entry.origins and entry.positions[other].verdict in ('issue', 'agree', 'revised'):
                entry.owner = other
                entry.proposal = entry.positions[other]
                entry.state, entry.pending = 'settled', None
            else:
                entry.state, entry.pending = 'withdrawn', None
            return True
        return False

    def _cross_review(self, entry, updates):
        owner, other = entry.owner, self.other(entry.owner)
        own = entry.positions.get(owner)
        rival = entry.positions.get(other)
        if own and own.verdict == 'resolved' and self._withdraw(entry, owner):
            return
        if other not in updates:
            entry.pending, entry.reason = other, 'missing cross-review response'
            return
        if rival.verdict in ('disagree', 'resolved'):
            entry.pending, entry.reason = owner, 'answer objection'
            return
        if own and own.verdict == 'disagree':
            entry.pending, entry.reason = owner, 'clarify or withdraw your claim'
            return
        # A material proposal change must be acknowledged explicitly. Merely
        # writing agree against the old snapshot does not accept a new revision.
        if rival.verdict == 'revised' or own and own.severity.lower() != rival.severity.lower():
            if own and own.verdict == 'revised' and substantive(own) == substantive(rival):
                entry.proposal = own
                entry.state, entry.pending = 'settled', None
            else:
                entry.proposal = rival
                entry.pending, entry.reason = owner, 'accept or contest revised claim/severity'
        elif own and own.verdict == 'revised':
            entry.proposal = own
            entry.pending, entry.reason = other, 'accept or contest revised claim/severity'
        else:
            entry.proposal = own or rival
            entry.state, entry.pending = 'settled', None

    def _respond(self, entry, role, response):
        if response.verdict == 'resolved' and self._withdraw(entry, role):
            return
        if response.verdict == 'agree' and role == entry.owner and entry.reason in (
            'answer objection', 'clarify or withdraw your claim', 'clarify duplicate withdrawal'
        ):
            entry.pending, entry.reason = self.other(role), 'answer defense or revised claim'
        elif response.verdict == 'agree' and response.severity.lower() == entry.proposal.severity.lower():
            # agree accepts the current proposal, whose text stays authoritative.
            entry.state, entry.pending = 'settled', None
        elif response.verdict in ('issue', 'revised'):
            entry.proposal = response
            entry.pending, entry.reason = self.other(role), 'answer defense or revised claim'
        else:
            entry.pending, entry.reason = self.other(role), 'answer objection'

    @property
    def complete(self):
        return all(e.state != 'open' for e in self.entries.values())

    def result(self):
        agreed, disputes, unanswered = [], [], []
        for entry in self.entries.values():
            if entry.state == 'withdrawn':
                continue
            if entry.state == 'settled':
                agreed.append(entry.proposal)
            elif entry.reason == 'clarify duplicate withdrawal':
                # An unsafe attempted withdrawal must not replace the original
                # claim or look like a settled non-issue at the round cap.
                unanswered.append(entry.proposal)
            elif len(entry.positions) == 2:
                left, right = (entry.positions[r] for r in self.roles)
                disputes.append(Disagreement(entry.proposal.file, entry.proposal.line, left, right))
            else:
                unanswered.append(entry.proposal)
        return ConvergenceResult(agreed, disputes, unanswered)
