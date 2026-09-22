"""Local lifecycle CLI. Each account owns one Control, Broker, database and origin.

Uses only stdlib until it launches the dedicated, hash-locked Broker environment.
No shared tenant database, personal Pi, .env import, shell evaluation or Docker fallback.
"""
from __future__ import annotations
import argparse, contextlib, fcntl, hashlib, json, os, pwd, re, secrets, shutil, signal, socket, subprocess, sys, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
ID = re.compile(r'[a-z][a-z0-9_-]{0,31}\Z')
IMAGE = re.compile(r'sha256:[a-f0-9]{64}\Z')


def save(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp=path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    with tmp.open('x') as out:
        os.chmod(tmp,mode);json.dump(data,out,ensure_ascii=False,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
    os.replace(tmp,path)


def load(path):return json.loads(path.read_text())


def directory(path):
    if any(p.casefold() in {'.pi','.ssh'} for p in path.parts) or not path.is_absolute() or path.resolve()!=path or path in (Path('/'),Path(pwd.getpwuid(os.getuid()).pw_dir),ROOT,ROOT/'app'):
        raise ValueError('需要独立、无符号链接的绝对运行目录')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    return path


def env(home):
    return {'HOME':str(home),'PATH':'/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin','LANG':'en_US.UTF-8','LC_ALL':'en_US.UTF-8','PYTHONDONTWRITEBYTECODE':'1','CARME_LOAD_ENV':'0','PYTHONUNBUFFERED':'1'}


DAEMON_HINTS = ('Cannot connect to the Docker daemon', 'Is the docker daemon running', 'error during connect',
                'docker daemon is not running', 'Cannot connect to the Docker Desktop', 'connection refused',
                'Connection refused')


def failure_message(argv, text):
    """Docker 没运行是最常见的中断原因，这里换成可执行的中文提示；其它失败保持原文。"""
    if Path(argv[0]).name.startswith('docker') and any(hint in text for hint in DAEMON_HINTS):
        return ('Docker 没有运行或尚未就绪：请先打开 Docker Desktop（等菜单栏图标显示 Running）后重试；'
                '刚开机时 Docker Desktop 通常需要 30–60 秒才能启动。Carme 的数据与配置不受影响。')
    return '命令失败：'+Path(argv[0]).name+' '+str(argv[1:3])+'（没有执行回退）'


def command(argv, *, home, timeout=60, check=True, log=None, cwd=None):
    if log:
        with log.open('a') as out:
            p=subprocess.run(argv,env=env(home),stdout=out,stderr=subprocess.STDOUT,timeout=timeout,cwd=cwd)
        if p.returncode and check:
            tail=log.read_bytes()[-4000:].decode('utf-8','ignore') if log.exists() else ''
            raise RuntimeError(failure_message(argv, tail) if any(hint in tail for hint in DAEMON_HINTS)
                               else '命令失败，请查看本实例日志：'+str(log))
        return ''
    p=subprocess.run(argv,env=env(home),capture_output=True,text=True,timeout=timeout,cwd=cwd)
    if p.returncode and check:raise RuntimeError(failure_message(argv, (p.stdout or '')+(p.stderr or '')))
    return p.stdout.strip() if not p.returncode else ''


def docker(setup, *args, **kwargs):
    return command([setup['docker'],'--config',setup['docker_config'],'--context',setup['docker_context'],*args],home=Path(setup['home'])/'client-home',**kwargs)


def inspect(setup, name):
    raw=docker(setup,'inspect',name,check=False)
    return json.loads(raw)[0] if raw else None


def owned(value, setup, account):
    labels=value['Config'].get('Labels') or {}
    return labels.get('carme.installation')==setup['id'] and labels.get('carme.instance')==account['instance_id']


def install(home, *, binary='', context=''):
    path=home/'installation.json'
    if path.exists():
        result=load(path)
        if result['home']!=str(home):raise RuntimeError('installation_home_mismatch')
        return result
    real_home=Path(pwd.getpwuid(os.getuid()).pw_dir)
    choices=[binary] if binary else [str(real_home/'.local/bin/docker'),'/opt/homebrew/bin/docker','/usr/local/bin/docker','/Applications/Docker.app/Contents/Resources/bin/docker']
    binary=next((p for p in choices if p and Path(p).is_absolute() and os.access(p,os.X_OK)),None)
    if not binary:raise RuntimeError('未找到 Docker CLI；请先安装并启动 Docker Desktop。不会使用宿主执行。')
    selected=context or command([binary,'context','show'],home=real_home)
    info=json.loads(command([binary,'context','inspect',selected],home=real_home))[0]
    endpoint=info['Endpoints']['docker']['Host']
    if not endpoint.startswith('unix:///'):raise RuntimeError('当前启动器只接受本机 Unix socket context；远程 daemon 路径尚未验收')
    config=directory(home/'docker-config');directory(home/'client-home')
    save(config/'config.json',{'cliPluginsExtraDirs':['/Applications/Docker.app/Contents/Resources/cli-plugins']})
    selected='carme-local'
    command([binary,'--config',str(config),'context','create',selected,'--docker','host='+endpoint],home=home/'client-home')
    result={'version':1,'id':'install-'+secrets.token_hex(8),'home':str(home),'docker':binary,'docker_config':str(config),'docker_context':selected,'source_context':context or info['Name']}
    docker(result,'version','--format','{{.Server.Version}}')
    save(path,result)
    return result


def source_files():
    paths=[]
    for name in ('carme','web/src','web/public','deploy/docker'):
        paths.extend(p for p in (ROOT/'app'/name).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'
                     and not any(q.startswith('.') for q in p.relative_to(ROOT/'app').parts))
    for pat in ('.dockerignore','web/package*.json','web/tsconfig*.json','web/vite.config.*','web/index.html','web/*.png','web/*.webmanifest','web/sw.js'):
        paths.extend((ROOT/'app').glob(pat))
    private_names={'auth.json','credentials.json','cookies','cookies-journal','history','history-journal','local state'}
    paths=[p for p in paths if p.name.casefold() not in private_names and not p.name.casefold().startswith(('login data','web data'))
           and not any(p.name.casefold().endswith(s) for s in ('.db','.db-wal','.db-shm','.sqlite','.sqlite3','.pem','.key'))
           and 'browser-profile' not in p.parts]
    for path in paths:
        if path.resolve() != path or not path.is_file():
            raise ValueError('source_symlink_denied')
        if any(p.startswith('.') and p != '.dockerignore' for p in path.relative_to(ROOT/'app').parts):
            raise ValueError('private_source_path_denied')
    return sorted(set(paths))


def build(setup):
    home=Path(setup['home']);files=source_files()
    hashes={str(p.relative_to(ROOT/'app')):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    identity=hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest()
    release=directory(home/'releases'/identity)
    for p in files:
        out=release/p.relative_to(ROOT/'app');out.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,out)
    manifest=release/'release.json'
    if manifest.exists():
        ready=load(manifest)
        if all(docker(setup,'image','inspect','--format','{{.Id}}',image,check=False)==image for image in ready['images'].values()):return ready
    images={}
    for role in ('control','pi','action','browser'):
        print('构建 '+role+' 固定镜像…',flush=True)
        image_file=release/(role+'.id')
        docker(setup,'build','--progress=plain','--iidfile',str(image_file),'-f',str(release/'deploy/docker'/('Dockerfile.'+role)),str(release),timeout=1800,log=release/('build-'+role+'.log'))
        images[role]=image_file.read_text().strip()
        if not IMAGE.fullmatch(images[role]):raise RuntimeError('image_digest_required')
    ready={'source_hash':identity,'source':str(release),'files':hashes,'images':images}
    save(manifest,ready)
    return ready


def broker_python(setup):
    home=Path(setup['home']);base=home/'toolchains/broker';python=base/'bin/python'
    lock=ROOT/'app/deploy/docker/requirements-control.lock';wanted=hashlib.sha256(lock.read_bytes()).hexdigest();stamp=base/'installed.json'
    if python.exists() and stamp.exists() and load(stamp).get('lock')==wanted:return str(python)
    if sys.version_info[:2]!=(3,13):raise RuntimeError('需要专用 Python 3.13 运行 Broker；请设置 CARME_BOOTSTRAP_PYTHON。不会升级全局 Python。')
    print('准备 Carme 专用 Broker 环境…',flush=True)
    if not python.exists():command([sys.executable,'-m','venv',str(base)],home=home/'client-home',timeout=120)
    command([str(python),'-m','pip','install','--disable-pip-version-check','--no-cache-dir','--require-hashes','-r',str(lock)],home=home/'client-home',timeout=900,log=home/'broker-install.log')
    save(stamp,{'lock':wanted});return str(python)


def gateway_python(setup):
    home=Path(setup['home']);base=home/'toolchains/gateway';python=base/'bin/python'
    lock=ROOT/'app/deploy/docker/requirements-gateway.lock'
    wanted=hashlib.sha256(lock.read_bytes()+(lock.parent/'requirements-control.lock').read_bytes()).hexdigest()
    stamp=base/'installed.json'
    if python.exists() and stamp.exists() and load(stamp).get('lock')==wanted:return str(python)
    if sys.version_info[:2]!=(3,13):raise RuntimeError('Gateway 需要 Python 3.13')
    if not python.exists():command([sys.executable,'-m','venv',str(base)],home=home/'client-home',timeout=120)
    command([str(python),'-m','pip','install','--disable-pip-version-check','--no-cache-dir','--only-binary=:all:','--require-hashes','-r',str(lock)],home=home/'client-home',timeout=900,log=home/'gateway-install.log')
    save(stamp,{'lock':wanted});return str(python)


def gateway_status(home):
    path=home/'gateway/process.json'
    if not path.exists():return {'gateway':'stopped'}
    state=load(path);actual=process_identity(state['pid'])
    if not actual:return {'gateway':'stopped'}
    if actual!=state['identity'] or ' -m carme.gateway ' not in actual:
        raise RuntimeError('gateway_process_identity_mismatch')
    return {'gateway':'running','origin':load(home/'gateway/config.json')['origin'],'pid':state['pid']}


def gateway_stop(home):
    state=gateway_status(home)
    if state['gateway']=='stopped':return
    os.kill(state['pid'],signal.SIGTERM)
    for _ in range(100):
        if not process_identity(state['pid']):
            (home/'gateway/process.json').unlink(missing_ok=True);return
        time.sleep(.1)
    raise RuntimeError('gateway_stop_pending')


def gateway_start(setup, *, origin='', port=None, access_team='', access_audience='', local=False, restart=False):
    home=Path(setup['home']);base=directory(home/'gateway');path=base/'config.json'
    previous=load(path) if path.exists() else {}
    if gateway_status(home)['gateway']=='running':
        if not restart:
            print('统一登录入口已运行：'+previous['origin']);return
        gateway_stop(home)
    if not previous and not origin:raise RuntimeError('首次启动需要 --origin 和 Cloudflare Access 参数；本地预览使用 --local-gateway')
    config={**previous,'home':str(home),'web_dir':str(ROOT/'app/web/dist')}
    config.update({k:v for k,v in {'origin':origin,'port':port,'access_team':access_team,'access_audience':access_audience}.items() if v})
    config.setdefault('port',8898)
    if local:config['local_only']=True
    elif origin:config['local_only']=False
    if not 1024<=config['port']<=65535 or not port_available(config['port']):raise RuntimeError('gateway_port_unavailable')
    if not (ROOT/'app/web/dist/index.html').is_file():raise RuntimeError('请先构建 app/web 前端')
    python=gateway_python(setup)
    save(path,config)
    with (base/'gateway.log').open('ab') as log:
        child=subprocess.Popen([python,'-B','-m','carme.gateway','--config',str(path)],cwd=ROOT/'app',env=env(home/'client-home'),stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    for _ in range(100):
        if child.poll() is not None:raise RuntimeError('Gateway 启动失败；查看本机 gateway/gateway.log')
        identity=process_identity(child.pid)
        if identity and ' -m carme.gateway ' in identity and not port_available(config['port']):
            save(base/'process.json',{'pid':child.pid,'identity':identity})
            print('统一登录入口已启动：'+config['origin']);return
        time.sleep(.1)
    child.terminate();child.wait(timeout=10)
    raise RuntimeError('gateway_start_timeout')


def account_list(home):
    results=[]
    for path in sorted((home/'accounts').glob('*/account.json')):
        value=load(path)
        if (path.resolve()!=path or not ID.fullmatch(path.parent.name) or value.get('id')!=path.parent.name
                or value.get('home')!=str(path.parent) or value.get('installation')!=load(home/'installation.json')['id']):
            raise RuntimeError('account_owner_mismatch')
        results.append(value)
    return results


def account(setup, account_id, release):
    if not ID.fullmatch(account_id):raise ValueError('账号 ID 仅允许小写字母开头，后接字母、数字、下划线或横线，最长 32 字符')
    home=Path(setup['home']);base=directory(home/'accounts'/account_id);path=base/'account.json'
    if path.exists():
        value=load(path)
        if value['id']!=account_id or value['home']!=str(base) or value['installation']!=setup['id']:raise RuntimeError('account_owner_mismatch')
        return value
    instance='carme-'+secrets.token_hex(10);used={a['port'] for a in account_list(home)};port=8900
    while port in used or not port_available(port):port+=1
    host='c'+hashlib.sha256(instance.encode()).hexdigest()[:16]+'.localhost'
    value={'id':account_id,'instance_id':instance,'installation':setup['id'],'home':str(base),'port':port,'origin':f'http://{host}:{port}','control_name':instance+'-control'}
    for sub in ('config','runtime/control','runtime/artifacts','runtime/skills','runtime/secrets','runtime/credentials','runtime/broker','runtime/logs'):
        directory(base/sub)
    # Only individual key files are mounted; private parents prevent other host users reading them.
    for name in ('control-token','broker-key'):
        p=base/'runtime/secrets'/name;p.write_text(secrets.token_hex(32));p.chmod(0o644)
    for sub in ('config','runtime/control','runtime/artifacts','runtime/credentials'): (base/sub).chmod(0o777)
    save(base/'config/agents.yaml',{'agents':{'assistant':{'name':account_id+' 的助手','entry':True,'engine':'api','tools':['files','exec','browser','computer'],'execution_target':'container','execution_target_id':'action'}}},0o666)
    save(base/'config/models.yaml',{'providers':{},'models':[],'tiers':{},'allow_mock':False},0o666)
    save(base/'config/browser.yaml',{'enabled':False,'desktop':{'enabled':False}},0o666)
    save(base/'config/sandbox.yaml',{'default':'none','limits':{'max_task_seconds':600,'max_tool_calls':32,'max_output_bytes':65536}},0o666)
    save(base/'config/isolation.yaml',{'version':1,'broker':{'instance_id':instance,'key_file':'/run/secrets/broker-key','max_pi':2},'targets':{'action':{'tools':['files','exec','read_attachment','create_artifact'],'image_digest':release['images']['action']}},'profiles':{},'credentials':{}},0o666)
    save(path,value);return value


def port_available(port):
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        try:s.bind(('127.0.0.1',port));return True
        except OSError:return False


def api(acc, path, method='GET', body=None):
    key=(Path(acc['home'])/'runtime/secrets/control-token').read_text().strip()
    request=urllib.request.Request(f'http://127.0.0.1:{acc["port"]}'+path,method=method,data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization':'Bearer '+key,'Host':urlsplit(acc['origin']).netloc,'Content-Type':'application/json'})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request,timeout=3) as response:return json.load(response)


def process_identity(pid):
    # ps 的 lstart 日期格式随 locale 变化；固定 C locale，否则同一个进程会因格式不同被判成身份不匹配。
    p=subprocess.run(['/bin/ps','-p',str(pid),'-o','lstart=','-o','command='],capture_output=True,text=True,
                     env={**os.environ,'LC_ALL':'C','LANG':'C'})
    return p.stdout.strip() if not p.returncode else ''


def stop_broker(acc):
    path=Path(acc['home'])/'broker-process.json'
    if not path.exists():return
    state=load(path);pid=state['pid'];actual=process_identity(pid)
    if not actual:return
    if actual!=state['identity'] or ' -m carme.broker ' not in actual or not actual.endswith(str(Path(acc['home'])/'broker.json')):
        raise RuntimeError('broker_process_identity_mismatch: 不会停止复用 PID 的其他进程')
    os.kill(pid,signal.SIGTERM)
    for _ in range(100):
        if not process_identity(pid):path.unlink(missing_ok=True);return
        time.sleep(.2)
    raise RuntimeError('broker_stop_pending: 保留状态，未强杀其他进程')


def stop(setup, acc):
    mac=Path(acc['home'])/'runtime/mac-runner'
    if mac.exists():
        directory(mac);save(mac/'stop',{'stop':True});(mac/'grant.json').unlink(missing_ok=True)
    value=inspect(setup,acc['control_name'])
    if value:
        if not owned(value,setup,acc):raise RuntimeError('control_owner_mismatch')
        docker(setup,'stop','--time','15',acc['control_name'])
    stop_broker(acc)
    # Only this account's Worker labels; no global prune, down -v or broad process kill.
    for cid in docker(setup,'ps','-aq','--filter','label=carme.instance='+acc['instance_id']).splitlines():
        item=inspect(setup,cid);labels=item['Config'].get('Labels') or {}
        if labels.get('carme.instance')==acc['instance_id'] and labels.get('carme.role') in ('pi','action','browser') and re.fullmatch(r'[a-f0-9]{32}',labels.get('carme.job','')):
            docker(setup,'rm','-f',cid)
    print('已停止账号 '+acc['id']+'；数据库、配置和成果已保留。',flush=True)


def start(setup, acc, release, python):
    current=inspect(setup,acc['control_name'])
    if current:
        if not owned(current,setup,acc):raise RuntimeError('control_owner_mismatch')
        if current['State']['Running']:
            result=api(acc,'/api/health')
            if result.get('execution',{}).get('broker')!='ready':raise RuntimeError('Control 已运行但 Broker 未就绪；使用 --restart 对本账号恢复，不启动第二个 Broker')
            print('账号 '+acc['id']+' 已运行：'+acc['origin'])
            if 'browser' not in result.get('execution',{}):
                print('此账号仍使用旧发行版；加 --restart 后启用 Docker Browser 路由。')
            return
        docker(setup,'rm',acc['control_name'])
    if not port_available(acc['port']):raise RuntimeError('账号端口已被占用；不会停止占用端口的其他服务')
    base=Path(acc['home']);stop_broker(acc)
    backup_m4(base,acc['instance_id'])
    for sub in ('config','runtime/control','runtime/artifacts','runtime/skills','runtime/secrets','runtime/credentials','runtime/broker','runtime/logs'):
        directory(base/sub)
    (base/'runtime/credentials').chmod(0o777)
    # Read only the account's own config; no parent .env or personal profile discovery.
    import_config=base/'config/isolation.yaml'
    # The dedicated Python is used to parse existing YAML without changing user edits.
    parsed=command([python,'-c','import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))',str(import_config)],home=Path(setup['home'])/'client-home')
    isolation=json.loads(parsed)
    if isolation.get('broker',{}).get('instance_id')!=acc['instance_id']:raise RuntimeError('broker_instance_mismatch')
    # One-time route upgrade, preserving explicitly configured Bot grants and targets.
    if 'browser' not in isolation:
        backup=directory(base/'config-backups'/('docker-browser-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)))
        for name in ('isolation.yaml','browser.yaml','agents.yaml'):
            shutil.copy2(base/'config'/name,backup/name);(backup/name).chmod(0o600)
        browser=json.loads(command([python,'-c','import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1])) or {}))',str(base/'config/browser.yaml')],home=Path(setup['home'])/'client-home'))
        browser.update(enabled=True,headless=True,profiles={},default_profile='default',desktop={'enabled':False})
        # Defaults apply inside the fixed worker; no host profile path is sent to it.
        save(base/'config/browser.yaml',browser,0o666)
        agents=json.loads(command([python,'-c','import json,sys,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1])) or {}))',str(base/'config/agents.yaml')],home=Path(setup['home'])/'client-home'))
        assistant=agents.get('agents',{}).get('assistant',{})
        if (assistant.get('execution_target')=='container' and assistant.get('execution_target_id')=='action'
                and assistant.get('tools')==['files','exec'] and assistant.get('entry')):
            assistant['tools'] += ['browser','computer'];save(base/'config/agents.yaml',agents,0o666)
        action=isolation.get('targets',{}).get('action')
        if action and 'tools' in action:
            action['tools']=list(dict.fromkeys([*action['tools'],'browser','computer']))
        isolation['browser']={'routing_version':1}
    isolation['browser']['image_digest']=release['images']['browser']
    target=isolation.setdefault('targets',{}).get('action')
    if target:target['image_digest']=release['images']['action']
    for profile in isolation.get('profiles',{}).values():
        if profile.get('engine')=='pi':profile['image_digest']=release['images']['pi']
    save(import_config,isolation,0o666)
    settings={'home':str(base),'instance_id':acc['instance_id'],'key_file':str(base/'runtime/secrets/broker-key'),'control_url':f'http://127.0.0.1:{acc["port"]}',
        'docker_binary':setup['docker'],'docker_config':setup['docker_config'],'docker_context':setup['docker_context'],'images':{k:release['images'][k] for k in ('pi','action','browser')},'targets':list(isolation.get('targets',{})),'max_pi':2,'max_browser':2}
    save(base/'broker.json',settings)
    args=['create','--name',acc['control_name'],'--label','carme.installation='+setup['id'],'--label','carme.instance='+acc['instance_id'],'--label','carme.role=control','--pull=never',
          '--publish',f'127.0.0.1:{acc["port"]}:8899','--read-only','--user','1000:1000','--cap-drop=ALL','--security-opt=no-new-privileges:true','--pids-limit','128','--memory','768m','--cpus','1',
          '--log-driver','local','--log-opt','max-size=10m','--log-opt','max-file=3','--tmpfs','/runtime:rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=700','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777',
          '--env','CARME_ACCOUNT_ORIGIN='+acc['origin'],'--env','CARME_PUBLIC_ORIGIN='+acc['origin'],
          '--env','CARME_CREDENTIALS_DIR=/run/credentials','--env','CARME_ENV_FILE=/config/.env']
    for src,dst,ro in [('config','/config',False),('runtime/control','/control-state',False),('runtime/artifacts','/artifacts',False),('runtime/skills','/skills',False),('runtime/credentials','/run/credentials',False)]:
        args+=['--mount',f'type=bind,src={base/src},dst={dst}'+(',readonly' if ro else '')]
    secret_names={'control-token','broker-key'}
    if isolation.get('mac_runner'):
        if isolation['mac_runner'].get('key_file')!='/run/secrets/mac-key':raise RuntimeError('mac_pairing_key_path_denied')
        secret_names.add('mac-key')
    for cred in isolation.get('credentials',{}).values():
        raw=str(cred.get('key_file',''));name=Path(raw).name
        if not re.fullmatch(r'[A-Za-z0-9_-]+',name):raise RuntimeError('credential_mount_denied')
        if raw=='/run/secrets/'+name:secret_names.add(name)
        elif raw!='/run/credentials/'+name:raise RuntimeError('credential_mount_denied')
    for name in sorted(secret_names):
        p=base/'runtime/secrets'/name
        if p.is_symlink() or not p.is_file():raise RuntimeError('dedicated_secret_missing:'+name)
        args+=['--mount',f'type=bind,src={p},dst=/run/secrets/{name},readonly']
    args+=[release['images']['control']]
    docker(setup,*args);docker(setup,'start',acc['control_name'])
    try:
        for _ in range(100):
            try:api(acc,'/api/health');break
            except (OSError,ValueError):time.sleep(.2)
        else:raise RuntimeError('control_start_timeout')
        log=base/'runtime/logs/broker.log'
        with log.open('ab') as out:
            proc=subprocess.Popen([python,'-B','-m','carme.broker',str(base/'broker.json')],cwd=release['source'],env=env(base/'runtime/broker'),stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
        time.sleep(.1);identity=process_identity(proc.pid)
        if not identity:raise RuntimeError('broker_start_failed')
        save(base/'broker-process.json',{'pid':proc.pid,'identity':identity,'release':release['source_hash']})
        for _ in range(100):
            if proc.poll() is not None:raise RuntimeError('broker_start_failed')
            if api(acc,'/api/health').get('execution',{}).get('broker')=='ready':break
            time.sleep(.2)
        else:raise RuntimeError('broker_start_timeout')
    except BaseException:
        with contextlib.suppress(Exception):stop(setup,acc)
        raise
    save(base/'running-release.json',release)
    print('账号 '+acc['id']+' 已启动：'+acc['origin'],flush=True)


def backup_m4(base, instance_id):
    """Called only after this account's Control and Broker are stopped."""
    import sqlite3
    db=base/'runtime/control/carme.db'
    if not db.exists():return None
    if db.is_symlink():raise RuntimeError('database_symlink_denied')
    snapshot_database(base, db)
    with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as source:
        tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'schema_versions' in tables and source.execute("SELECT 1 FROM schema_versions WHERE name='m4_memory'").fetchone():return None
        if source.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running','waiting_approval')").fetchone()[0]:
            raise RuntimeError('m4_upgrade_requires_interrupted_tasks_reconciled')
        destination=directory(base/'schema-backups'/('M4-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)))
        for relative in ('config','runtime/control','runtime/skills'):
            original=base/relative
            if any(p.is_symlink() for p in original.rglob('*')):raise RuntimeError('backup_symlink_denied')
            shutil.copytree(original,destination/relative,ignore=shutil.ignore_patterns('carme.db','carme.db-wal','carme.db-shm'))
        with contextlib.closing(sqlite3.connect(destination/'runtime/control/carme.db')) as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise RuntimeError('m4_backup_integrity_failed')
        old_release=load(base/'running-release.json') if (base/'running-release.json').exists() else None
        manifest={str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.rglob('*') if p.is_file()}
        save(destination/'backup.json',{'instance_id':instance_id,'files':manifest,'previous_release':old_release,
            'artifacts':'append-only CAS retained in account; new state must be archived before rollback'})
        print('M4 升级前备份：'+str(destination),flush=True)
        return destination


def snapshot_database(base, db, keep=5):
    """停容器后做一次带校验的滚动快照；库损坏时保留旧快照并跳过。"""
    import sqlite3
    target=directory(base/'db-snapshots')
    path=target/('carme-'+time.strftime('%Y%m%d-%H%M%S')+'.db')
    try:
        with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as source, \
                contextlib.closing(sqlite3.connect(str(path))) as copy:
            source.backup(copy)
            if copy.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('db_snapshot_integrity_failed')
    except (sqlite3.Error, RuntimeError, ValueError, OSError):
        path.unlink(missing_ok=True)
        return None
    files=sorted(target.glob('carme-*.db'))
    for old in files[:-keep]:
        old.unlink(missing_ok=True)
    return path


def status(setup, acc):
    item=inspect(setup,acc['control_name'])
    if item and not owned(item,setup,acc):raise RuntimeError('control_owner_mismatch')
    row={'account':acc['id'],'origin':acc['origin'],'control':'running' if item and item['State']['Running'] else 'stopped','data_home':acc['home'],
         'routes':{'web':'Docker Browser','native_mac':'Mac Runner（单独配对和授权）'}}
    if row['control']=='running':
        try:row['components']=api(acc,'/api/health').get('execution',{})
        except (OSError,ValueError):row['components']={'control':'unreachable'}
        row['routing_status']='active' if 'browser' in row['components'] else 'upgrade_required'
    return row


def restore_m4(base, instance_id, backup, approval):
    """Restore a stopped account only; archive its complete new state before replacement."""
    import sqlite3
    if backup.resolve()!=backup or backup.parent!=base/'schema-backups':raise RuntimeError('account_backup_path_denied')
    receipt=backup/'backup.json'
    if hashlib.sha256(receipt.read_bytes()).hexdigest()!=approval:raise RuntimeError('backup_manifest_approval_required')
    manifest=load(receipt)
    if manifest['instance_id']!=instance_id or not manifest.get('previous_release'):raise RuntimeError('backup_owner_or_release_missing')
    for relative,expected in manifest['files'].items():
        path=backup/relative
        if path.resolve()!=path or not path.is_relative_to(backup) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise RuntimeError('backup_hash_conflict')
    db=base/'runtime/control/carme.db'
    with contextlib.closing(sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)) as current:
        if current.execute("SELECT COUNT(*) FROM tasks WHERE status IN ('queued','running','waiting_approval')").fetchone()[0]:raise RuntimeError('rollback_active_tasks_denied')
        tables={r[0] for r in current.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'task_operations' in tables and current.execute("SELECT COUNT(*) FROM task_operations WHERE status='pending'").fetchone()[0]:
            raise RuntimeError('rollback_external_effect_reconciliation_required')
    archive=directory(base/'rollback-archives'/('M4-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)))
    save(archive/'release.json',load(base/'running-release.json'))
    for relative in ('config','runtime/control','runtime/skills'):
        source=base/relative;target=archive/relative
        if source.resolve()!=source or any(p.is_symlink() for p in source.rglob('*')):raise RuntimeError('rollback_symlink_denied')
        target.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(source,target)
    hashes={str(p.relative_to(archive)):hashlib.sha256(p.read_bytes()).hexdigest() for p in archive.rglob('*') if p.is_file()}
    save(archive/'archive.json',{'instance_id':instance_id,'files':hashes,'restored_backup':str(backup),'artifacts':'retained in account CAS'})
    for relative in ('config','runtime/control','runtime/skills'):
        original=base/relative
        # The complete pre-rollback copy is already durable and hash-recorded above.
        shutil.rmtree(original);shutil.copytree(backup/relative,original)
    save(base/'running-release.json',manifest['previous_release'])
    return manifest['previous_release'],archive


def main(argv=None):
    parser=argparse.ArgumentParser(description='每账号独立的 Carme Docker 启停。没有全局清理或宿主执行回退。')
    parser.add_argument('action',choices=['start','stop','status','build','migrate','pair-mac','rollback-m4','login-init','login-password','login-disable','gateway-start','gateway-stop','gateway-status'])
    parser.add_argument('--home',type=Path,default=ROOT/'runtime/docker',help='安装状态父目录；账号数据保存在 accounts/<ID>')
    parser.add_argument('--account',help='选择或首次建立独立账号；省略则管理全部已登记账号')
    parser.add_argument('--docker',default='');parser.add_argument('--context',default='')
    parser.add_argument('--restart',action='store_true');parser.add_argument('--no-open',action='store_true');parser.add_argument('--json',action='store_true');parser.add_argument('--public',action='store_true',help='启动后打开公网入口而不是本机账号地址')
    parser.add_argument('--stage',type=Path);parser.add_argument('--approve',default='')
    parser.add_argument('--origin',default='');parser.add_argument('--gateway-port',type=int)
    parser.add_argument('--access-team',default='');parser.add_argument('--access-audience',default='')
    parser.add_argument('--local-gateway',action='store_true',help='仅限 127.0.0.1 HTTP 本地验收，不用于公网')
    args=parser.parse_args(argv);home=args.home.absolute()
    if args.account and not ID.fullmatch(args.account):parser.error('无效的账号 ID')
    if args.action in {'status','stop'} and not (home/'installation.json').exists():
        print(json.dumps({'initialized':False,'accounts':[]}) if args.json else '未初始化 Docker 实例；旧 Carme 服务不受此脚本管理。');return 0
    directory(home)
    with (home/'lifecycle.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
        setup=install(home,binary=args.docker,context=args.context)
        if args.action.startswith('gateway-'):
            if args.action=='gateway-start':gateway_start(setup,origin=args.origin,port=args.gateway_port,access_team=args.access_team,access_audience=args.access_audience,local=args.local_gateway,restart=args.restart)
            elif args.action=='gateway-stop':gateway_stop(home)
            else:print(json.dumps(gateway_status(home),ensure_ascii=False))
            return 0
        if args.action.startswith('login-'):
            if not args.account:raise RuntimeError('需要 --account 指定已有 Docker 账号')
            python=gateway_python(setup)
            result=subprocess.run([python,'-B','-m','carme.gateway',args.action,'--home',str(home),'--account',args.account],cwd=ROOT/'app',env=env(home/'client-home'))
            return result.returncode
        # Daemon errors remain errors; never probe or run host Pi/Shell instead.
        docker(setup,'version','--format','{{.Server.Version}}')
        accounts=account_list(home)
        if args.action=='rollback-m4':
            if not args.account or not args.stage or not args.approve:raise RuntimeError('rollback_requires_account_backup_and_manifest_hash')
            acc=next((a for a in accounts if a['id']==args.account),None)
            if not acc:raise RuntimeError('account_missing')
            item=inspect(setup,acc['control_name'])
            if item and (not owned(item,setup,acc) or item['State']['Running']):raise RuntimeError('account_must_be_stopped')
            stop_broker(acc)
            release,archive=restore_m4(Path(acc['home']),acc['instance_id'],args.stage.absolute(),args.approve)
            start(setup,acc,release,broker_python(setup))
            print('已恢复备份对应发行版；M4 新数据完整保存在：'+str(archive));return 0
        if args.action in {'start','build','migrate','pair-mac'}:
            release=build(setup)
            if args.action=='build':print(json.dumps(release['images'],indent=2));return 0
            python=broker_python(setup)
            ids=[args.account] if args.account else [a['id'] for a in accounts] or ['main']
            accounts=[account(setup,name,release) for name in ids]
            if args.action in {'migrate','pair-mac'}:
                if not args.account:raise RuntimeError('explicit_account_required')
                acc=accounts[0];base=Path(acc['home']);item=inspect(setup,acc['control_name'])
                if item and (not owned(item,setup,acc) or item['State']['Running']):raise RuntimeError('account_must_be_stopped')
                if args.action=='migrate':
                    if not args.stage or not args.approve:raise RuntimeError('verified_stage_and_approval_required')
                    print(command([python,'-c','from carme.migrate import install; import sys,json; print(json.dumps(install(*sys.argv[1:])))',str(args.stage),str(base),args.approve],home=home/'client-home',cwd=release['source'],timeout=300))
                else:
                    path=base/'config/isolation.yaml'
                    isolation=json.loads(command([python,'-c','import sys,json,yaml; print(json.dumps(yaml.safe_load(open(sys.argv[1]))))',str(path)],home=home/'client-home'))
                    if isolation.get('mac_runner'):raise RuntimeError('mac_runner_already_paired')
                    runner_id='mac-'+secrets.token_hex(8);key=base/'runtime/secrets/mac-key'
                    if key.exists():raise RuntimeError('existing_mac_key_requires_manual_review')
                    key.write_text(secrets.token_hex(32));key.chmod(0o644)
                    directory(base/'runtime/mac-runner')
                    isolation['mac_runner']={'runner_id':runner_id,'key_file':'/run/secrets/mac-key'}
                    isolation['targets'][runner_id]={'tools':['mac_action']};save(path,isolation,0o666)
                    save(base/'mac-runner.json',{'runner_id':runner_id,'key_file':str(key),'state_dir':str(base/'runtime/mac-runner'),'control_url':f'http://127.0.0.1:{acc["port"]}'})
                    print('已创建独立配对。Runner 未启动、Bot 未授权；配置：'+str(base/'mac-runner.json'))
                return 0
            for acc in accounts:
                if args.restart:stop(setup,acc)
                start(setup,acc,release,python)
                print('登录凭据仅保存在：'+str(Path(acc['home'])/'runtime/secrets/control-token'))
                print('在此账号网页的“设置 → 通用”中配对；模型身份需单独配置。')
                print('本机入口：'+acc['origin']+'（直接打开 Carme，不需要 Cloudflare 登录）')
            public=''
            if (home/'gateway/config.json').exists():
                gateway_start(setup,restart=args.restart)
                public=load(home/'gateway/config.json')['origin']
                print('公网入口：'+public+'（需要 Cloudflare Access 登录；加 --public 直接打开它）')
            if not args.no_open and sys.platform=='darwin':
                subprocess.run(['/usr/bin/open',public if args.public and public else accounts[0]['origin']],check=False)
        else:
            if args.account:accounts=[a for a in accounts if a['id']==args.account]
            if args.account and not accounts:raise RuntimeError('账号不存在')
            if args.action=='stop':
                for acc in accounts:stop(setup,acc)
                if not args.account:gateway_stop(home)
            else:
                rows=[status(setup,a) for a in accounts]
                print(json.dumps(rows,ensure_ascii=False,indent=2) if args.json else '\n'.join(f"{r['account']}: {r['control']}  {r['origin']}\n  {r.get('components',{})}\n  数据：{r['data_home']}" for r in rows))
    return 0


if __name__=='__main__':
    try:raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('Carme Docker 操作未完成：'+str(exc),file=sys.stderr);raise SystemExit(1)
