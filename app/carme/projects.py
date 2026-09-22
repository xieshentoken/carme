"""Explicit local project snapshots and reviewed file proposals. Never runs Git or code."""
from __future__ import annotations
import argparse, base64, contextlib, hashlib, json, os, pwd, secrets, stat
from pathlib import Path, PurePosixPath

LIMIT = 700_000  # Fits the existing bounded Broker message after base64 encoding.
DENIED = {'.git', '.pi', '.ssh', '.aws', '.gnupg', '.config', 'node_modules', '__pycache__',
          'auth.json', 'credentials.json', 'Cookies', 'Login Data', 'Local State', 'browser-profile'}
DENIED_FOLD = {name.casefold() for name in DENIED}

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def sha(raw):return hashlib.sha256(raw).hexdigest()

def save(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+secrets.token_hex(6))
    with tmp.open('x') as f:
        os.chmod(tmp,0o600);json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def relative(name):
    p=PurePosixPath(name)
    if (not isinstance(name,str) or not name or p.is_absolute() or str(p)!=name or '\\' in name
            or any(x in {'','..','.'} or x.casefold() in DENIED_FOLD or x.casefold().startswith(('.env','.carme-'))
                   or x.casefold().endswith(('.pem','.key','.db','.sqlite','.p12')) for x in p.parts)
            or any(ord(c)<32 for c in name)):
        raise ValueError('project_path_denied')
    return p.parts

def root_path(root):
    root=Path(root)
    if (not root.is_absolute() or root.resolve()!=root or not root.is_dir()
            or root in {Path('/'),Path(pwd.getpwuid(os.getuid()).pw_dir)}
            or root.samefile(Path(pwd.getpwuid(os.getuid()).pw_dir)) or any(x.casefold() in DENIED_FOLD for x in root.parts)):
        raise ValueError('explicit_project_directory_required')
    return root

@contextlib.contextmanager
def parent_fd(root,name):
    parts=relative(name);fds=[]
    try:
        fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(fd)
        for component in parts[:-1]:
            fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);fds.append(fd)
        yield fd,parts[-1]
    finally:
        for fd in reversed(fds):os.close(fd)

def read(root,name,limit=LIMIT):
    with parent_fd(root,name) as (fd,leaf):
        try:f=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
        except FileNotFoundError:return None
        try:
            info=os.fstat(f)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>limit:raise ValueError('project_file_type_or_size_denied')
            data=bytearray()
            while chunk:=os.read(f,65536):
                data.extend(chunk)
                if len(data)>limit:raise ValueError('project_size_denied')
            return bytes(data)
        finally:os.close(f)

def work_state(root):
    # Detect edits outside the selected files too, without Git helpers, filters or hooks.
    values={};total=0
    for current,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d.casefold() not in DENIED_FOLD and not d.casefold().startswith(('.env','.carme-')))
        for name in sorted(files):
            rel=str((Path(current)/name).relative_to(root))
            try:relative(rel)
            except ValueError:continue
            p=root/rel
            if p.is_symlink():values[rel]={'link':os.readlink(p)};continue
            raw=read(root,rel,64*1024*1024);total+=len(raw or b'')
            if len(values)>=50000 or total>256*1024*1024:raise ValueError('project_inventory_limit')
            values[rel]={'sha256':sha(raw or b''),'mode':p.stat().st_mode & 0o777}
    git={};g=root/'.git'
    if g.exists():
        if g.is_symlink() or not g.is_dir():raise ValueError('shared_git_worktree_denied: export an independent snapshot')
        if (g/'index.lock').exists() or (g/'HEAD.lock').exists():raise ValueError('git_operation_in_progress')
        # Only Git revision/index metadata, never config, credentials, hooks or objects.
        for p in [g/'HEAD',g/'index',g/'packed-refs',*sorted((g/'refs').rglob('*'))]:
            if not p.exists():continue
            if p.resolve()!=p:raise ValueError('git_metadata_symlink_denied')
            if p.is_file():
                if p.stat().st_size>16*1024*1024:raise ValueError('git_metadata_limit')
                git[str(p.relative_to(g))]=sha(p.read_bytes())
    return {'worktree':digest(values),'git':digest(git)}

