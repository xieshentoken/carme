"""Read-only inventory, WAL-aware backups and reversible offline migration staging."""
from __future__ import annotations
import argparse, contextlib, json, os, re, shutil, sqlite3, stat, time
from pathlib import Path
from urllib.parse import quote
import yaml
from .projects import digest, sha, save, root_path

PATH_KEYS={'CARME_CONFIG_DIR','CARME_DATA_DIR','CARME_ARTIFACTS_DIR','CARME_SKILLS_DIR','CARME_ENV_FILE'}

def discover(app,overrides=None):
    app=root_path(app);overrides=overrides or {};hints={}
    # Parse path hints only. Never source/evaluate .env, export tokens or import credentials.
    for file in (app/'.env',app/'.local/active/.env'):
        if file.is_file() and not file.is_symlink():
            for line in file.read_text().splitlines():
                m=re.fullmatch(r'\s*(?:export\s+)?(CARME_[A-Z_]+)\s*=\s*(.*?)\s*',line)
                if m and m[1] in PATH_KEYS:hints[m[1]]=m[2].strip('"\'')
    # config.py reads directory variables before dotenv. Hints are ambiguous until confirmed.
    paths={'config':str(app/'config'),'data':str(app/'data')}
    for key,name in [('CARME_CONFIG_DIR','config'),('CARME_DATA_DIR','data')]:
        if key in overrides:paths[name]=str(Path(overrides[key]))
    paths['artifacts']=overrides.get('CARME_ARTIFACTS_DIR',str(Path(paths['data'])/'artifacts'))
    paths['skills']=overrides.get('CARME_SKILLS_DIR',str(Path(paths['data'])/'skills'))
    return paths,hints

def inventory(directory):
    directory=Path(directory);result={}
    if not directory.exists():return result
    if directory.resolve()!=directory:raise ValueError('migration_symlink_root_denied')
    for current,dirs,files in os.walk(directory,followlinks=False):
        for name in dirs+files:
            p=Path(current)/name
            if p.is_symlink():raise ValueError('migration_symlink_denied')
        for name in sorted(files):
            p=Path(current)/name
            if not p.is_file() or p.stat().st_nlink!=1:raise ValueError('migration_special_file_denied')
            if p.stat().st_size>256*1024*1024:raise ValueError('migration_file_too_large')
            result[str(p.relative_to(directory))]={'size':p.stat().st_size,'sha256':sha(p.read_bytes())}
            if len(result)>50000:raise ValueError('migration_file_count_limit')
    return result

