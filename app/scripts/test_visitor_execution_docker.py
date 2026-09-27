"""M6 real Action workers with Broker-generated specs, synthetic Store/Agent.
Local synthetic fixture supplies only the transport; no production changes.
Uses existing immutable Action image; does not deploy a Control or touch live data.
"""
import asyncio,hashlib,json,os,secrets,shlex,sys,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from test_visitor_context import ContextTests
from carme.broker import Broker
from carme.sandbox.docker import DockerSandbox
from carme.tools.base import ToolContext
from carme.attachments import archive_binary,file_path

async def main():
    root=Path(__file__).resolve().parents[2]
    params=json.loads((root/'docs/carme-isolation-plan/implementation/evidence/group-g7-20260926/params-group.json').read_text())
    docker=[params['docker'],'--config',params['docker_config'],'--context',params['docker_context']]
    fixture=ContextTests();await fixture.asyncSetUp();r,s=fixture.r,fixture.s
    instance='visitor-m6-'+secrets.token_hex(4)
    home=fixture.root/'broker';home.mkdir();key=home/'key';key.write_bytes(secrets.token_bytes(48))
    config={'instance_id':instance,'home':str(home),'key_file':str(key),'control_url':'http://127.0.0.1:1',
            'docker_binary':params['docker'],'docker_config':params['docker_config'],'docker_context':params['docker_context'],
            'images':{'action':params['images']['action']},'targets':['action']}
    broker=Broker(config);inspections=[];calls=[]
    async def command(*args):
        proc=await asyncio.create_subprocess_exec(*docker,*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await asyncio.wait_for(proc.communicate(),60)
        if proc.returncode:raise AssertionError('Docker operation failed: '+err.decode()[:300])
        return out.decode()
    tid=fixture.task('Compute 6 * 7');policy=fixture.policy(tid)
    async def submit(run_id,role,payload,**kwargs):
        s.task_context(run_id);assert role=='action'
        job={'job_id':secrets.token_hex(16),'instance_id':instance,'bot_id':'chief','run_id':run_id,
             'role':role,'target_id':'action','image_digest':params['images']['action'],'deadline':time.time()+60,
             'max_output_bytes':65536,'permission_version':policy['permission_version'],
             'token_id':secrets.token_hex(16),'audience':'worker-relay','tools':policy['tools'],'payload':payload}
        name,args=broker.create_args(job);proc=None
        try:
            await command(*args)
            inspect=json.loads(await command('inspect',name))[0];h=inspect['HostConfig'];mounts=inspect['Mounts']
            assert h['NetworkMode']=='none' and h['ReadonlyRootfs'] and not h['Privileged']
            assert h['CapDrop']==['ALL'] and inspect['Config']['User']=='1000:1000'
            assert {m['Destination'] for m in mounts}=={'/workspace','/inputs','/out'}
            assert all(Path(m['Source']).is_relative_to(home/'runtime/runs'/run_id) for m in mounts)
            assert not next(m for m in mounts if m['Destination']=='/inputs')['RW']
            inspections.append({'role':role,'network':'none','read_only_root':True,'task_only_mounts':True,'inputs_read_only':True})
            proc=await asyncio.create_subprocess_exec(*docker,'start','-ai',name,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            proc.stdin.write((json.dumps(job)+'\n').encode());await proc.stdin.drain()
            line=await asyncio.wait_for(proc.stdout.readline(),60)
            reply=json.loads(line);assert reply['type']=='result',reply
            assert 'error' not in reply['result'],reply
            await asyncio.wait_for(proc.wait(),10);calls.append(payload['op']);return reply['result']
        finally:
            if proc and proc.returncode is None:proc.kill();await proc.wait()
            await command('rm','-f',name)
    sandbox=DockerSandbox(SimpleNamespace(task_id=tid,mode='docker',settings={},agent_id='chief'),r.execution)
    sandbox._ready=True  # Transport uses the real Broker-generated worker, without a live Control queue.
    async def get():return sandbox
    ctx=ToolContext(agent=fixture.spec,task_id=tid,store=s,sandbox_handle=SimpleNamespace(get=get),browser_manager=r.browsers,
                    extras={'policy':policy,'check_policy':lambda:s.task_context(tid)})
    try:
        with patch.object(r.execution,'submit',side_effect=submit):
            raw=b'6,7';input_file=archive_binary(s,fixture.cid,'input.csv',raw,task_id=tid)
            input_path='artifacts/'+input_file['id']+'/input.csv'
            assert s.artifact_access(tid,input_file['id'])['sha256']==hashlib.sha256(raw).hexdigest()
            await r.execution.stage_input(tid,input_path,raw)
            command_text="python -c \"from pathlib import Path; import json; a,b=map(int,Path('/inputs/artifacts/synthetic/input.csv').read_text().split(',')); Path('/out/result.json').write_text(json.dumps({'result':a*b})); print(a*b)\""
            command_text=command_text.replace('artifacts/synthetic/input.csv',input_path)
            result=await r.registry.execute(ctx,'shell',{'command':command_text})
            assert '42' in result,result
            probe_code="""from pathlib import Path
import socket
assert not Path('/control-state').exists()
assert not Path('/profile').exists()
assert not Path('/session').exists()
try:
    Path('/inputs/should-not-write').write_text('denied')
except OSError:
    pass
else:
    raise AssertionError('inputs writable')
sock=socket.socket();sock.settimeout(1)
assert sock.connect_ex(('1.1.1.1',443)) != 0
sock.close()
print('isolated')
"""
            probe=await submit(tid,'action',{'op':'exec','cwd':'/workspace','timeout':10,'command':'python -c '+shlex.quote(probe_code)})
            assert probe['ok'],probe
            artifact=json.loads(await r.registry.execute(ctx,'create_artifact',{'name':'result.json','path':'/out/result.json'}))
            content=file_path(s,artifact['id']).read_bytes();assert json.loads(content)=={'result':42}
            assert hashlib.sha256(content).hexdigest()==artifact['sha256']
            file=s.get_attachment(artifact['id']);assert file['conversation_id']==fixture.cid and file['task_id']==tid
        fixture.change('remove')
        for action in (lambda:s.add_conversation_message(fixture.cid,'chief','assistant','late',task_id=tid),
                       lambda:archive_binary(s,fixture.cid,'late.json',content,task_id=tid)):
            try:action()
            except ValueError:pass
            else:raise AssertionError('revoked Docker result published')
        before=len(calls)
        denied=await r.registry.execute(ctx,'shell',{'command':'echo forbidden'})
        assert 'conversation_context_changed' in denied and len(calls)==before
        remaining=(await command('ps','-aq','--filter','label=carme.instance='+instance)).strip();assert not remaining
        result={'result':'PASS','layer':'real Docker Action / synthetic local runtime transport; no model',
                'checks':8,'worker_invocations':len(inspections),'inspections':inspections,'operations':calls,
                'calculation':42,'artifact_hash_verified':True,'instance_containers_remaining':0,
                'image':params['images']['action']}
        out=root/'docs/carme-isolation-plan/implementation/evidence/visitor-m6-20260926/docker.json'
        out.write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
    finally:
        await broker.client.aclose();await broker.relay_client.aclose();await fixture.asyncTearDown()

if __name__=='__main__':asyncio.run(main())