def validate_bundle(bundle):
    if not isinstance(bundle,dict) or set(bundle)!={'version','snapshot_id','files'} or bundle['version']!=1:raise ValueError('snapshot_fields_denied')
    files=bundle['files']
    if not isinstance(files,dict) or not 1<=len(files)<=100:raise ValueError('snapshot_file_limit')
    total=0
    for name,entry in files.items():
        relative(name)
        if set(entry)!={'before','bytes'}:raise ValueError('snapshot_entry_denied')
        raw=base64.b64decode(entry['bytes'],validate=True);total+=len(raw)
        if entry['before'] not in {None,sha(raw)} or (entry['before'] is None and raw):raise ValueError('snapshot_hash_mismatch')
    if total>LIMIT or bundle['snapshot_id']!=digest({'version':1,'files':files}):raise ValueError('snapshot_hash_or_size_denied')
    return bundle

def snapshot(root,names,output):
    root=root_path(root);before=work_state(root);files={}
    for name in names:
        raw=read(root,name)
        files[name]={'before':sha(raw) if raw is not None else None,'bytes':base64.b64encode(raw or b'').decode()}
    bundle={'version':1,'files':files};bundle['snapshot_id']=digest(bundle);validate_bundle(bundle)
    if work_state(root)!=before:raise ValueError('project_changed_during_snapshot')
    output=Path(output)
    if output.exists():raise ValueError('snapshot_destination_exists')
    output.mkdir(mode=0o700,parents=True)
    save(output/'bundle.json',bundle)
    save(output/'local.json',{'root':str(root),'state':before,'snapshot_id':bundle['snapshot_id']})
    return {'snapshot_id':bundle['snapshot_id'],'files':list(files),'bundle':str(output/'bundle.json')}

def materialize(bundle,root):
    validate_bundle(bundle)
    root=Path(root)
    # Only an empty, run-owned workspace; no extraction or links supplied by the caller.
    if list(root.iterdir()):raise ValueError('snapshot_workspace_not_empty')
    for name,entry in bundle['files'].items():
        if entry['before'] is None:continue
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('xb') as f:f.write(base64.b64decode(entry['bytes'],validate=True))
    return {'snapshot_id':bundle['snapshot_id'],'files':len(bundle['files'])}

def proposal(snapshot_dir,artifact,validation,output):
    source=Path(snapshot_dir);bundle=validate_bundle(json.loads((source/'bundle.json').read_text()))
    raw=Path(artifact).read_bytes()
    if len(raw)>LIMIT*2:raise ValueError('patch_size_limit')
    patch=json.loads(raw);checks=json.loads(Path(validation).read_text())
    if set(patch)!={'snapshot_id','files'} or patch['snapshot_id']!=bundle['snapshot_id']:raise ValueError('patch_snapshot_mismatch')
    if not patch['files'] or set(patch['files'])-set(bundle['files']):raise ValueError('patch_unselected_path')
    for name,data in patch['files'].items():
        relative(name)
        if data is not None and len(base64.b64decode(data,validate=True))>LIMIT:raise ValueError('patch_size_limit')
    if (checks.get('snapshot_id')!=bundle['snapshot_id'] or checks.get('artifact_sha256')!=sha(raw)
            or not checks.get('checks') or any(x.get('status')!='pass' or not x.get('name') for x in checks['checks'])):
        raise ValueError('validation_not_passed_or_unbound')
    result={'snapshot_id':bundle['snapshot_id'],'artifact_sha256':sha(raw),'patch':patch,'validation':checks}
    result['approval_digest']=digest(result);save(output,result)
    return {'approval_digest':result['approval_digest'],'files':list(patch['files']),'review':str(output)}