def copy_inventory(source,destination,manifest):
    """Copy only inventoried regular files; never follow a link introduced during migration."""
    source=Path(source);destination=Path(destination);destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    for name,expected in manifest.items():
        parts=Path(name).parts
        if Path(name).is_absolute() or '..' in parts:raise ValueError('migration_manifest_path_denied')
        descriptors=[]
        try:
            fd=os.open(source,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(fd)
            for component in parts[:-1]:
                fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);descriptors.append(fd)
            f=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd);descriptors.append(f);info=os.fstat(f)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size!=expected['size']:raise ValueError('migration_file_changed')
            raw=bytearray()
            while chunk:=os.read(f,65536):
                raw.extend(chunk)
                if len(raw)>expected['size']:raise ValueError('migration_file_changed')
            if sha(raw)!=expected['sha256']:raise ValueError('migration_file_changed')
            dst=destination/name;dst.parent.mkdir(parents=True,exist_ok=True)
            with dst.open('xb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
            dst.chmod(0o600)
        finally:
            for fd in reversed(descriptors):os.close(fd)

@contextlib.contextmanager
def connect_ro(path):
    connection=sqlite3.connect('file:'+quote(str(path),safe='/')+'?mode=ro',uri=True,timeout=5)
    try:
        connection.execute('BEGIN')
        yield connection
    finally:connection.close()

def database_manifest(db):
    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('sqlite_integrity_failed')
    result={}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        if not re.fullmatch('[A-Za-z0-9_]+',table):raise ValueError('sqlite_table_name_denied')
        rows=db.execute('SELECT * FROM "'+table+'"').fetchall()
        # No contents are printed or included in reports; IDs/ACL/relations contribute to the hash.
        normalized=sorted(json.dumps(r,default=lambda b:{'bytes':bytes(b).hex()},separators=(',',':')) for r in rows)
        result[table]={'rows':len(rows),'sha256':digest(normalized)}
    return result

def backup(source,destination):
    source=Path(source);destination=Path(destination)
    if source.resolve()!=source or not source.is_file() or destination.exists():raise ValueError('backup_path_denied')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    with connect_ro(source) as src,contextlib.closing(sqlite3.connect(destination)) as dst:
        src.backup(dst,pages=64,sleep=.01)
        result=database_manifest(dst)
    destination.chmod(0o600)
    return result

def plan(app,output,overrides=None,allow_roots=()):
    app=root_path(app);paths,hints=discover(app,overrides)
    allowed=[app,*map(root_path,allow_roots)];blocked=[];manifests={}
    for label,value in paths.items():
        p=Path(value)
        if not p.is_absolute() or p.resolve()!=p or any(x in {'.pi','.ssh'} for x in p.parts):raise ValueError('migration_path_denied')
        if not any(p==a or p.is_relative_to(a) for a in allowed):blocked.append('explicit_source_grant_required:'+label);continue
        if label!='data':manifests[label]=inventory(p)
    if hints and any(k not in (overrides or {}) for k in hints if k in {'CARME_CONFIG_DIR','CARME_DATA_DIR'}):
        blocked.append('active_env_path_hints_require_explicit_confirmation')
    data=Path(paths['data']);db=data/'carme.db'
    if not any(data==a or data.is_relative_to(a) for a in allowed):blocked.append('database_scope_denied')
    elif db.is_file():
        with connect_ro(db) as conn:tables=database_manifest(conn)
    else:blocked.append('source_database_missing_confirm_active_paths')
    for label in ('attachments','avatars','workspaces','snapshots','screenshots'):
        paths[label]=str(data/label)
        if 'database_scope_denied' not in blocked:manifests[label]=inventory(data/label)
    # Browser is metadata-only. Linux must use a new dedicated login, never Keychain/Chrome copies.
    paths['browser']=str(data/'browser')
    recognized={'carme.db','carme.db-wal','carme.db-shm','attachments','avatars','workspaces','snapshots','screenshots','browser','skills','artifacts','admission-paused.json','.DS_Store'}
    extra=sorted(p.name for p in data.iterdir() if p.name not in recognized) if data.is_dir() and 'database_scope_denied' not in blocked else []
    if extra:blocked.append('additional_data_entries_require_mapping')
    external=[]
    for name in ('sandbox','browser','skills'):
        file=Path(paths['config'])/(name+'.yaml')
        if 'config' not in manifests or not file.is_file():continue
        config=yaml.safe_load(file.read_text()) or {}
        def visit(value,key='',trail=()):
            if isinstance(value,dict):
                for k,v in value.items():visit(v,str(k),(*trail,str(k)))
            elif isinstance(value,list):
                for v in value:visit(v,key,trail)
            elif isinstance(value,str) and (key in {'root','roots','path','profile_dir','user_data_dir','workdir'} or (key=='dir' and 'screenshots' in trail)):
                namespace='ssh' if name=='sandbox' and ('remote' in trail or 'nodes' in trail) else 'container' if name=='sandbox' and 'docker' in trail else 'host'
                external.append({'config':name,'field':'.'.join(trail),'path':value,'namespace':namespace})
        visit(config)
    for item in external:
        if item['namespace']!='host':continue  # Describe these paths; never expand/read them on this Mac.
        p=Path(item['path'])
        if not p.is_absolute():p=app/p
        if p.resolve()!=p or not any(p==Path(x) or p.is_relative_to(Path(x)) for x in paths.values()):
            blocked.append('additional_configured_path_requires_mapping:'+item['config']+':'+item['field'])
    result={'version':1,'source_app':str(app),'paths':paths,'path_hints':hints,'configured_paths':external,
            'files':manifests,'tables':locals().get('tables',{}),'blocked':sorted(set(blocked)),
            'unmapped_data_entries':extra,'browser':'not_copied_relogin_required','credentials':'not_imported','source_mutation':False}
    result['approval_digest']=digest(result);save(output,result);return result

def stage(plan_file,approval,destination,*,quiesced=False):
    p=json.loads(Path(plan_file).read_text());expected=p.pop('approval_digest')
    if expected!=approval or digest(p)!=approval:raise ValueError('migration_plan_approval_required')
    if p['blocked']:raise ValueError('migration_plan_blocked')
    if not quiesced:raise ValueError('pause_admission_drain_and_confirm_source_quiesced')
    out=Path(destination)
    if out.exists() or out.resolve()!=out or not out.is_absolute():raise ValueError('fresh_stage_required')
    if any(out==Path(v) or out.is_relative_to(Path(v)) or Path(v).is_relative_to(out) for v in p['paths'].values()):raise ValueError('migration_source_destination_overlap')
    out.mkdir(mode=0o700,parents=True);journal={'state':'copying','plan':approval,'source':p['source_app']};save(out/'migration.json',journal)
    mapping={'config':'config-original','attachments':'runtime/control/attachments','avatars':'runtime/control/avatars',
             'artifacts':'runtime/artifacts','skills':'runtime/skills','workspaces':'archive/workspaces','snapshots':'runtime/control/snapshots','screenshots':'runtime/control/screenshots'}
    try:
        for label,target in mapping.items():
            source=Path(p['paths'][label]);before=inventory(source)
            if before!=p['files'][label]:raise ValueError('migration_source_changed:'+label)
            copy_inventory(source,out/target,before)
            if inventory(out/target)!=before or inventory(source)!=before:raise ValueError('migration_copy_changed:'+label)
        tables=backup(Path(p['paths']['data'])/'carme.db',out/'runtime/control/carme.db')
        if tables!=p['tables']:raise ValueError('migration_database_changed_since_plan')
        with connect_ro(out/'runtime/control/carme.db') as db:
            if db.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','running','waiting_approval')").fetchone()[0]:
                raise ValueError('active_tasks_require_reconciliation')
        shutil.copytree(out/'runtime/control',out/'archive/control-before-schema')
        from .store import Store
        store=Store(out/'runtime/control/carme.db')
        store._write('UPDATE browser_sessions SET revoked=1')
        # No pending approval or streaming task is automatically replayed after migration.
        store._write("UPDATE approvals SET status='rejected',note='migration_requires_new_approval' WHERE status='pending'")
        store.close()
        # Seal the inactive destination as one file; first Control start re-enables WAL.
        with contextlib.closing(sqlite3.connect(out/'runtime/control/carme.db')) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)');db.execute('PRAGMA journal_mode=DELETE')
        save(out/'runtime/control/admission-paused.json',{'paused':True,'reason':'migration_candidate_requires_review'})
        with connect_ro(out/'runtime/control/carme.db') as db:after=database_manifest(db)
        with connect_ro(out/'archive/control-before-schema/carme.db') as old,connect_ro(out/'runtime/control/carme.db') as new:
            for table in tables:
                columns=[r[1] for r in old.execute('PRAGMA table_info("'+table+'")')]
                excluded={'browser_sessions':{'revoked'},'approvals':{'status','note'},'conversation_messages':{'status'}}.get(table,set())
                columns=[c for c in columns if c not in excluded]
                select='SELECT '+','.join('"'+c+'"' for c in columns)+' FROM "'+table+'"'
                if sorted(map(repr,old.execute(select)))!=sorted(map(repr,new.execute(select))):raise ValueError('historical_content_or_acl_changed:'+table)
        changes={k for k in tables if after.get(k)!=tables[k]}
        allowed={'browser_sessions','approvals','conversation_messages','attachments','tasks','usage_log','conversations'}
        if changes-allowed:raise ValueError('unexpected_schema_or_data_change')
        # Preserve historical IDs and content; remove inherited host/model/plugin authority.
        config=out/'config';config.mkdir()
        agents=yaml.safe_load((out/'config-original/agents.yaml').read_text()) or {}
        for agent in agents.get('agents',{}).values():
            agent.update(engine='api',execution_target='none',execution_target_id='',runtime_profile='',engine_workspace='',tools=[],can_delegate=False)
        save(config/'agents.yaml',agents)
        save(config/'models.yaml',{'providers':{},'models':{},'tiers':{},'allow_mock':False})
        save(config/'sandbox.yaml',{'default':'none'})
        save(config/'browser.yaml',{'enabled':False,'desktop':{'enabled':False}})
        skills=yaml.safe_load((out/'config-original/skills.yaml').read_text()) if (out/'config-original/skills.yaml').exists() else {}
        skills=skills or {};skills['grants']={};skills['roots']=[];skills['disabled']=list((skills.get('installed') or {}).keys());save(config/'skills.yaml',skills)
        save(config/'mcp.yaml',{'servers':{}})
        # Verify the exact paths used by both legacy attachments and M2 CAS.
        with connect_ro(out/'runtime/control/carme.db') as db:
            for fid,blob_hash,size in db.execute('SELECT id,sha256,size FROM attachments'):
                if not re.fullmatch(r'f_[a-f0-9]{32}',fid):raise ValueError('attachment_id_denied')
                file=out/'runtime/artifacts'/blob_hash if blob_hash else out/'runtime/control/attachments'/fid
                if blob_hash and not re.fullmatch(r'[a-f0-9]{64}',blob_hash):raise ValueError('artifact_hash_denied')
                if not file.is_file() or file.stat().st_size!=size or (blob_hash and sha(file.read_bytes())!=blob_hash):raise ValueError('attachment_integrity_failed')
        # Repeat source checks at the end to catch writes across directory copies.
        for label in mapping:
            if inventory(p['paths'][label])!=p['files'][label]:raise ValueError('migration_source_changed_during_stage')
        with connect_ro(Path(p['paths']['data'])/'carme.db') as db:
            if database_manifest(db)!=tables:raise ValueError('migration_database_changed_during_stage')
        journal.update(state='verified',tables_before=tables,tables_after=after,browser='relogin_required',
                       replay=False,changed_tables=sorted(changes),files=inventory(out/'runtime'),config_files=inventory(config))
        journal['approval_digest']=digest(journal);save(out/'migration.json',journal)
        return journal
    except BaseException:
        journal['state']='incomplete_preserved';save(out/'migration.json',journal);raise

