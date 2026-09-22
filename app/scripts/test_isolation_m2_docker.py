"""Real Docker + fixed Pi + Control/Broker integration with a synthetic TLS model.

Requires prebuilt image IDs and an isolated Docker CLI config. Never uses personal
credentials, the live Carme database, global cleanup, or a model provider account.
"""
from __future__ import annotations
import asyncio, hashlib, io, json, os, secrets, shutil, signal, socket, subprocess, sys, tempfile, time, zipfile
from pathlib import Path
import httpx, yaml

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
PARAMS = json.loads(Path(sys.argv[1]).read_text())
WORK = Path(PARAMS['work'])
EVIDENCE = Path(PARAMS['evidence'])
EVIDENCE.mkdir(parents=True, exist_ok=True)
RUN = Path(tempfile.mkdtemp(prefix='docker-e2e-', dir=WORK))
INSTANCE = 'm2-' + secrets.token_hex(4)
DOCKER = [PARAMS['docker'], '--config', PARAMS['docker_config'], '--context', PARAMS['docker_context']]
ENV = {'HOME': str(RUN/'client-home'), 'PATH':'/usr/local/bin:/usr/bin:/bin', 'DOCKER_CONFIG': PARAMS['docker_config']}
Path(ENV['HOME']).mkdir()
NAMES, NETWORKS, RESULTS, INSPECTED = [], [], [], {}
BROKER = None
PERSONAL_PROCESS = None
CONTROL_LAUNCH = []
CONTROL_NETWORK = ''


