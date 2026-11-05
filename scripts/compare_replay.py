"""Summarize recorded usage/time; substantive outcome comparison is manual."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path


def totals(records):
    by_role = defaultdict(Counter)
    for row in records:
        usage = row.get('usage', row)
        for field in ('input_tokens','output_tokens','cache_read_tokens','cache_creation_tokens'):
            by_role[row['role']][field] += usage.get(field,0)
    result = {'by_role':{role:dict(counts) for role,counts in by_role.items()}}
    for field in ('input_tokens','output_tokens','cache_read_tokens','cache_creation_tokens'):
        result[field] = sum(counts[field] for counts in by_role.values())
    result['app_reported_input_plus_output'] = result['input_tokens']+result['output_tokens']
    # Claude reports cached reads/writes separately from input_tokens. Codex's
    # input_tokens already includes cached tokens: do not count those twice.
    result['processed_tokens_including_cache'] = sum(
        c['input_tokens']+c['output_tokens'] +
        (c['cache_read_tokens']+c['cache_creation_tokens'] if role != 'codex' else 0)
        for role,c in by_role.items())
    return result


def compare(baseline, replay):
    before = totals(baseline['usage'])
    after = totals(replay['usage'])
    before['duration_seconds'] = baseline['review']['duration_ms']/1000
    after['duration_seconds'] = replay['duration_seconds']
    before['harness_invocations'] = len(baseline['findings'])
    after['harness_invocations'] = len(replay['raw_findings'])
    before['rounds_used'] = baseline['review']['rounds_used']
    after['rounds_used'] = replay['rounds_used']
    before['agreed_findings'] = baseline['review']['converged_count']
    after['agreed_findings'] = replay['converged_count']
    before['unresolved_disagreements'] = baseline['review']['disagreement_count']
    after['unresolved_disagreements'] = replay['disagreement_count']
    reductions = {key:round(100*(before[key]-after[key])/before[key],2)
                  for key in ('duration_seconds','harness_invocations','output_tokens',
                              'app_reported_input_plus_output','processed_tokens_including_cache')
                  if before[key]}
    return {'baseline_review_id':baseline['review']['id'], 'before':before, 'after':after,
            'reduction_percent':reductions,
            'token_note':'Cached tokens are repeated across tool/model turns, not unique source size or dollar cost. Codex cache counters are subsets of input; Claude cache counters are additional.'}


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('baseline',type=Path)
    parser.add_argument('replay',type=Path)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    result=compare(json.loads(args.baseline.read_text(encoding='utf-8-sig')),
                   json.loads(args.replay.read_text(encoding='utf-8-sig')))
    text=json.dumps(result,indent=2)
    if args.output:
        args.output.write_text(text,encoding='utf-8')
    print(text)
