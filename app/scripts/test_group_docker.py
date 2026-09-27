"""G4 scenarios used by the existing isolated Docker acceptance harness."""
import asyncio
import json
import secrets
import time


async def run(client, settle, record, home, capture, params, evidence):
    real = bool(params.get('group_provider_config'))
    async def post(path, data, status=200):
        response = await client.post('/api'+path, json=data)
        assert response.status_code == status, (path, response.status_code)
        return response.json()
    async def detail(cid):
        return (await client.get('/api/conversations/'+cid)).json()
    async def send(cid, text, targets=None, request=None):
        data={'content':text,'request_id':request or secrets.token_hex(12)}
        if targets is not None:data['agent_ids']=targets
        result=await post('/conversations/'+cid+'/messages',data)
        tasks=[]
        for tid in result['task_ids']:
            task=await settle(tid,approve=False,timeout=180)
            assert task['status']=='done', ('group task failed',task['agent_id'])
            tasks.append(task)
        return result,tasks
    if real:
        # Each code is supplied only through its owner's private memory. The peer
        # can learn the predecessor's code only from the published group reply.
        codes={a:'G7-'+secrets.token_hex(6).upper() for a in ('pi','peer')}
        for a,code in codes.items():
            response=await client.put('/api/memory/'+a,json={'key':'g7_fixture','value':'本次验收自己的代码为 '+code+'；只在被要求时公布自己的代码。'})
            assert response.status_code==200
        check=(await post('/conversations',{'agent_ids':['pi','peer'],'title':'独立记忆顺序接续验收'},201))['conversation']['id']
        for turn in range(2):
            if turn:
                codes['pi']='G7-'+secrets.token_hex(6).upper()
                response=await client.put('/api/memory/pi',json={'key':'g7_fixture','value':'本次验收自己的最新代码为 '+codes['pi']+'；此前代码已被替换，公布时只用最新代码。'})
                assert response.status_code==200
            _,answers=await send(check,'本次为顺序接续验收，不调用工具。示例甲只输出自己记忆中的验收代码；示例乙输出示例甲刚刚公开的代码，再输出自己记忆中的验收代码。不要代写另一成员回复。')
            assert codes['pi'] in answers[0]['result'] and codes['peer'] not in answers[0]['result'],'author memory isolation failed'
            assert all(code in answers[1]['result'] for code in codes.values()),'peer did not receive published predecessor reply'
            for answer in answers:assert not (await client.get('/api/tasks/'+answer['id'])).json()['operations']
            record('private_memory_ordered_handoff_round_'+str(turn+1),{'tasks':2,'tools':0,'predecessor_code_recalled':True})
        for a in codes:
            response=await client.delete('/api/memory/'+a,params={'key':'g7_fixture'});assert response.status_code==200
    rows=(await client.get('/api/agents')).json()['agents']
    assert {a['id'] for a in rows if a['group_invitable']}=={'pi','peer','api'}
    await post('/conversations',{'agent_ids':['pi','readonly']},422)
    group=(await post('/conversations',{'agent_ids':['pi','peer'],'title':'群聊综合验收'},201))['conversation']
    cid=group['id'];path='/api/conversations/'+cid
    marker='G4-'+secrets.token_hex(4).upper()
    start=time.monotonic()
    _,tasks=await send(cid,f'项目代号 {marker}，格式 CSV，发布前需用户批准。不要使用工具或长期记忆。仅确认这些约定。')
    assert [t['agent_id'] for t in tasks]==['pi','peer']
    record('group_default_delivers_once_to_each_member',{'real_provider':real,'seconds':round(time.monotonic()-start,2)})
    rid=secrets.token_hex(12)
    message={'content':'@示例乙 格式改为 JSON，其他约定保持不变。不要调用工具，仅确认。','request_id':rid}
    responses=await asyncio.gather(*[client.post(path+'/messages',json=message) for _ in range(3)])
    assert all(r.status_code==200 for r in responses)
    ids=[r.json()['task_ids'] for r in responses];assert ids[0]==ids[1]==ids[2] and len(ids[0])==1
    task=await settle(ids[0][0],approve=False,timeout=180)
    assert task['status']=='done' and task['agent_id']=='peer'
    record('named_mention_chinese_and_concurrent_http_retry')
    _,tasks=await send(cid,'请各自复述项目代号、最新格式、发布限制。不调用工具，仅依据本群对话回答。')
    assert [t['agent_id'] for t in tasks]==['pi','peer']
    if real:
        assert all(marker in t['result'] and 'JSON' in t['result'] and ('批准' in t['result'] or '同意' in t['result']) for t in tasks),'real_context_missing'
        for t in tasks:assert not (await client.get('/api/tasks/'+t['id'])).json()['operations']
    d=await detail(cid)
    assert len([m for m in d['messages'] if m['role']=='user'])==3
    assert len(d['tasks'])==5
    record('multi_member_context_latest_format_and_single_user_rows',{'real_provider':real,'tasks':5,'user_messages':3})
    sessions=list((home/'runtime/control/pi-sessions'/cid).rglob('*.jsonl'))
    assert len(sessions)>=2
    if not real:
        requests=[json.loads(line) for line in (capture/'requests.jsonl').read_text().splitlines()]
        recalls=[r for r in requests if '请各自复述' in json.dumps(r['messages'],ensure_ascii=False)]
        assert len(recalls)==2
        assert all(marker in json.dumps(r['messages']) and 'JSON' in json.dumps(r['messages']) for r in recalls)
    record('distinct_native_member_sessions_and_model_input_context')
    response=await client.patch(path+'/members',json={'agent_ids':['pi','peer','api'],'expected_revision':0})
    assert response.status_code==200
    response=await client.patch(path+'/members',json={'agent_ids':['pi'],'expected_revision':0})
    assert response.status_code==409
    response=await client.patch(path+'/members',json={'agent_ids':['pi'],'expected_revision':1})
    assert response.status_code==200 and response.json()['conversation']['kind']=='group'
    await post('/conversations/'+cid+'/messages',{'content':'removed','request_id':secrets.token_hex(8),'agent_id':'peer'},422)
    response=await client.patch(path,json={'title':'用户修改后的群名'})
    assert response.status_code==200 and (await detail(cid))['conversation']['title']=='用户修改后的群名'
    response=await client.patch(path+'/members',json={'agent_ids':['pi','peer'],'expected_revision':2})
    assert response.status_code==200
    assert (await detail(cid))['messages']==d['messages']
    record('add_remove_readd_revision_single_member_and_rename_preserve_history')
    private=(await post('/conversations',{'agent_ids':['pi'],'title':'独立私聊'},201))['conversation']['id']
    _,private_tasks=await send(private,'只回答 PRIVATE_OK，不调用工具。')
    assert len(private_tasks)==1
    record('owner_private_chat_regression')
    if not real:
        fourth=(await post('/agents/peer/duplicate',{},201))['agent']['id']
        four=(await post('/conversations',{'agent_ids':['pi','peer','api',fourth],'title':'四成员送达验收'},201))['conversation']['id']
        _,four_tasks=await send(four,'四位成员分别确认收到本条消息。')
        assert [t['agent_id'] for t in four_tasks]==['pi','peer','api',fourth]
        assert len([m for m in (await detail(four))['messages'] if m['role']=='user'])==1
        record('four_member_group_one_input_four_actual_tasks')
    if params.get('browser_script'):
        import os
        from pathlib import Path
        context=evidence/'browser-context.json'
        context.write_text(json.dumps({'url':str(client.base_url),'token':client.headers['Authorization'][7:],'cid':cid}))
        context.chmod(0o600)
        process=await asyncio.create_subprocess_exec(params['node'],params['browser_script'],str(context),str(evidence),env=os.environ.copy())
        try:assert await asyncio.wait_for(process.wait(),90)==0,'browser_acceptance_failed'
        finally:
            if process.returncode is None:process.kill();await process.wait()
            context.unlink(missing_ok=True)
        record('real_docker_browser_desktop_mobile')
