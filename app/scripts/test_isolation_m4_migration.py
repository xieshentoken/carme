"""Rehearse additive M4 migration and archived rollback with an explicit pre-M4 Store source.

Usage: python test_isolation_m4_migration.py <pre-M4 store.py> <evidence.json>
Only synthetic data in a fresh temporary account is mutated; the supplied source is read-only.
"""
import hashlib, importlib.util, json, sqlite3, sys, tempfile, time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from carme.store import Store

def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value

legacy_path=Path(sys.argv[1]).resolve();output=Path(sys.argv[2]).resolve()
legacy=module('pre_m4_store',legacy_path)
cli=module('m4_launcher',Path(__file__).with_name('carme_docker.py'))
root=Path(tempfile.mkdtemp(prefix='carme-m4-migration-',dir=tempfile.gettempdir()));base=root/'accounts/synthetic'
for path in ('config','runtime/control','runtime/skills','runtime/artifacts'): (base/path).mkdir(parents=True)
(base/'config/fixture.json').write_text('{"synthetic":true}')
(base/'runtime/skills/legacy.txt').write_text('original installed skill')
artifact=base/'runtime/artifacts/original.bin';artifact.write_bytes(b'UNCHANGED_SYNTHETIC_BINARY')
artifact_hash=hashlib.sha256(artifact.read_bytes()).hexdigest()
old_release={'source_hash':'pre-m4-synthetic','source':str(root/'releases/pre-m4-synthetic'),'images':{}}
cli.save(base/'running-release.json',old_release)
db=base/'runtime/control/carme.db';old=legacy.Store(db)
cid=old.create_conversation(['bot'])['id'];tid=old.create_task('bot','original goal',conversation_id=cid)
old.finish_task(tid,'original result');old.remember('bot','fact','original private');old.remember('__shared__','constraint','original shared')
old.add_message(tid,'bot','user','original message')
old.add_attachment({'id':'legacy-artifact','conversation_id':cid,'message_id':'','task_id':tid,'name':'original.bin','mime':'application/octet-stream','text':'','note':'','size':artifact.stat().st_size,'kind':'artifact','created_at':time.time()})
old.close()
def rows(path):
    with sqlite3.connect(path) as connection:
        tables=[r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name:[list(row) for row in connection.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in tables}
before=rows(db);backup=cli.backup_m4(base,'synthetic-instance');assert backup
assert rows(backup/'runtime/control/carme.db')==before
upgraded=Store(db);after=rows(db)
assert all(after[name]==values for name,values in before.items())
assert len(upgraded.memory_search('bot'))==2
upgraded.close();again=Store(db);assert len(again.memory_search('bot'))==2
new=again.create_task('bot','post-upgrade goal');again.finish_task(new,'post-upgrade result')
again.memory_write('bot','bot','bot','new','new fact');again.close()
assert cli.backup_m4(base,'synthetic-instance') is None
(base/'runtime/skills/new.txt').write_text('new candidate is retained on rollback')
cli.save(base/'running-release.json',{'source_hash':'M4-synthetic','images':{}})
approval=hashlib.sha256((backup/'backup.json').read_bytes()).hexdigest()
store=Store(db);operation=store.operation_begin(new,'mcp__fixture__send','unknown')
try:cli.restore_m4(base,'synthetic-instance',backup,approval);raise AssertionError('unknown external effect must block rollback')
except RuntimeError as error:assert str(error)=='rollback_external_effect_reconciliation_required'
store.operation_reconcile(new,operation['id'],effect='confirmed',receipt={'synthetic_external_id':'one'});store.close()
new_rows=rows(db)
release,archive=cli.restore_m4(base,'synthetic-instance',backup,approval)
assert release==old_release and rows(db)==before and rows(archive/'runtime/control/carme.db')==new_rows
assert (archive/'runtime/skills/new.txt').read_text()=='new candidate is retained on rollback'
reopened=legacy.Store(db);assert reopened.get_task(tid)['result']=='original result';reopened.close()
assert hashlib.sha256(artifact.read_bytes()).hexdigest()==artifact_hash
evidence={'status':'pass','layer':'synthetic SQLite WAL + pre-M4 source + real launcher backup/restore',
    'work':str(root),'backup':str(backup),'archive':str(archive),'legacy_source_sha256':hashlib.sha256(legacy_path.read_bytes()).hexdigest(),
    'preserved_tables':{name:len(values) for name,values in before.items()},'checks':['consistent pre-upgrade backup','original tables byte-value preservation',
    'one-time versioned memory import','second startup idempotent','no repeated upgrade backup','unknown effect blocks rollback',
    'new DB/config/skills archived','matching pre-M4 DB/source opens','original artifact hash unchanged']}
output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps(evidence,indent=2))
print('PASS M4 migration and rollback: '+str(len(evidence['checks']))+' checks; retained fixture '+str(root))
