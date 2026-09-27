"""M8 isolated candidate: real Gateway HTTP -> Docker Control/Broker/Pi/Action.
Uses the existing fixture and exact-instance cleanup; no production mutation.
"""
import asyncio, hashlib, json, secrets, socket, sys
from pathlib import Path
import httpx
import uvicorn
from carme.gateway import create_gateway, initialize, database, password_hash, csrf

async def run(client, settle, record, home, capture, params, evidence):
    async def owner(method,path,body=None):
        r=await client.request(method,'/api'+path,json=body)
        assert r.is_success,(method,path,r.status_code)
        return r.json()
    cid=(await owner('POST','/conversations',{'agent_ids':['pi','api'],'title':'M8 visitor candidate'}))['conversation']['id']
    other=(await owner('POST','/conversations',{'agent_ids':['pi','api'],'title':'M8 other group'}))['conversation']['id']
    hidden='M8_HIDDEN_'+secrets.token_hex(8)
    await owner('POST',f'/conversations/{cid}/messages',{'content':hidden,'request_id':'hidden','mode':'message'})
    for a in ('pi','api'):
        await owner('PUT','/memory/'+a,{'key':'m8_private','value':hidden})
    invites=[]
    path=f'/conversations/{cid}/visitors'
    for i in range(3):
        invites.append(await owner('POST',path,{'display_name':f'Visitor {i+1}','expected_revision':i}))
    rejects=await asyncio.gather(*[client.post('/api'+path,json={'display_name':'Fourth','expected_revision':3}) for _ in range(3)])
    assert all(r.status_code==409 for r in rejects)
    assert 'password' not in json.dumps(await owner('GET',path))
    record('three_visitors_concurrent_fourth_denied_password_once')
    gatehome=home.parent/'gateway';gatehome.mkdir()
    (gatehome/'installation.json').write_text(json.dumps({'id':'m8','home':str(gatehome)}));initialize(gatehome)
    base=gatehome/'accounts/alice';(base/'runtime/secrets').mkdir(parents=True)
    (base/'runtime/secrets/control-token').write_bytes((home/'runtime/secrets/control-token').read_bytes())
    import yaml
    instance=yaml.safe_load((home/'config/isolation.yaml').read_text())['broker']['instance_id']
    port=client.base_url.port
    (base/'account.json').write_text(json.dumps({'id':'alice','home':str(base),'installation':'m8','instance_id':instance,'port':port,'origin':f'http://c0000000000000000.localhost:{port}'}))
    with database(gatehome) as db:db.execute('INSERT INTO users(account,instance,password) VALUES(?,?,?)',('alice',instance,password_hash('synthetic-owner-password')))
    sock=socket.socket();sock.bind(('127.0.0.1',0));origin='http://127.0.0.1:'+str(sock.getsockname()[1])
    app=create_gateway({'home':str(gatehome),'origin':origin,'port':sock.getsockname()[1],'local_only':True,'web_dir':str(Path(params['release'])/'web/dist')})
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));serving=asyncio.create_task(server.serve(sockets=[sock]))
    visitors=[httpx.AsyncClient(base_url=origin,trust_env=False,timeout=30) for _ in invites]
    prefix=f'/api/visitor/alice/conversations/{cid}'
    async def vpost(c,url,body):return await c.post(url,json=body,headers={'Origin':origin,'X-Carme-CSRF':csrf(c.cookies.get('carme_visitor',''))})
    try:
        while not server.started:
            if serving.done():await serving
            await asyncio.sleep(.05)
        logins=await asyncio.gather(*[vpost(c,'/api/visitor/alice/login',{'username':v['visitor']['username'],'password':v['password'],'conversation_id':cid}) for c,v in zip(visitors,invites)])
        assert all(r.status_code==200 for r in logins),[r.status_code for r in logins]
        record('real_gateway_to_docker_three_independent_logins')
        for c in visitors:
            for p in ('/api/conversations','/api/agents','/api/visitor/bob/session',f'/api/visitor/alice/conversations/{other}','/api/visitor/alice/approvals'):
                assert (await c.get(p)).status_code in (401,403,404)
            assert hidden not in (await c.get(prefix+'/messages')).text
        direct=await client.get('/api/visitor/alice/session',headers={'X-Carme-Visitor-ID':invites[0]['visitor']['id']})
        assert direct.status_code in (401,403)
        record('cross_account_group_admin_direct_forgery_and_history_denied')
        # Denied cross-account requests deliberately clear an invalid visitor cookie.
        for c,v in zip(visitors,invites):
            r=await vpost(c,'/api/visitor/alice/login',{'username':v['visitor']['username'],'password':v['password'],'conversation_id':cid})
            assert r.status_code==200
        messages=await asyncio.gather(*[vpost(c,prefix+'/messages',{'content':f'PUBLIC_M8_{i}','request_id':'same-key','mode':'message'}) for i,c in enumerate(visitors)])
        assert all(r.status_code==200 for r in messages),[r.status_code for r in messages]
        rows=(await visitors[0].get(prefix+'/messages')).json()['messages']
        assert len(rows)==3 and len({r['sender_id'] for r in rows})==3
        record('concurrent_three_human_senders_same_request_key_isolated')
        real=bool(params.get('group_provider_config'))
        prompt=('仅回答 PUBLIC_M8_0。不要调用工具，不要猜测入群前历史或私人记忆。' if real else 'M8_COMPUTE 6 multiplied by 7, generate result.json')
        response=await vpost(visitors[0],prefix+'/messages',{'content':prompt,'request_id':'compute','mode':'task'})
        assert response.status_code==200,response.status_code
        tasks=[]
        for tid in response.json()['task_ids']:
            task=await settle(tid,approve=False,timeout=240)
            assert task['status']=='done',('visitor task',task['status'],task['agent_id'])
            tasks.append(task)
        assert len(tasks)==2
        if real:assert all('PUBLIC_M8_0' in t['result'] and hidden not in t['result'] for t in tasks)
        else:
            files=(await owner('GET',f'/conversations/{cid}'))['files']
            assert files
            for file in files:
                r=await visitors[0].get(prefix+'/attachments/'+file['id']+'/download')
                assert r.status_code==200 and json.loads(r.content)=={'result':42},r.status_code
                assert hashlib.sha256(r.content).hexdigest()==file['sha256']
                assert (await visitors[0].get(f'/api/visitor/alice/conversations/{other}/attachments/{file["id"]}/download')).status_code in (403,404)
            calls=[json.loads(x) for x in (capture/'requests.jsonl').read_text().splitlines()]
            assert calls and all(hidden not in json.dumps(x) for x in calls)
        record('visitor_group_two_engines_results_files_and_common_context',{'real_provider':real,'tasks':len(tasks),'artifact_download':not real})
        # Browser-discovered regression: ordinary uploads have an empty stored SHA256.
        uploaded=await client.post('/api/conversations/'+cid+'/attachments',params={'name':'owner-input.txt'},
                                   content=b'M8_PUBLIC_INPUT_42',headers={'Content-Type':'application/octet-stream'})
        assert uploaded.status_code==201,uploaded.status_code
        upload=uploaded.json()['file']
        posted=await owner('POST',f'/conversations/{cid}/messages',{'content':'Only reply received, no tools.',
                          'request_id':'uploaded-input','attachment_ids':[upload['id']]})
        for tid in posted['task_ids']:
            task=await settle(tid,approve=False,timeout=240)
            assert task['status']=='done',('uploaded input',task['status'],task.get('error'))
        downloaded=await visitors[1].get(prefix+'/attachments/'+upload['id']+'/download')
        assert downloaded.status_code==200 and downloaded.content==b'M8_PUBLIC_INPUT_42'
        record('owner_upload_empty_hash_two_engines_input_and_visitor_download')
        private=(await owner('POST','/conversations',{'agent_ids':['pi'],'title':'M8 owner private'}))['conversation']['id']
        task=(await owner('POST',f'/conversations/{private}/messages',{'content':'Only reply OWNER_OK, no tools.','request_id':'owner'}))['task_id']
        assert (await settle(task,approve=False,timeout=240))['status']=='done'
        record('owner_private_task_compatible')
        v=invites[0]['visitor'];await owner('PATCH',path+'/'+v['id'],{'action':'remove','expected_revision':3})
        assert (await visitors[0].get(prefix+'/messages')).status_code==401
        assert (await vpost(visitors[0],prefix+'/messages',{'content':'denied','request_id':'late','mode':'task'})).status_code in (401,403)
        assert (await visitors[1].get(prefix+'/messages')).status_code==200
        record('revocation_denies_read_and_new_task_other_visitors_survive')
    finally:
        for c in visitors:await c.aclose()
        server.should_exit=True
        await asyncio.wait_for(serving,15)

