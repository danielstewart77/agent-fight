"""Replay a saved review with readonly settings and no ADO writes.

Run in an isolated runner container with /baseline-db mounted readonly and the
candidate checkout as /candidate. Outputs contain no settings credentials.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import time

from app.ado import AdoClient
from app.container import DockerRunner
from app.coordinator import run_review
from app.orm import Settings
from app.review.models import ReviewJob
from app.skills import selected_skill_mounts


ROOT = Path('/candidate/plans/directed-review')
OUTPUT = Path('/results')


class HistoricalContext(AdoClient):
    """Exclude discussion published after the original review began.

    ADO does not provide historical versions of comment edits through this
    endpoint. Drop subsequently edited comments rather than leak later answers.
    Description and linked work items remain current; record that limitation.
    """
    def __init__(self, cutoff):
        super().__init__()
        self.cutoff = cutoff
        self.excluded = 0

    async def _get_json(self, settings, url, what):
        result = await super()._get_json(settings, url, what)
        if what == 'PR thread list':
            for thread in result.get('value', []):
                kept = []
                for comment in thread.get('comments', []):
                    date = comment.get('lastUpdatedDate') or comment.get('publishedDate')
                    when = datetime.fromisoformat(date.replace('Z', '+00:00')) if date else None
                    if when and when <= self.cutoff:
                        kept.append(comment)
                    else:
                        self.excluded += 1
                thread['comments'] = kept
        return result


class FrozenContext:
    def __init__(self, text):
        self.text = text

    async def fetch_pr_context(self, settings, job):
        return self.text


class RecordingRunner:
    def __init__(self, inner):
        self.inner = inner

    @asynccontextmanager
    async def start(self, job):
        async with self.inner.start(job) as handle:
            class RecordingHandle:
                async def exec(self, argv, **kwargs):
                    begun = time.monotonic()
                    result = await handle.exec(argv, **kwargs)
                    if argv[0] in ('claude','codex'):
                        prompt = kwargs.get('stdin', '')
                        import re
                        output = re.search(r'OUTPUT -- write JSON to (\S+)',prompt).group(1)
                        label = Path(output).stem
                        (OUTPUT / (label+'.stream.jsonl')).write_text(result.stdout)
                        (OUTPUT / (label+'.timing.json')).write_text(json.dumps({
                            'duration_seconds':time.monotonic()-begun,'rc':result.rc}))
                    return result

                async def read_file(self, path):
                    return await handle.read_file(path)

                async def write_file(self, path, content):
                    if path in ('/work/pr.diff','/work/pr-context.md'):
                        (OUTPUT / Path(path).name).write_text(content)
                    return await handle.write_file(path,content)
            yield RecordingHandle()


async def main():
    OUTPUT.mkdir(exist_ok=True)
    baseline = json.loads((ROOT/'baseline-1131.json').read_text(encoding='utf-8-sig'))
    job = ReviewJob(**baseline['job'])
    db = sqlite3.connect('file:/baseline-db/agent_fight.db?mode=ro',uri=True)
    db.row_factory = sqlite3.Row
    settings_row = dict(db.execute('select * from settings where id=1').fetchone())
    settings_row.pop('updated_at',None)
    settings = Settings(**settings_row)
    db.close()
    cutoff = datetime.fromisoformat(baseline['review']['started_at']).replace(tzinfo=timezone.utc)
    ado = HistoricalContext(cutoff)
    try:
        context = await ado.fetch_pr_context(settings,job)
    finally:
        await ado.aclose()
    # The review itself uses only the frozen context, preventing later PR
    # discussion from supplying answers through a live API call.
    settings.ado_pat = None
    events=[]
    async def progress(kind,phase,message,**kwargs):
        event=dict(kind=kind,phase=phase,message=message,**kwargs,
                   elapsed_seconds=round(time.monotonic()-begun,3))
        events.append(event)
        with (OUTPUT/'events.jsonl').open('a') as out:
            out.write(json.dumps(event)+'\n')
        print(json.dumps(event),flush=True)

    metadata = {'baseline_review_id':baseline['review']['id'], 'job':asdict(job),
                'claude_model':settings.claude_model,'codex_model':settings.codex_model,
                'round_cap':settings.round_cap,'excluded_comments':ado.excluded,
                'context_cutoff_utc':cutoff.isoformat(),
                'context_limitations':'Historical comment edits cannot be recovered; description and work items are current.',
                'post_back':False,'database_mode':'read-only',
                'candidate_coordinator_sha256':hashlib.sha256(Path('/candidate/app/coordinator.py').read_bytes()).hexdigest()}
    (OUTPUT/'metadata.json').write_text(json.dumps(metadata,indent=2))
    begun = time.monotonic()
    runner = RecordingRunner(DockerRunner('agent-fight-review:latest',mounts=selected_skill_mounts(settings)))
    try:
        outcome = await run_review(job,settings,runner=runner,ado_client=FrozenContext(context),
                                   post_back=False,progress=progress)
    except BaseException as exc:
        (OUTPUT/'failure.json').write_text(json.dumps({'error_type':type(exc).__name__,
                                                      'duration_seconds':time.monotonic()-begun}))
        raise
    result = asdict(outcome)
    result['duration_seconds']=time.monotonic()-begun
    result['events']=events
    (OUTPUT/'outcome.json').write_text(json.dumps(result,indent=2))
    (OUTPUT/'review.md').write_text(outcome.review_md)
    print(json.dumps({'completed':True,'status':outcome.status,'rounds':outcome.rounds_used,
                      'duration_seconds':result['duration_seconds']}),flush=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