def docker(*args, check=True, text=True):
    result = subprocess.run([*DOCKER, *args], env=ENV, capture_output=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError('Docker '+args[0]+': '+result.stderr.decode(errors='replace')[:500])
    raw=result.stdout + (result.stderr if args[0]=='logs' else b'')
    return raw.decode().strip() if text else raw


def record(name, details=None):
    RESULTS.append({'id':name, 'status':'pass', 'layer':'real Docker / synthetic model', 'details':details})
    (EVIDENCE/'docker-results.json').write_text(json.dumps({'instance':INSTANCE,'run':str(RUN),'tests':RESULTS},indent=2))
    print('PASS', name, flush=True)


def inspect_workers():
    ids=docker('ps','-q','--filter','label=carme.instance='+INSTANCE)
    for cid in ids.splitlines():
        if any(value['Id']==cid or value['Id'].startswith(cid) for value in INSPECTED.values()):
            continue
        value=json.loads(docker('inspect',cid))[0]
        if value['Config']['Labels'].get('carme.role') not in {'pi','action'}: continue
        key=value['Name'].lstrip('/')
        INSPECTED[key]={k:value[k] for k in ('Id','Name','Config','HostConfig','Mounts','NetworkSettings','State')}
        config,host=value['Config'],value['HostConfig']
        assert config['User']=='1000:1000' and host['ReadonlyRootfs'] and not host['Privileged']
        assert host['CapDrop']==['ALL'] and 'no-new-privileges:true' in host['SecurityOpt']
        assert host['NetworkMode']=='none' and host['PidsLimit']==64 and host['NanoCpus']==1000000000
        assert host['Memory']==768*1024*1024 and host['MemorySwap']==host['Memory']
        assert not host.get('Devices') and host.get('PidMode','') != 'host'
        mounts=value['Mounts'];role=config['Labels']['carme.role']
        assert len(mounts)==(0 if role=='pi' else 3)
        for mount in mounts:
            assert Path(mount['Source']).is_relative_to(RUN/'实例 Carme'/'runtime'/'runs')
            assert mount['Destination'] in ('/workspace','/out','/inputs')
            assert mount['RW'] == (mount['Destination']!='/inputs')
    return [v for v in INSPECTED.values() if v['State']['Running']]


MODEL_SERVER = r'''
import json, ssl, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
CASES=json.loads(Path('/fixture/cases.json').read_text())
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  with Path('/capture/requests.jsonl').open('a') as f: f.write(json.dumps(body)+'\n')
  content=json.dumps(body['messages'])
  case=next((key for key in CASES if 'M2_CASE:'+key in content),'plain')
  if case in ('authfailure','redirect'):
   self.send_response(401 if case=='authfailure' else 302);self.send_header('Location','https://169.254.169.254/blocked');self.send_header('Content-Length','0');self.end_headers();return
  tools=body.get('tools',[]);pi=any(t['function']['name'].startswith('carme_') for t in tools)
  count=sum(m.get('role')=='tool' for m in body['messages'])
  actions=CASES.get(case,[])
  if count < len(actions):
   name,args=actions[count]
   name=('carme_'+name if pi and name != 'bash' else name)
   delta={'tool_calls':[{'index':0,'id':'call_'+str(count),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}
   finish='tool_calls'
  else:
   delta={'content':'M2_FIXTURE_DONE '+case};finish='stop'
  chunks=[{'id':'fixture','object':'chat.completion.chunk','created':1,'model':body['model'],'choices':[{'index':0,'delta':{'role':'assistant'},'finish_reason':None}]},
   {'id':'fixture','object':'chat.completion.chunk','created':1,'model':body['model'],'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
   {'id':'fixture','object':'chat.completion.chunk','created':1,'model':body['model'],'choices':[{'index':0,'delta':{},'finish_reason':finish}], 'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}]
  data=(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('0.0.0.0',443),Handler)
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/fixture/cert.pem','/fixture/key.pem')
server.socket=ctx.wrap_socket(server.socket,server_side=True);server.serve_forever()
'''


def setup():
    global PERSONAL_PROCESS, CONTROL_LAUNCH, CONTROL_NETWORK
    home=RUN/'实例 Carme'
    for sub in ('config','runtime/control','runtime/artifacts','runtime/skills','runtime/secrets','runtime/broker'):
        path=home/sub;path.mkdir(parents=True,exist_ok=True);path.chmod(0o777)
    fixture=RUN/'fixture';fixture.mkdir();capture=RUN/'capture';capture.mkdir();capture.chmod(0o777)
    (fixture/'model.py').write_text(MODEL_SERVER)
    cert=subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(fixture/'key.pem'),'-out',str(fixture/'cert.pem'),'-days','2','-subj','/CN=fixture-model','-addext','subjectAltName=DNS:fixture-model'],capture_output=True)
    if cert.returncode: raise RuntimeError('fixture certificate generation failed')
    (fixture/'key.pem').chmod(0o644)
    makezip="from zipfile import ZipFile\nwith ZipFile('/out/result.zip','w') as z: z.writestr('hello.txt','容器二进制成果')\n"
    isolation=r'''import json,os,socket
from pathlib import Path
paths=['/control-state/carme.db','/run/secrets/control-token','/run/secrets/provider-key','/var/run/docker.sock','/Users/you/.pi','/root/.pi','/other-bot/secret','/app/config','/runtime/home/pi/auth.json']
def visible(p):
 try:return Path(p).exists()
 except OSError:return False
r={'uid':os.getuid(),'exists':{p:visible(p) for p in paths},'env':sorted(os.environ),'writes':{},'connections':{}}
for p in ['/inputs/deny','/app/deny','/workspace/allowed','/out/allowed']:
 try: Path(p).write_text('fixture');r['writes'][p]=True
 except OSError:r['writes'][p]=False
for host in ['1.1.1.1','169.254.169.254','172.17.0.1','127.0.0.1','host.docker.internal','::1','2606:4700:4700::1111']:
 try:
  s=socket.create_connection((host,8899 if host=='127.0.0.1' else 80),timeout=.3);s.close();r['connections'][host]=True
 except OSError:r['connections'][host]=False
Path('/out/isolation.json').write_text(json.dumps(r))
'''
    cases={'authfailure':[],'redirect':[],
      'seed':[['write_file',{'path':'private.txt','content':'RUN_PRIVATE_CANARY_M2'}]],
      'foreign':[['shell',{'command':"test ! -e /workspace/private.txt && test ! -e /out/result.zip && echo RUN_SCOPE_ISOLATED",'timeout':5}]],
      'binary':[['write_file',{'path':'make.py','content':makezip}],['shell',{'command':'python /workspace/make.py','timeout':15}],['create_artifact',{'name':'result.zip','path':'/out/result.zip'}]],
      'isolation':[['write_file',{'path':'isolation.py','content':isolation}],['shell',{'command':'python /workspace/isolation.py','timeout':15}],['create_artifact',{'name':'isolation.json','path':'/out/isolation.json'}]],
      'cancel':[['shell',{'command':'sleep 110 & sleep 110 & wait','timeout':120}]],
      'output':[['shell',{'command':"python -c 'print(\"x\"*200000)'",'timeout':5}]],
      'timeout':[['shell',{'command':'sleep 50 & wait','timeout':2}]],
      'memory':[['shell',{'command':"python -c 'a=bytearray(1024**3)'",'timeout':15}]],
      'cpu':[['shell',{'command':"python - <<'PYCODE'\nfrom pathlib import Path\nimport subprocess\ndef throttles():return int(dict(line.split() for line in Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())['nr_throttled'])\nbefore=throttles()\na=[subprocess.Popen(['python','-c','import time; end=time.monotonic()+3\\nwhile time.monotonic()<end: pass']) for _ in range(3)]\nfor p in a:p.wait()\nassert throttles()>before\nprint('CPU_QUOTA_EFFECTIVE')\nPYCODE",'timeout':15}]],
      'pids':[['shell',{'command':"python - <<'PYCODE'\nimport subprocess\na=[]\ntry:\n for i in range(90):a.append(subprocess.Popen(['sleep','5']))\nexcept OSError:print('PIDS_LIMIT_EFFECTIVE',len(a))\nfinally:\n for p in a:p.terminate()\n for p in a:p.wait()\nPYCODE",'timeout':15}]],
      'readonly':[['write_file',{'path':'unauthorized','content':'DENIED'}],['bash',{'command':'touch /workspace/native-escape'}]],
    }
    if PARAMS.get('m3_project'):
        import base64
        from carme import projects
        original=RUN/'selected-project';original.mkdir();(original/'main.txt').write_text('before')
        (original/'other.txt').write_text('human original');(original/'.pi').mkdir();(original/'.pi/auth.json').write_text('SYNTHETIC_PRIVATE')
        info=projects.snapshot(original,['main.txt'],RUN/'snapshot')
        patch={'snapshot_id':info['snapshot_id'],'files':{'main.txt':base64.b64encode(b'after').decode()}}
        cases['project']=[['read_file',{'path':'main.txt'}],['write_file',{'path':'main.txt','content':'after'}],
            ['shell',{'command':"test \"$(cat /workspace/main.txt)\" = after && test ! -e /workspace/.pi && echo VALIDATED_SNAPSHOT",'timeout':5}],
            ['write_file',{'path':'/out/patch.json','content':json.dumps(patch)}],['create_artifact',{'name':'patch.json','path':'/out/patch.json'}]]
    (fixture/'cases.json').write_text(json.dumps(cases,ensure_ascii=False))
    if PARAMS.get('docker_browser'):
        cases['browser'] = [['web_open', {'url': 'https://example.com'}],
            ['web_screenshot', {'name': 'docker-browser'}],
            ['fetch_page', {'url': 'http://169.254.169.254'}], ['web_close', {}]]
        (fixture/'cases.json').write_text(json.dumps(cases, ensure_ascii=False))
    network=INSTANCE+'-model';docker('network','create','--internal','--label','carme.fixture='+INSTANCE,network);NETWORKS.append(network)
    model=INSTANCE+'-model';NAMES.append(model)
    docker('run','-d','--name',model,'--label','carme.fixture='+INSTANCE,'--network',network,'--network-alias','fixture-model',
        '--user','1000:1000','--read-only','--cap-drop=ALL','--security-opt=no-new-privileges:true','--tmpfs','/tmp',
        '--mount',f'type=bind,src={fixture},dst=/fixture,readonly','--mount',f'type=bind,src={capture},dst=/capture',
        '--entrypoint','python',PARAMS['images']['control'],'/fixture/model.py')
    ip=json.loads(docker('inspect',model))[0]['NetworkSettings']['Networks'][network]['IPAddress']
    admin=secrets.token_urlsafe(32);broker_key=secrets.token_hex(32);provider_key=secrets.token_hex(32)
    for filename,value in [('control-token',admin),('broker-key',broker_key),('provider-key',provider_key)]:
        (home/'runtime/secrets'/filename).write_text(value);(home/'runtime/secrets'/filename).chmod(0o644)
    shutil.copy2(fixture/'cert.pem',home/'config/model-ca.pem')
    agents={'defaults':{'max_steps':12},'agents':{}}
    for name,engine,tools in [('pi','pi',['files','exec']),('api','api',['files','exec']),('readonly','pi',['read_file']),('peer','pi',['files','exec'])]:
        agents['agents'][name]={'name':name,'entry':name=='pi','engine':engine,'runtime_profile':'dedicated',
            'engine_model':'fixture/model','model':'fixture/model','tools':tools,'execution_target':'container','execution_target_id':'action','prompt':'Follow explicit request.'}
    profile={'engine':'pi','package_name':'@earendil-works/pi-coding-agent','package_version':'0.85.1',
        'image_digest':PARAMS['images']['pi'],'execution_mode':'managed_bridge','inherit_user_config':False,'native_tools':[],
        'credential_ref':'dedicated','credential_kind':'api_key','model':'fixture/model','session_authority':'carme','resume_personal_sessions':False}
    isolation={'version':1,'broker':{'instance_id':INSTANCE,'key_file':'/run/secrets/broker-key','max_pi':2},
        'targets':{'action':{'tools':['files','exec','read_attachment','create_artifact'],'image_digest':PARAMS['images']['action']}},'profiles':{'dedicated':profile},
        'credentials':{'dedicated':{'protocol':'openai-completions','base_url':'https://fixture-model/v1','models':['fixture/model'],
            'key_file':'/run/secrets/provider-key','ca_file':'/config/model-ca.pem','allowed_ips':[ip]}}}
    config={'agents.yaml':agents,'isolation.yaml':isolation,'browser.yaml':{'enabled':False},
        'sandbox.yaml':{'default':'none','limits':{'max_task_seconds':180,'max_tool_calls':16,'max_output_bytes':65536}},
        'models.yaml':{'providers':{'fixture':{'type':'openai_compatible','base_url':'https://fixture-model/v1','api_key_default':provider_key}},'tiers':{'balanced':{'candidates':['fixture/model']}}}}
    if PARAMS.get('docker_browser'):
        isolation['browser'] = {'image_digest': PARAMS['images']['browser']}
        isolation['targets']['action']['tools'] += ['browser', 'computer']
        config['browser.yaml'] = {'enabled': True}
        for key in ('api', 'pi'):
            agents['agents'][key]['tools'] += ['browser', 'computer']
    for filename,value in config.items():(home/'config'/filename).write_text(yaml.safe_dump(value))
    control=INSTANCE+'-control';NAMES.append(control)
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0)); fixed_port=reserve.getsockname()[1]
    args=['run','-d','--name',control,'--label','carme.fixture='+INSTANCE,'--network','bridge','--publish',f'127.0.0.1:{fixed_port}:8899','--read-only','--user','1000:1000',
          '--cap-drop=ALL','--security-opt=no-new-privileges:true','--pids-limit','128','--memory','768m','--cpus','1',
          '--tmpfs','/runtime:rw,nosuid,nodev,size=128m,uid=1000,gid=1000,mode=700','--tmpfs','/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777',
          '--env','SSL_CERT_FILE=/config/model-ca.pem']
    for src,dst,ro in [('config','/config',False),('runtime/control','/control-state',False),('runtime/artifacts','/artifacts',False),('runtime/skills','/skills',True)]:
        args+=['--mount',f'type=bind,src={home/src},dst={dst}'+(',readonly' if ro else '')]
    for secret in ('control-token','broker-key','provider-key'):
        args+=['--mount',f'type=bind,src={home/"runtime/secrets"/secret},dst=/run/secrets/{secret},readonly']
    CONTROL_LAUNCH=[*args,PARAMS['images']['control']];CONTROL_NETWORK=network
    docker(*CONTROL_LAUNCH)
    docker('network','connect',network,control)
    for _ in range(40):
        status=json.loads(docker('inspect',control))[0]
        ports=status['NetworkSettings']['Ports'].get('8899/tcp') or []
        if ports:break
        if not status['State']['Running']:raise RuntimeError('Control exited: '+docker('logs',control)[-1800:])
        time.sleep(.25)
    if not ports:raise RuntimeError('Control port unpublished: '+json.dumps(status['State']))
    port=ports[0]['HostPort']
    broker={'home':str(home),'instance_id':INSTANCE,'key_file':str(home/'runtime/secrets/broker-key'),
        'control_url':'http://127.0.0.1:'+port,'docker_binary':PARAMS['docker'],'docker_config':PARAMS['docker_config'],
        'docker_context':PARAMS['docker_context'],'images':{k:PARAMS['images'][k] for k in ('pi','action')},'targets':['action'],'max_pi':2}
    if PARAMS.get('docker_browser'):
        broker['images']['browser'] = PARAMS['images']['browser']
    (home/'broker.json').write_text(json.dumps(broker));(home/'broker.json').chmod(0o600)
    # An unrelated synthetic process and container must survive every cancellation/cleanup.
    personal=INSTANCE+'-personal';NAMES.append(personal)
    docker('run','-d','--name',personal,'--label','carme.fixture='+INSTANCE,'--network','none','--read-only',
        '--entrypoint','python',PARAMS['images']['action'],'-c','import time;time.sleep(1800)')
    PERSONAL_PROCESS=subprocess.Popen([sys.executable,'-c','import time; time.sleep(1800)'],env=ENV,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    return home,broker,admin,control,personal,capture


def start_broker(home):
    global BROKER
    env={**ENV,'HOME':str(RUN/'broker-home'),'PYTHONDONTWRITEBYTECODE':'1','CARME_LOAD_ENV':'0'}
    Path(env['HOME']).mkdir(exist_ok=True)
    log=(RUN/'broker.log').open('a')
    BROKER=subprocess.Popen([sys.executable,'-B','-m','carme.broker',str(home/'broker.json')],cwd=SOURCE,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    log.close()


async def main():
    home,broker,admin,control,personal,capture=setup()
    client=httpx.AsyncClient(base_url=broker['control_url'],headers={'Authorization':'Bearer '+admin},trust_env=False,timeout=10)
    async def wait_ready():
        for _ in range(80):
            try:
                response=await client.get('/api/health')
                if response.status_code==200:return response.json()
            except httpx.HTTPError:pass
            await asyncio.sleep(.25)
        raise RuntimeError('Control not ready: '+docker('logs',control)[-1500:])
    await wait_ready();start_broker(home)
    for _ in range(100):
        health=(await client.get('/api/health')).json()
        if health['execution']['broker']=='ready':break
        if BROKER.poll() is not None:raise RuntimeError('Broker exited: '+(RUN/'broker.log').read_text()[-1500:])
        await asyncio.sleep(.25)
    assert health['execution']['broker']=='ready'
    record('control_broker_authenticated_ready')
    probe=await client.post('/api/engines/pi/test',json={'model':'fixture/model','runtime_profile':'dedicated'})
    assert probe.json()['ok'],probe.json()
    record('dedicated_profile_connection_probe_no_tools')
    async def conversation(agent):
        response=await client.post('/api/conversations',json={'agent_ids':[agent]});response.raise_for_status()
        return response.json()['conversation']['id']
    async def launch(agent,case):
        cid=await conversation(agent)
        response=await client.post(f'/api/conversations/{cid}/messages',json={'content':'M2_CASE:'+case,'request_id':secrets.token_hex(8),'agent_id':agent})
        response.raise_for_status();return cid,response.json()['task_id']
    async def settle(tid,approve=True,timeout=120):
        end=time.time()+timeout
        while time.time()<end:
            inspect_workers()
            for row in (await client.get('/api/approvals')).json()['approvals']:
                if row['task_id']==tid and approve:
                    response=await client.post('/api/approvals/'+row['id']+'/decide',json={'approved':True,'note':'synthetic fixture command only'})
                    response.raise_for_status()
            task=(await client.get('/api/tasks/'+tid)).json()['task']
            if task['status'] in ('done','failed','cancelled'):return task
            await asyncio.sleep(.15)
        raise RuntimeError('Task timeout '+tid)
    async def artifact(cid,filename):
        detail=(await client.get('/api/conversations/'+cid)).json()
        file=next(f for f in detail['files'] if f['name']==filename)
        raw=(await client.get(f'/api/conversations/{cid}/attachments/{file["id"]}/download')).content
        assert hashlib.sha256(raw).hexdigest()==file['sha256']
        assert json.loads(file['provenance'])['status']=='unverified'
        return file,raw
    async def active_action():
        _,tid=await launch('pi','cancel')
        for _ in range(150):
            for row in (await client.get('/api/approvals')).json()['approvals']:
                if row['task_id']==tid:await client.post('/api/approvals/'+row['id']+'/decide',json={'approved':True})
            inspect_workers()
            if docker('ps','-q','--filter','label=carme.instance='+INSTANCE,'--filter','label=carme.run='+tid,'--filter','label=carme.role=action'):
                return tid
            await asyncio.sleep(.2)
        raise RuntimeError('No active Action for interruption test')
    async def drained(tid):
        for _ in range(100):
            if not docker('ps','-q','--filter','label=carme.instance='+INSTANCE,'--filter','label=carme.run='+tid):return
            await asyncio.sleep(.2)
        raise RuntimeError('Owned worker still running after interruption')
    if PARAMS.get('docker_browser'):
        for agent in ('api', 'pi'):
            cid, tid = await launch(agent, 'browser');task = await settle(tid)
            assert task['status'] == 'done', task.get('error')
            detail = (await client.get('/api/tasks/' + tid)).json()
            assert 'Example Domain' in json.dumps(detail), detail
            assert 'browser_destination_or_response_denied' in json.dumps(detail), detail
            files = (await client.get('/api/conversations/' + cid)).json()['files']
            file = next(f for f in files if f['name'].startswith('docker-browser_'))
            raw = (await client.get(f'/api/conversations/{cid}/attachments/{file["id"]}/download')).content
            assert raw.startswith(b'\x89PNG\r\n\x1a\n') and hashlib.sha256(raw).hexdigest() == file['sha256']
            await drained(tid)
            record(agent + '_docker_control_real_browser_bridge_artifact_private_egress_denied',
                   {'task': tid, 'image': PARAMS['images']['browser'], 'artifact_sha256': file['sha256']})
        await client.aclose();return
    if PARAMS.get('m3_project'):
        from carme import projects
        cid=await conversation('pi');bundle=json.loads((RUN/'snapshot/bundle.json').read_text())
        response=await client.post(f'/api/conversations/{cid}/messages',json={'content':'M2_CASE:project','request_id':secrets.token_hex(8),'agent_id':'pi','project_snapshot':bundle})
        response.raise_for_status();tid=response.json()['task_id'];task=await settle(tid)
        assert task['status']=='done',task.get('error')
        detail=(await client.get('/api/tasks/'+tid)).json();assert 'VALIDATED_SNAPSHOT' in json.dumps(detail)
        file,raw=await artifact(cid,'patch.json');original=RUN/'selected-project'
        assert (original/'main.txt').read_text()=='before'
        record('m3_real_pi_snapshot_action_artifact_validation',{'task':tid,'snapshot_id':bundle['snapshot_id'],'artifact_id':file['id'],'sha256':file['sha256']})
        patch=RUN/'patch.json';patch.write_bytes(raw)
        projects.save(RUN/'validation.json',{'snapshot_id':bundle['snapshot_id'],'artifact_sha256':hashlib.sha256(raw).hexdigest(),'checks':[{'name':'real Action test expected bytes and no private Pi directory','status':'pass','task_id':tid}]})
        review=projects.proposal(RUN/'snapshot',patch,RUN/'validation.json',RUN/'review.json')
        (original/'other.txt').write_text('human concurrent edit')
        try:projects.apply(RUN/'snapshot',RUN/'review.json',review['approval_digest'],RUN/'apply.json')
        except ValueError as exc:assert 'project_conflict' in str(exc)
        else:raise AssertionError('concurrent edit overwritten')
        assert (original/'main.txt').read_text()=='before';record('m3_human_project_edit_conflict_refused')
        (original/'other.txt').write_text('human original')
        projects.apply(RUN/'snapshot',RUN/'review.json',review['approval_digest'],RUN/'apply.json');assert (original/'main.txt').read_text()=='after'
        projects.rollback(RUN/'apply.json');assert (original/'main.txt').read_text()=='before'
        record('m3_approved_patch_apply_and_rollback')
        (EVIDENCE/'docker-inspect.json').write_text(json.dumps(INSPECTED,indent=2))
        await client.aclose();return
    if PARAMS.get('fault_only'):
        _,tid=await launch('pi','cpu');assert (await settle(tid))['status']=='done'
        assert 'CPU_QUOTA_EFFECTIVE' in json.dumps((await client.get('/api/tasks/'+tid)).json())
        record('cpu_quota_throttles_real_busy_children')
        tid=await active_action();BROKER.kill();BROKER.wait(timeout=10)
        start_broker(home)
        failed=await settle(tid);assert failed['status']=='failed',failed
        await drained(tid)
        assert PERSONAL_PROCESS.poll() is None and json.loads(docker('inspect',personal))[0]['State']['Running']
        record('broker_crash_reconciles_owned_workers_without_replay')
        tid=await active_action();docker('kill',control);docker('rm',control)
        docker(*CONTROL_LAUNCH);docker('network','connect',CONTROL_NETWORK,control);await wait_ready()
        failed=await settle(tid);assert failed['status']=='failed' and '重启' in failed['error'],failed
        await drained(tid)
        assert PERSONAL_PROCESS.poll() is None and json.loads(docker('inspect',personal))[0]['State']['Running']
        record('control_crash_revokes_active_run_without_replay')
        (EVIDENCE/'docker-inspect.json').write_text(json.dumps(INSPECTED,indent=2))
        await client.aclose();return
    for agent in ('pi','api'):
        cid,tid=await launch(agent,'binary');task=await settle(tid)
        if task['status']!='done':raise RuntimeError(agent+' failed: '+str(task.get('error'))+' '+str(task.get('output'))[:600])
        file,raw=await artifact(cid,'result.zip')
        with zipfile.ZipFile(io.BytesIO(raw)) as z:assert z.read('hello.txt').decode()=='容器二进制成果'
        record(agent+'_real_action_binary_artifact',{'sha256':file['sha256'],'task':tid,'bytes':len(raw)})
        if agent=='pi':saved=(cid,tid,file,raw)
    _,seed_task=await launch('pi','seed');assert (await settle(seed_task))['status']=='done'
    for agent in ('pi','peer'):
        cid,tid=await launch(agent,'foreign');assert (await settle(tid))['status']=='done'
        detail=(await client.get('/api/tasks/'+tid)).json()
        assert 'RUN_SCOPE_ISOLATED' in json.dumps(detail)
        assert (await client.get(f'/api/conversations/{cid}/attachments/{saved[2]["id"]}/download')).status_code==404
    record('same_bot_and_other_bot_run_workspace_and_artifact_scope')
    cid,tid=await launch('pi','isolation');task=await settle(tid);assert task['status']=='done',task.get('error')
    _,raw=await artifact(cid,'isolation.json');bound=json.loads(raw)
    assert bound['uid']==1000 and not any(bound['exists'].values())
    assert bound['writes']=={'/inputs/deny':False,'/app/deny':False,'/workspace/allowed':True,'/out/allowed':True}
    assert not any(bound['connections'].values())
    assert not set(bound['env']) & {'CARME_TOKEN','CARME_BRIDGE_TOKEN','SSH_AUTH_SOCK','NODE_OPTIONS','DOCKER_HOST','OPENAI_API_KEY'}
    record('actual_mount_env_readonly_network_isolation',bound)
    cid,tid=await launch('readonly','readonly');task=await settle(tid);assert task['status']=='done',task.get('error')
    assert not (home/'runtime/runs'/tid).exists()
    record('pi_native_and_ungranted_tools_denied')
    for case in ('output','timeout','memory','pids'):
        cid,tid=await launch('pi',case);task=await settle(tid)
        assert task['status']=='done',task.get('error')
        detail=(await client.get('/api/tasks/'+tid)).json()
        evidence=json.dumps(detail)
        assert ('worker_failed' in evidence if case in ('output','timeout') else 'exit=137' in evidence if case=='memory' else 'PIDS_LIMIT_EFFECTIVE' in evidence),case
        record('action_'+case+'_bound')
    cid,tid=await launch('pi','cancel')
    end=time.time()+45
    active=[]
    while time.time()<end:
        for row in (await client.get('/api/approvals')).json()['approvals']:
            if row['task_id']==tid:await client.post('/api/approvals/'+row['id']+'/decide',json={'approved':True})
        inspect_workers()
        ids=docker('ps','-q','--filter','label=carme.instance='+INSTANCE,'--filter','label=carme.run='+tid,'--filter','label=carme.role=action')
        if ids:active=ids.splitlines();break
        await asyncio.sleep(.2)
    assert active,'No Action container for cancellation'
    await client.post('/api/tasks/'+tid+'/cancel')
    assert (await settle(tid))['status']=='cancelled'
    for _ in range(80):
        if not docker('ps','-q','--filter','label=carme.instance='+INSTANCE,'--filter','label=carme.run='+tid):break
        await asyncio.sleep(.2)
    assert not docker('ps','-q','--filter','label=carme.instance='+INSTANCE,'--filter','label=carme.run='+tid)
    assert json.loads(docker('inspect',personal))[0]['State']['Running']
    assert PERSONAL_PROCESS.poll() is None
    record('cancel_terminates_owned_pi_action_and_descendants',{'synthetic_personal_process_survived':True,'unrelated_container_survived':True})
    for case,code in (('authfailure',401),('redirect',302)):
        cid,tid=await launch('pi',case);failed=await settle(tid)
        assert failed['status']=='failed' and failed['error']==f'model_provider_http_{code}',failed.get('error')
        health=(await client.get('/api/health')).json()['execution']
        assert health['broker']=='ready' and health['pi']=='ready'
        record('model_'+case+'_reported_separately_from_docker')
    # Disconnect only this Broker's context; never stop the shared Docker daemon.
    BROKER.send_signal(signal.SIGTERM);BROKER.wait(timeout=20)
    for _ in range(80):
        if (await client.get('/api/health')).json()['execution']['broker']=='offline':break
        await asyncio.sleep(.2)
    cid,tid=await launch('pi','binary');failed=await settle(tid)
    assert failed['status']=='failed' and 'container_runner_unavailable' in failed['error']
    record('broker_offline_fails_without_host_fallback')
    bad_context=INSTANCE+'-unreachable'
    docker('context','create',bad_context,'--docker','host=unix://'+str(RUN/'absent-docker.sock'))
    invalid={**broker,'docker_context':bad_context};(home/'broker.json').write_text(json.dumps(invalid))
    start_broker(home)
    for _ in range(80):
        if BROKER.poll() is not None:break
        await asyncio.sleep(.1)
    assert BROKER.poll() not in (None,0)
    assert json.loads(docker('inspect',personal))[0]['State']['Running']
    record('isolated_daemon_connection_failure_no_context_fallback')
    docker('context','rm',bad_context)
    isolation=yaml.safe_load((home/'config/isolation.yaml').read_text())
    actual_pi=isolation['profiles']['dedicated']['image_digest'];missing='sha256:'+'0'*64
    isolation['profiles']['dedicated']['image_digest']=missing
    (home/'config/isolation.yaml').write_text(yaml.safe_dump(isolation))
    await client.post('/api/config/reload')
    (home/'broker.json').write_text(json.dumps({**broker,'images':{**broker['images'],'pi':missing}}))
    start_broker(home)
    for _ in range(100):
        if (await client.get('/api/health')).json()['execution']['broker']=='ready':break
        await asyncio.sleep(.1)
    cid,tid=await launch('pi','binary');failed=await settle(tid)
    assert failed['status']=='failed' and 'docker_operation_failed:create' in failed['error'],failed.get('error')
    record('missing_registered_image_fails_without_host_fallback')
    BROKER.send_signal(signal.SIGTERM);BROKER.wait(timeout=20)
    isolation['profiles']['dedicated']['image_digest']=actual_pi
    (home/'config/isolation.yaml').write_text(yaml.safe_dump(isolation));await client.post('/api/config/reload')
    (home/'broker.json').write_text(json.dumps(broker));start_broker(home)
    # Remove and recreate Control; one-writer SQLite and exact bytes survive.
    docker('stop',control);docker('rm',control)
    docker(*CONTROL_LAUNCH);docker('network','connect',CONTROL_NETWORK,control);await wait_ready()
    cid,tid,file,raw=saved
    received=(await client.get(f'/api/conversations/{cid}/attachments/{file["id"]}/download')).content
    assert received==raw
    record('control_recreate_preserves_database_and_binary_bytes')
    requests=[json.loads(line) for line in (capture/'requests.jsonl').read_text().splitlines()]
    assert requests and all('PERSONAL_CANARY' not in json.dumps(req) for req in requests)
    assert all('RUN_PRIVATE_CANARY_M2' not in json.dumps(req) for req in requests if 'M2_CASE:foreign' in json.dumps(req))
    (EVIDENCE/'docker-inspect.json').write_text(json.dumps(INSPECTED,indent=2))
    record('docker_inspect_actual_constraints',{'containers_observed':len(INSPECTED),'requests':len(requests)})
    await client.aclose()


try:
    asyncio.run(main())
except BaseException as exc:
    RESULTS.append({'id':'integration','status':'fail','reason':type(exc).__name__+':'+str(exc)[:1500]})
    (EVIDENCE/'docker-results.json').write_text(json.dumps({'instance':INSTANCE,'run':str(RUN),'tests':RESULTS},indent=2))
    raise
finally:
    if PERSONAL_PROCESS and PERSONAL_PROCESS.poll() is None:
        PERSONAL_PROCESS.terminate();PERSONAL_PROCESS.wait(timeout=5)
    if BROKER and BROKER.poll() is None:
        BROKER.send_signal(signal.SIGTERM)
        try:BROKER.wait(timeout=20)
        except subprocess.TimeoutExpired:os.killpg(BROKER.pid,signal.SIGTERM);BROKER.wait(timeout=10)
    # Exact owned names/labels only. Keep private evidence/data directories.
    for cid in docker('ps','-aq','--filter','label=carme.instance='+INSTANCE,check=False).splitlines():
        value=json.loads(docker('inspect',cid))[0]
        if value['Config']['Labels'].get('carme.instance')==INSTANCE:docker('rm','-f',cid,check=False)
    for name in reversed(NAMES):
        value=docker('inspect',name,check=False)
        if value and json.loads(value)[0]['Config']['Labels'].get('carme.fixture')==INSTANCE:docker('rm','-f',name,check=False)
    for name in NETWORKS:docker('network','rm',name,check=False)
    (EVIDENCE/'cleanup.json').write_text(json.dumps({'instance':INSTANCE,'remaining_workers':docker('ps','-aq','--filter','label=carme.instance='+INSTANCE),'fixture_names':NAMES,'no_global_cleanup':True},indent=2))