def install(stage_dir,account_dir,approval):
    stage_dir=Path(stage_dir);account_dir=root_path(account_dir)
    receipt=json.loads((stage_dir/'migration.json').read_text());expected=receipt.pop('approval_digest',None)
    if receipt['state']!='verified' or approval!=expected or digest(receipt)!=approval:raise ValueError('verified_stage_approval_required')
    if inventory(stage_dir/'runtime')!=receipt['files'] or inventory(stage_dir/'config')!=receipt['config_files']:raise ValueError('stage_changed')
    if (account_dir/'runtime/control/carme.db').exists():raise ValueError('new_empty_account_required')
    if (account_dir/'migration-install.json').exists():raise ValueError('migration_already_attempted_use_new_account')
    journal={'state':'installing','stage':str(stage_dir),'approval_digest':approval,'original_unchanged':True}
    save(account_dir/'migration-install.json',journal)
    for rel in ('runtime/control','runtime/artifacts','runtime/skills','config'):
        target=account_dir/rel
        if target.resolve()!=target:raise ValueError('account_symlink_denied')
        if rel!='config' and target.exists() and list(target.iterdir()):raise ValueError('empty_destination_required')
        source=stage_dir/rel
        target.mkdir(parents=True,exist_ok=True)
        for p in source.rglob('*'):
            dst=target/p.relative_to(source)
            if dst.resolve()!=dst or dst.is_symlink() or (dst.is_file() and dst.stat().st_nlink!=1):raise ValueError('account_destination_link_denied')
            if p.is_dir():dst.mkdir(exist_ok=True)
            else:shutil.copy2(p,dst);dst.chmod(0o666)
        for d,_,_ in os.walk(target):Path(d).chmod(0o777)
    # Preserve the original account's dedicated isolation.yaml and secrets, not legacy identity.
    journal['state']='installed';save(account_dir/'migration-install.json',journal);return journal

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='op',required=True)
    s=sub.add_parser('plan');s.add_argument('--app',required=True);s.add_argument('--output',required=True);s.add_argument('--allow-root',action='append',default=[])
    s.add_argument('--config');s.add_argument('--data');s.add_argument('--artifacts');s.add_argument('--skills')
    s=sub.add_parser('stage');s.add_argument('--plan',required=True);s.add_argument('--approve',required=True);s.add_argument('--output',required=True);s.add_argument('--source-quiesced',action='store_true')
    s=sub.add_parser('backup');s.add_argument('--database',required=True);s.add_argument('--output',required=True)
    a=parser.parse_args()
    if a.op=='plan':r=plan(a.app,a.output,{key:getattr(a,name) for key,name in [('CARME_CONFIG_DIR','config'),('CARME_DATA_DIR','data'),('CARME_ARTIFACTS_DIR','artifacts'),('CARME_SKILLS_DIR','skills')] if getattr(a,name)},a.allow_root)
    elif a.op=='stage':r=stage(a.plan,a.approve,a.output,quiesced=a.source_quiesced)
    else:r=backup(a.database,a.output)
    print(json.dumps(r,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