def apply(snapshot_dir,proposal_file,approval,journal):
    source=Path(snapshot_dir);local=json.loads((source/'local.json').read_text())
    bundle=validate_bundle(json.loads((source/'bundle.json').read_text()));review=json.loads(Path(proposal_file).read_text())
    if approval!=review.pop('approval_digest',None) or approval!=digest(review):raise ValueError('explicit_approval_digest_required')
    if review['snapshot_id']!=local['snapshot_id'] or review['snapshot_id']!=bundle['snapshot_id']:raise ValueError('snapshot_mismatch')
    root=root_path(local['root']);patch=review['patch']['files']
    if not patch or set(patch)-set(bundle['files']):raise ValueError('patch_unselected_path')
    if work_state(root)!=local['state']:raise ValueError('project_conflict: HEAD/index/worktree changed; create a new snapshot')
    if Path(journal).exists():raise ValueError('apply_journal_exists')
    tx=secrets.token_hex(8);record={'root':str(root),'snapshot_id':bundle['snapshot_id'],'approval_digest':approval,'state':'applying','files':[]}
    save(journal,record)
    try:
        for name,data in patch.items():
            raw=None if data is None else base64.b64decode(data,validate=True)
            expected=bundle['files'][name]['before']
            old=read(root,name)
            if (sha(old) if old is not None else None)!=expected:raise ValueError('project_conflict')
            with parent_fd(root,name) as (fd,leaf):
                backup='.carme-'+tx+'-'+leaf;temp=backup+'.new'
                row={'path':name,'backup':backup,'before':expected,'after':sha(raw) if raw is not None else None,'moved':False,'installed':False}
                record['files'].append(row);save(journal,record)
                if raw is not None:
                    f=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600,dir_fd=fd)
                    with os.fdopen(f,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
                if old is not None:
                    mode=os.stat(leaf,dir_fd=fd,follow_symlinks=False).st_mode & 0o777
                    os.rename(leaf,backup,src_dir_fd=fd,dst_dir_fd=fd);row['moved']=True;save(journal,record)
                    if sha((root/name).with_name(backup).read_bytes())!=expected:raise ValueError('project_conflict_during_apply')
                    if raw is not None:os.chmod(temp,mode,dir_fd=fd)
                if raw is not None:
                    # Atomic exclusive creation never overwrites a concurrently recreated file.
                    os.link(temp,leaf,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                    os.unlink(temp,dir_fd=fd);row['installed']=True
                os.fsync(fd);save(journal,record)
        record['state']='applied';save(journal,record)
    except BaseException:
        record['state']='needs_recovery';save(journal,record);raise
    return record

def rollback(journal):
    record=json.loads(Path(journal).read_text());root=root_path(record['root'])
    if record['state']=='rolled_back':return record
    pending=[]
    # Preflight every file before any restoration; preserve new user edits on conflict.
    for row in record['files']:
        relative(row['path'])
        if '/' in row['backup'] or not row['backup'].startswith('.carme-'):raise ValueError('journal_path_denied')
        current=read(root,row['path']);actual=sha(current) if current is not None else None
        backup=(root/row['path']).with_name(row['backup'])
        if not backup.exists() and actual==row['before']:continue  # Not yet changed at interruption.
        if actual not in {row['after'],None} or (row['installed'] and actual!=row['after']):raise ValueError('rollback_conflict')
        pending.append(row)
    for row in reversed(pending):
        with parent_fd(root,row['path']) as (fd,leaf):
            backup=row['backup']
            if (root/row['path']).exists():
                if (root/row['path']).with_name(backup+'.result').exists():raise ValueError('rollback_result_exists')
                os.rename(leaf,backup+'.result',src_dir_fd=fd,dst_dir_fd=fd)
                if sha((root/row['path']).with_name(backup+'.result').read_bytes())!=row['after']:
                    os.link(backup+'.result',leaf,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                    os.unlink(backup+'.result',dir_fd=fd)
                    raise ValueError('rollback_conflict_during_restore')
            # Also detect the crash window between rename and journal fsync.
            if (root/row['path']).with_name(backup).exists():
                os.link(backup,leaf,src_dir_fd=fd,dst_dir_fd=fd,follow_symlinks=False)
                os.unlink(backup,dir_fd=fd)
            os.fsync(fd)
    record['state']='rolled_back';save(journal,record);return record

def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='op',required=True)
    s=sub.add_parser('snapshot');s.add_argument('--project',required=True);s.add_argument('--file',action='append',required=True);s.add_argument('--output',required=True)
    s=sub.add_parser('proposal');s.add_argument('--snapshot',required=True);s.add_argument('--artifact',required=True);s.add_argument('--validation',required=True);s.add_argument('--output',required=True)
    s=sub.add_parser('apply');s.add_argument('--snapshot',required=True);s.add_argument('--proposal',required=True);s.add_argument('--approve',required=True);s.add_argument('--journal',required=True)
    s=sub.add_parser('rollback');s.add_argument('--journal',required=True)
    a=parser.parse_args()
    if a.op=='snapshot':r=snapshot(a.project,a.file,a.output)
    elif a.op=='proposal':r=proposal(a.snapshot,a.artifact,a.validation,a.output)
    elif a.op=='apply':r=apply(a.snapshot,a.proposal,a.approve,a.journal)
    else:r=rollback(a.journal)
    print(json.dumps(r,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
