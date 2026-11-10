"""Guarded rollout on ulhgpuhamster; invoke prepare/check/switch/verify.

Preserves the patched image and unrelated dirty source. No schema changes.
Never switches while reviews or requests are active. No settings/env output.
"""
import datetime
import hashlib
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.request

P = pathlib.Path('/home/daniel/agent-fight')
FILES = ['app/coordinator.py', 'app/review/ledger.py', 'app/review/models.py', 'app/runner.py']
TABLES = ['reviews','review_requests','review_findings','review_token_usage','users']


def run(*args, cwd=P):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def norm(path):
    return path.read_bytes().replace(b'\r\n', b'\n')


def db():
    return sqlite3.connect('file:' + str(P/'data/agent_fight.db') + '?mode=ro', uri=True)


def counts(connection):
    return {t: connection.execute('select count(*) from '+t).fetchone()[0] for t in TABLES}


def health():
    with urllib.request.urlopen('http://localhost:8890/health',timeout=2) as response:
        value=json.load(response)
    assert value == {'status':'healthy','review_trigger':'pr_comment'}, value
    return value


def prepare(bundle, commit):
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=P.parent/'agent-fight-backups'/('reliability-'+stamp)
    backup.mkdir(mode=0o700)
    with tarfile.open(bundle) as archive:
        archive.extractall(backup,filter='data')
    for file in FILES:
        assert norm(P/file)==norm(backup/'base'/file), 'Production differs from baseline: '+file
        loaded=run('docker','exec','agent-fight','sha256sum','/app/'+file).split()[0]
        assert loaded==hashlib.sha256((P/file).read_bytes()).hexdigest(), 'Image/source mismatch: '+file
    info=json.loads(run('docker','inspect','agent-fight'))[0]
    old_image=info['Image']
    rollback='agent-fight:before-'+backup.name
    run('docker','tag',old_image,rollback)
    candidate='agent-fight:reliability-'+commit[:7]
    originals=backup/'originals'
    for file in FILES+['docker-compose.yml']:
        (originals/file).parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(P/file,originals/file)
    with db() as connection, sqlite3.connect(backup/'preparation.db') as destination:
        connection.backup(destination)
    (backup/'working-tree.patch').write_text(run('git','diff','--binary'))
    stage=backup/'release'
    stage.mkdir()
    shutil.copytree(P/'app',stage/'app',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(P/'migrations',stage/'migrations')
    shutil.copy2(P/'alembic.ini',stage/'alembic.ini')
    for file in FILES:
        shutil.copy2(backup/'candidate'/file,stage/file)
    shutil.copytree(backup/'candidate/tests',stage/'tests')
    shutil.copy2(backup/'candidate/pytest.ini',stage/'pytest.ini')
    (stage/'Dockerfile').write_text('FROM '+old_image+'\nCOPY app/ /app/app/\n'
        'LABEL org.opencontainers.image.revision="'+commit+'"\n')
    meta=dict(backup=str(backup),commit=commit,old_image=old_image,
              old_tag=info['Config']['Image'],rollback=rollback,candidate=candidate,
              hashes={f:hashlib.sha256((backup/'candidate'/f).read_bytes()).hexdigest() for f in FILES})
    (backup/'deployment.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))


def check(backup,meta):
    # Test the staged production app against the candidate regression suite.
    result=subprocess.run([str(P/'.venv/bin/python'),'-m','pytest','-q'],cwd=backup/'release',
                          stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    (backup/'linux-tests.log').write_text(result.stdout)
    print(result.stdout)
    assert result.returncode==0, 'Linux regression suite failed'
    run('docker','build','-t',meta['candidate'],str(backup/'release'))
    # Exercise real runner/coordinator/ledger from the actual image. Pytest and
    # test-only libraries come from the existing host venv via a read-only mount.
    sites=list((P/'.venv/lib').glob('python*/site-packages'))
    assert len(sites)==1,sites
    command=['docker','run','--rm','--network','none','-e','PYTHONPATH=/app:/verification:/test-deps',
             '-v',str(sites[0])+':/test-deps:ro','-v',str(backup/'candidate')+':/verification:ro',
             '-w','/verification',meta['candidate'],'python','-m','pytest','-q','-p','no:cacheprovider',
             'tests/test_review_reliability.py','tests/test_finding_ledger.py','tests/test_runner.py',
             'tests/test_directed_review.py']
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    (backup/'image-tests.log').write_text(result.stdout)
    print(result.stdout)
    assert result.returncode==0, 'Actual image regression checks failed'
    for image in [meta['old_image'],meta['candidate']]:
        inventory=run('docker','run','--rm','--network','none',image,'python','-m','pip','list','--format=json')
        if image==meta['old_image']:
            packages=json.loads(inventory)
        else:
            assert json.loads(inventory)==packages, 'Python dependency inventory changed'
    print('Patched dependency inventories identical')
    meta['checked']=True
    meta['candidate_image']=run('docker','image','inspect','--format={{.Id}}',meta['candidate'])
    (backup/'deployment.json').write_text(json.dumps(meta,indent=2))


def switch(backup,meta):
    assert meta.get('checked'), 'Candidate has not passed checks'
    for file in FILES+['docker-compose.yml']:
        assert (P/file).read_bytes()==(backup/'originals'/file).read_bytes(), 'Concurrent source edit: '+file
    assert run('docker','inspect','--format={{.Image}}','agent-fight')==meta['old_image']
    assert run('docker','image','inspect','--format={{.Id}}',meta['candidate'])==meta['candidate_image']
    run('docker','pause','agent-fight')
    try:
        with db() as connection:
            active=connection.execute("select count(*) from reviews where status='running'").fetchone()[0]
            queued=connection.execute("select count(*) from review_requests where status in ('pending','queued','running','interrupted')").fetchone()[0]
            assert active==0 and queued==0, ('Active work; retry after completion',active,queued)
            before=counts(connection)
            with sqlite3.connect(backup/'pre-switch.db') as destination:
                connection.backup(destination)
    except BaseException:
        run('docker','unpause','agent-fight')
        raise
    try:
        for file in FILES:
            shutil.copy2(backup/'candidate'/file,P/file)
        compose=(P/'docker-compose.yml').read_text()
        assert compose.count('image: '+meta['old_tag'])==1
        (P/'docker-compose.yml').write_text(compose.replace('image: '+meta['old_tag'],'image: '+meta['candidate']))
        run('docker','rm','-f','agent-fight')
        run('docker','compose','up','-d','--no-build','--no-deps','agent-fight')
        for _ in range(60):
            try:
                value=health()
                break
            except Exception:
                time.sleep(.5)
        else:
            raise RuntimeError('Release startup failed')
        assert run('docker','inspect','--format={{.Image}}','agent-fight')==meta['candidate_image']
        for file,digest in meta['hashes'].items():
            assert run('docker','exec','agent-fight','sha256sum','/app/'+file).split()[0]==digest,file
        with db() as connection:
            assert connection.execute('pragma integrity_check').fetchone()[0]=='ok'
            after=counts(connection)
            assert after==before,(before,after)
    except BaseException:
        for file in FILES+['docker-compose.yml']:
            shutil.copy2(backup/'originals'/file,P/file)
        subprocess.run(['docker','unpause','agent-fight'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        run('docker','compose','up','-d','--no-build','--no-deps','--force-recreate','agent-fight')
        raise
    meta.update(deployed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),health=value,
                counts_before=before,counts_after=after)
    (backup/'deployment.json').write_text(json.dumps(meta,indent=2))
    print(json.dumps(meta,indent=2))


if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='prepare':
        prepare(sys.argv[2],sys.argv[3])
    else:
        backup=pathlib.Path(sys.argv[2])
        assert backup.resolve().parent==P.parent/'agent-fight-backups'
        meta=json.loads((backup/'deployment.json').read_text())
        if mode=='check': check(backup,meta)
        elif mode=='switch': switch(backup,meta)
        elif mode=='verify': print(json.dumps(dict(health=health(),image=run('docker','inspect','--format={{.Image}}','agent-fight'))))
        else: raise ValueError(mode)