if __name__=='__main__':
    source=Path(__file__).with_name('test_isolation_m2_docker.py')
    code=source.read_text()
    code=code.replace("INSTANCE = 'm2-' + secrets.token_hex(4)","INSTANCE = 'carme-' + secrets.token_hex(10)")
    code=code.replace('admin=secrets.token_urlsafe(32)','admin=secrets.token_hex(32)')
    code=code.replace("CONTROL_LAUNCH=[*args,PARAMS['images']['control']]","CONTROL_LAUNCH=[*args,'--env','CARME_ACCOUNT_ID=alice',PARAMS['images']['control']]")
    code=code.replace("creation_source='user_created', tools=[]","creation_source='user_created', tools=['files','exec']")
    code=code.replace('from test_group_docker import run','from test_visitor_candidate import run')
    # Synthetic provider uses real tool calls; public model mode keeps tools disabled.
    code=code.replace("actions=CASES.get(case,[])",'''actions=CASES.get(case,[])
  if 'M8_COMPUTE' in content:
   actions=[['shell',{'command':"python -c \\\"from pathlib import Path; Path('/out/result.json').write_text('{\\\\\\\"result\\\\\\\":42}')\\\""}],['create_artifact',{'name':'result.json','path':'/out/result.json'}]]''')
    scope={'__file__':str(source),'__name__':'m8_fixture'}
    exec(compile(code,str(source),'exec'),scope)
