"""M4 real Control/Broker/Pi/Action, synthetic TLS model and synthetic data only.
Run with a JSON parameter file like test_isolation_m2_docker.py. No personal files.
"""
from __future__ import annotations
import asyncio, base64, hashlib, io, json, os, secrets, shlex, signal, sqlite3, sys, time, zipfile
from pathlib import Path
import httpx,yaml
SOURCE=Path(__file__).resolve().parents[1]
# Reuse the accepted, tightly scoped Docker fixture setup/cleanup helpers, without running M2 tests.
fixture_script=SOURCE/'scripts/test_isolation_m2_docker.py'
prefix=fixture_script.read_text().split('\ntry:\n    asyncio.run(main())')[0]
prefix=prefix.replace("('runtime/skills','/skills',True)","('runtime/skills','/skills',False)")
f={'__file__':str(fixture_script),'__name__':'m4_fixture'}
exec(compile(prefix,str(fixture_script),'exec'),f)
PARAMS=f['PARAMS'];WORK=f['WORK'];RUN=f['RUN'];OUT=f['EVIDENCE'];docker=f['docker'];record=f['record']
MODEL=r'''
import json,re,ssl
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
DYNAMIC={}
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  with Path('/capture/requests.jsonl').open('a') as out:out.write(json.dumps(body)+'\n')
  content=json.dumps(body['messages'],ensure_ascii=False)
  names=re.findall('你是「([^」]+)」',content);bot=names[0] if names else 'probe'
  plan=json.loads(Path('/fixture/m4-plan.json').read_text())
  count=sum(m.get('role')=='tool' for m in body['messages'])
  actions=plan.get(bot,[])
  if bot=='M4Chief' and count==2:
   previous=next(m['content'] for m in reversed(body['messages']) if m.get('role')=='tool' and '交付文件：' in str(m.get('content')))
   files=json.loads(previous.split('交付文件：')[-1]);DYNAMIC['files']=files
   actions=[None,None,['delegate',{'agent':'critic','goal':'审核指定版本的报告，使用 verify_artifact。','envelope':{'input_artifact_ids':[x['id'] for x in files]}}]]
  if bot=='M4Critic':
   actions=[['verify_artifact',{'artifact_id':x['id'],'checks':plan['checks'][x['name']]}] for x in DYNAMIC['files']]
  if bot=='M4PiParent' and '子任务 ' in content:actions=[]
  if count<len(actions):
   name,args=actions[count]
   if any(t['function']['name'].startswith('carme_') for t in body.get('tools',[])):name='carme_'+name
   delta={'tool_calls':[{'index':0,'id':'m4call'+str(count),'type':'function','function':{'name':name,'arguments':json.dumps(args)}}]};finish='tool_calls'
  else:delta={'content':'M4 synthetic model finished '+bot};finish='stop'
  chunks=[{'id':'m4fixture','object':'chat.completion.chunk','created':1,'model':body['model'],'choices':[{'index':0,'delta':{'role':'assistant'},'finish_reason':None}]},
   {'id':'m4fixture','object':'chat.completion.chunk','created':1,'model':body['model'],'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
   {'id':'m4fixture','object':'chat.completion.chunk','created':1,'model':body['model'],'choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}]
  data=(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=ThreadingHTTPServer(('0.0.0.0',443),Handler)
ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain('/fixture/cert.pem','/fixture/key.pem');server.socket=ctx.wrap_socket(server.socket,server_side=True);server.serve_forever()
'''
f['MODEL_SERVER']=MODEL
COUNTS=[]
async def main():
    home,broker,admin,control,personal,capture=f['setup']()
    plan={};planpath=RUN/'fixture/m4-plan.json';planpath.write_text('{}')
    agents=yaml.safe_load((home/'config/agents.yaml').read_text());template=agents['agents']['api']
    agents['agents']={}
    for bot,name,engine in [('chief','M4Chief','api'),('engineer','M4Engineer','api'),('critic','M4Critic','api'),('learner','M4Learner','api'),('parent','M4PiParent','pi'),('child','M4PiChild','pi'),('sender','M4Sender','api')]:
        agents['agents'][bot]={**template,'name':name,'engine':engine,'entry':bot=='chief','tools':['files','exec','team','skill','mcp','memory'],'can_delegate':True}
    agents['agents']['engineer']['tools']=agents['agents']['engineer']['tools']+['propose_skill']
    agents['defaults']['max_steps']=20;(home/'config/agents.yaml').write_text(yaml.safe_dump(agents))
    isolation=yaml.safe_load((home/'config/isolation.yaml').read_text());isolation['broker']['max_pi']=1
    isolation['targets']['action']['tools']=['files','exec','team','skill','propose_skill','mcp','memory','read_attachment','create_artifact','verify_artifact']
    (home/'config/isolation.yaml').write_text(yaml.safe_dump(isolation))
    (home/'config/sandbox.yaml').write_text(yaml.safe_dump({'default':'none','limits':{'max_task_seconds':600,'max_tool_calls':32,'max_output_bytes':65536}}))
    broker['max_pi']=1;(home/'broker.json').write_text(json.dumps(broker))
    client=httpx.AsyncClient(base_url=broker['control_url'],headers={'Authorization':'Bearer '+admin},trust_env=False,timeout=120)
    async def request(method,path,body=None,**kwargs):
        response=await client.request(method,'/api'+path,json=body,**kwargs)
        if response.status_code>=400:raise AssertionError(f'{method} {path}: {response.status_code} {response.text[:1000]}')
        return response.json()
    async def ready():
        for _ in range(120):
            try:
                h=await request('GET','/health')
                if h['execution']['broker']=='ready':return
            except (httpx.HTTPError,AssertionError):pass
            await asyncio.sleep(.2)
        raise AssertionError('Control/Broker readiness timeout')
    for _ in range(80):
        try:await request('GET','/health');break
        except (httpx.HTTPError,AssertionError):await asyncio.sleep(.2)
    await request('POST','/config/reload');f['start_broker'](home);await ready()
    def update_plan():planpath.write_text(json.dumps(plan,ensure_ascii=False))
    async def conversation(bot):return (await request('POST','/conversations',{'agent_ids':[bot]}))['conversation']['id']
    async def upload(cid,value):
        return (await request('POST',f'/conversations/{cid}/attachments?name=资料.json',None,content=json.dumps({'value':value}).encode(),headers={'content-type':'application/octet-stream'}))['file']['id']
    async def send(cid,bot,content,**kwargs):return (await request('POST',f'/conversations/{cid}/messages',{'agent_id':bot,'content':content,'request_id':secrets.token_hex(8),**kwargs}))['task_id']
    async def settle(tid):
        end=time.time()+240
        while time.time()<end:
            workers=f['inspect_workers']();counts={role:sum(v['Config']['Labels']['carme.role']==role for v in workers) for role in ('pi','action')}
            # Re-inspect live set because old inspect snapshots intentionally retain evidence.
            ids=docker('ps','-q','--filter','label=carme.instance='+f['INSTANCE'])
            counts={'pi':0,'action':0}
            for cid in ids.splitlines():
                role=json.loads(docker('inspect',cid))[0]['Config']['Labels'].get('carme.role')
                if role in counts:counts[role]+=1
            COUNTS.append(counts);assert counts['pi']<=1 and counts['action']<=1,counts
            approvals=(await request('GET','/approvals'))['approvals']
            for approval in approvals:
                await request('POST','/approvals/'+approval['id']+'/decide',{'approved':True,'note':'explicit synthetic M4 fixture command only'})
            detail=await request('GET','/tasks/'+tid)
            if detail['task']['status'] in {'done','failed','cancelled'}:return detail
            await asyncio.sleep(.2)
        raise AssertionError('task deadline '+tid)
    async def pi_delegation():
        # Genuine Pi parent releases its container before the Pi child starts at capacity one.
        plan['M4PiParent']=[['delegate',{'agent':'child','goal':'在容器写入子任务证明。'}]]
        plan['M4PiChild']=[['write_file',{'path':'/out/child.txt','content':'PI_CHILD_AT_CAPACITY_ONE'}]];update_plan()
        pc=await conversation('parent');pid=await send(pc,'parent','委派 child 完成证明后总结。');pi=await settle(pid)
        assert pi['task']['status']=='done' and len(pi['children'])==1 and pi['children'][0]['status']=='done',pi
        record('CO04_actual_pi_parent_yield_child_capacity_one',{'task':pid,'child':pi['children'][0]['id'],'max_observed':{k:max(c[k] for c in COUNTS) for k in ('pi','action')}})
    if PARAMS.get('native_delegate_only'):
        await pi_delegation()
        (OUT/'inspect.json').write_text(json.dumps(f['INSPECTED'],indent=2))
        (OUT/'capacity.json').write_text(json.dumps(COUNTS))
        await client.aclose()
        return
    async def verify_accept(tid):
        result=await request('POST',f'/tasks/{tid}/verify');assert result['status']=='verified',result
        await request('POST',f'/tasks/{tid}/accept',{'report_hash':result['report_hash']});return result
    def contract(value):return {'expected_outputs':['report.pdf','report.xlsx'],'acceptance_checks':[
        {'output':'report.pdf','check':{'kind':'text_contains','text':str(value)}},
        {'output':'report.xlsx','check':{'kind':'xlsx_cell','sheet':'Sheet','cell':'A1','equals':value}}]}
    generator="""from pathlib import Path
import json
from openpyxl import Workbook
from reportlab.pdfgen import canvas
path=next(Path('/inputs/artifacts').glob('*/*.json'));value=json.loads(path.read_text())['value']
w=Workbook();w.active['A1']=value;w.save('/out/report.xlsx')
c=canvas.Canvas('/out/report.pdf');c.drawString(40,760,'Verified value '+str(value));c.save()
print('ACTUAL_GENERATION',value)
"""
    shell='python -c '+shlex.quote(generator)
    make=[['shell',{'command':shell,'timeout':20}],['create_artifact',{'name':'report.pdf','path':'/out/report.pdf'}],['create_artifact',{'name':'report.xlsx','path':'/out/report.xlsx'}]]
    cid=await conversation('chief');input1=await upload(cid,42)
    badzip=io.BytesIO()
    with zipfile.ZipFile(badzip,'w') as archive:archive.writestr('../escape.txt','synthetic')
    rejected=await client.post(f'/api/conversations/{cid}/attachments?name=unsafe.zip',content=badzip.getvalue(),headers={'content-type':'application/octet-stream'})
    assert rejected.status_code==422,rejected.text
    record('CO03_real_action_rejects_traversal_zip_before_control_archive')
    plan.update(M4Chief=[['list_agents',{}],['delegate',{'agent':'engineer','goal':'读取授权材料，在容器生成 XLSX/PDF。','envelope':{'input_artifact_ids':[input1],**contract(42)}}]],M4Engineer=make,
        checks={'report.pdf':[{'kind':'text_contains','text':'42'}],'report.xlsx':[{'kind':'xlsx_cell','sheet':'Sheet','cell':'A1','equals':42}]})
    update_plan();root=await send(cid,'chief','研究输入，动态发现成员，委派工程生成文件后委派审校。',attachment_ids=[input1]);detail=await settle(root)
    assert detail['task']['status']=='done',detail
    children=detail['children'];assert {c['agent_id'] for c in children}=={'engineer','critic'},children
    source=next(c['id'] for c in children if c['agent_id']=='engineer');critic=next(c['id'] for c in children if c['agent_id']=='critic')
    critique=await request('GET','/tasks/'+critic);assert critique['task']['status']=='done',critique
    assert sum(m['tool_name']=='verify_artifact' and '"passed": true' in m['content'] for m in critique['messages'])==2,critique
    assert (await request('GET','/tasks/'+source))['outcome']['status']=='unverified'
    verified=await verify_accept(source)
    for art in verified['report']['artifacts']:
        raw=(await client.get(f'/api/conversations/{cid}/attachments/{art["id"]}/download')).content
        assert hashlib.sha256(raw).hexdigest()==art['sha256']
    record('AC01_research_dynamic_engineer_critic_artifact_acl_real_pdf_xlsx_user_accept',{'root':root,'source':source,'critic':critic,'report':verified['report']})
    assert (await client.delete(f'/api/conversations/{cid}/attachments/{input1}')).status_code==409
    proposal={'source_task_id':source,'name':'M4 reports','document':'---\nname: M4 reports\nconstraints: ["Read all input and verify output before claiming success"]\n---\nUse scripts/generate.py. PrivateName sk-FAKE123456789\n'+'Instruction.\n'*1200+'TAIL_MUST_READ',
        'private_literals':['PrivateName'],'files':{'scripts/generate.py':generator}}
    plan['M4Engineer']=[['propose_skill',proposal]];update_plan()
    proposal_task=await send(await conversation('engineer'),'engineer','从已认可的任务提出脱敏候选，等待人工审阅。')
    proposed=await settle(proposal_task)
    candidate=json.loads(next(m['content'] for m in proposed['messages'] if m['tool_name']=='propose_skill'))
    review=await request('GET','/skill-candidates/'+candidate['id']+'/review')
    assert all(secret not in json.dumps(review['files']) for secret in ('PrivateName','sk-FAKE123456789'))
    record('SK03_bot_proposes_redacted_candidate_without_publication',{'task':proposal_task,'candidate':candidate['id']})
    sid,rev=candidate['skill_id'],candidate['revision']
    denied=await client.post('/api/skill-grants',json={'bot_id':'learner','skill_id':sid,'revision':rev});assert denied.status_code==422
    input2=await upload(cid,43)
    use=[['use_skill',{'name':sid}]]
    # Full body reading via stable cursor; fixed bundle is present read-only before execution.
    use += [['use_skill',{'name':sid,'cursor':f'{rev}:{offset}'}] for offset in (4500,9000,13500)]
    runbundle=[['shell',{'command':'python /inputs/skills/'+sid+'/'+rev+'/scripts/generate.py && test ! -w /inputs/skills/'+sid+'/'+rev+'/scripts/generate.py','timeout':20}],*make[1:]]
    plan['M4Learner']=use+runbundle;update_plan()
    # Separate learner conversation explicitly receives its own new input, not cross-conversation IDs.
    lc=await conversation('learner');input2=await upload(lc,43)
    body={'conversation_id':lc,'agent_id':'learner','goal':'使用候选 '+sid+' 处理新输入并生成两份报告。','envelope':{'input_artifact_ids':[input2],**contract(43)}}
    badbody={**body,'envelope':{'input_artifact_ids':[input2],**contract(999)}}
    failed_test=(await request('POST',f'/skill-candidates/{candidate['id']}/test',badbody))['task_id']
    assert (await settle(failed_test))['task']['status']=='done'
    failed_outcome=await request('POST',f'/tasks/{failed_test}/verify');assert failed_outcome['status']=='failed',failed_outcome
    reject=await client.post(f'/api/skill-candidates/{candidate['id']}/publish',json={'test_task_id':failed_test,'bot_ids':['learner'],'revision':rev,'privacy_reviewed':True})
    assert reject.status_code==422,reject.text
    record('SK04_actual_new_input_failure_cannot_publish',{'test_task':failed_test})
    tid=(await request('POST',f'/skill-candidates/{candidate["id"]}/test',body))['task_id'];tested=await settle(tid)
    assert tested['task']['status']=='done',tested
    assert 'TAIL_MUST_READ' in json.dumps(tested),tested
    await verify_accept(tid)
    published=await request('POST',f'/skill-candidates/{candidate["id"]}/publish',{'test_task_id':tid,'bot_ids':['learner'],'revision':rev,'privacy_reviewed':True});assert published['status']=='published'
    lc2=await conversation('learner');input3=await upload(lc2,44)
    reused=await send(lc2,'learner','使用已授权 '+sid+' 处理新输入。',attachment_ids=[input3],envelope=contract(44));reused_detail=await settle(reused)
    assert reused_detail['task']['status']=='done',reused_detail
    await verify_accept(reused)
    record('SK01_04_AC01_candidate_redaction_actual_new_input_test_publish_reuse',{'candidate':candidate['id'],'revision':rev,'test_task':tid,'reuse_task':reused})
    # A later installed revision never expands the old grant. Admin selects a concrete version and can roll back.
    skill_path=home/'runtime/skills'/sid/'SKILL.md';skill_path.write_text(skill_path.read_text()+'\nV2_REGRESSION_FIXTURE\n')
    v2=(await request('POST',f'/skills/{sid}/snapshot'))['revision'];assert v2!=rev
    for revision in (v2,rev):await request('POST','/skill-grants',{'bot_id':'learner','skill_id':sid,'revision':revision})
    assert (await request('GET','/learning'))['skill_grants']['learner'][sid]==rev
    record('SK05_bot_fixed_version_update_and_rollback',{'v1':rev,'v2':v2})
    await pi_delegation()
    # Third-party stdio lives in Action. Catalog pagination >200, precise per-Bot grants.
    mcp_source='''import json,sys,time,os
from pathlib import Path
def visible(path):
 try:return Path(path).exists()
 except OSError:return False
def send(i,result):print(json.dumps({'jsonrpc':'2.0','id':i,'result':result}),flush=True)
schema={'type':'object','properties':{'resource':{'type':'string'}},'required':['resource']}
for line in sys.stdin:
 m=json.loads(line);i=m.get('id');method=m.get('method');p=m.get('params',{})
 if i is None:continue
 if method=='initialize':send(i,{'protocolVersion':'2024-11-05','serverInfo':{'name':'m4-synthetic-stdio','version':'1'},'capabilities':{'tools':{}}})
 elif method=='tools/list':
  tools=[{'name':'filler'+str(n),'inputSchema':{'type':'object','properties':{}}} for n in range(200)] if not p.get('cursor') else [{'name':'probe','inputSchema':schema},{'name':'send','inputSchema':schema}]
  send(i,{'tools':tools,**({'nextCursor':'tail'} if not p.get('cursor') else {})})
 elif method=='tools/call':
  if p['name']=='probe':
   paths=['/control-state/carme.db','/run/secrets/control-token','/run/secrets/provider-key','/var/run/docker.sock','/Users/you/.pi','/root/.pi']
   result={'uid':os.getuid(),'exists':{k:visible(k) for k in paths},'env':sorted(os.environ),'identity':os.environ.get('MCP_FIXTURE_ID')}
   send(i,{'content':[{'type':'text','text':json.dumps(result)}]})
  elif p['name']=='send':
   with Path('/out/send-receipts.jsonl').open('a') as out:out.write(json.dumps({'external_id':'fixture-effect-1','resource':p['arguments']['resource']})+'\\n')
   time.sleep(90);send(i,{'content':[{'type':'text','text':'sent'}]})
'''
    encoded_source=base64.b64encode(mcp_source.encode()).decode()
    args=['-c',"exec(__import__('base64').b64decode(''.join(__import__('sys').argv[1:])))"]+[encoded_source[i:i+390] for i in range(0,len(encoded_source),390)]
    added=await request('POST','/mcp/servers',{'id':'isolated','executor':'action','command':'python','args':args,'env_text':'MCP_FIXTURE_ID=ACCOUNT_FIXTURE_A','timeout':120})
    assert added['server']['status']=='connected' and added['server']['tool_count']==202,added
    await request('POST','/mcp-grants',{'bot_id':'sender','server_id':'isolated','remote':'probe','argument_allowlist':{'resource':['approved-resource']}})
    plan['M4Sender']=[['mcp__isolated__probe',{'resource':'approved-resource'}]];update_plan()
    sc=await conversation('sender');probe=await send(sc,'sender','核验隔离 MCP。');checked=await settle(probe)
    assert checked['task']['status']=='done',checked
    probe_text=next(m['content'] for m in checked['messages'] if m['tool_name']=='mcp__isolated__probe')
    assert probe_text.startswith('{'),probe_text
    result=json.loads(probe_text)
    assert result['uid']==1000 and not any(result['exists'].values()) and result['identity']=='ACCOUNT_FIXTURE_A',result
    assert not {'CARME_TOKEN','OPENAI_API_KEY','DOCKER_HOST'} & set(result['env']),result
    record('MC01_MC02_real_isolated_stdio_202_tools_no_control_or_personal_profile',{'task':probe,'catalog_count':202,'probe':result})
    plan['M4Engineer']=[['mcp__isolated__probe',{'resource':'approved-resource'}]];update_plan()
    ec=await conversation('engineer');deniedtask=await send(ec,'engineer','未授权 Bot 测试。');denied=await settle(deniedtask)
    assert 'capability_denied' in json.dumps(denied),denied
    plan['M4Sender']=[['mcp__isolated__probe',{'resource':'unapproved-resource'}]];update_plan()
    dc=await conversation('sender');denied=await settle(await send(dc,'sender','未授权资源测试。'))
    assert 'mcp_resource_denied' in json.dumps(denied),denied
    record('MC03_bot_identity_and_resource_grant_denied')
    # Simulated external receiver records one effect; Control loses the reply and is recreated.
    await request('POST','/mcp-grants',{'bot_id':'sender','server_id':'isolated','remote':'send','argument_allowlist':{'resource':['fixture-channel']}})
    await request('POST','/mcp-grants',{'bot_id':'chief','server_id':'isolated','remote':'send','argument_allowlist':{'resource':['fixture-channel']}})
    plan['M4Sender']=[['mcp__isolated__send',{'resource':'fixture-channel'}]]
    plan['M4Chief']=[['delegate',{'agent':'sender','goal':'仅向合成接收端执行一次；断线后人工对账。'}]];update_plan()
    sc2=await conversation('chief');interrupted=await send(sc2,'chief','委派 sender 一次合成发送，重启后不能重复子任务。')
    child_id='';ledger=None
    for _ in range(200):
        detail=await request('GET','/tasks/'+interrupted)
        if detail['children']:
            child_id=detail['children'][0]['id'];ledger=home/'runtime/runs'/child_id/'out/send-receipts.jsonl'
        if ledger and ledger.exists() and ledger.stat().st_size:break
        await asyncio.sleep(.1)
    else:raise AssertionError('synthetic receiver did not record effect')
    docker('stop','--time','10',control)
    f['BROKER'].send_signal(signal.SIGTERM);f['BROKER'].wait(timeout=25)
    docker('rm',control);docker(*f['CONTROL_LAUNCH']);docker('network','connect',f['CONTROL_NETWORK'],control)
    f['start_broker'](home);await ready()
    detail=await request('GET','/tasks/'+interrupted)
    pending=[o for o in detail['operations'] if o['status']=='pending'];assert len(pending)==1,detail
    blocked=await client.post('/api/tasks/'+interrupted+'/resume');assert blocked.status_code==409 and 'reconciliation_required' in blocked.text,blocked.text
    receipt=json.loads(ledger.read_text().splitlines()[0])
    child=await request('GET','/tasks/'+child_id);child_pending=[o for o in child['operations'] if o['status']=='pending'];assert len(child_pending)==1,child
    await request('POST','/tasks/'+child_id+'/reconcile',{'operation_id':child_pending[0]['id'],'effect':'confirmed','receipt':receipt})
    await request('POST','/tasks/'+interrupted+'/reconcile',{'operation_id':pending[0]['id'],'effect':'confirmed','receipt':{'child_id':child_id,'external_receipt':receipt}})
    # If recovery regenerated the interrupted response, this changed intent would
    # create a second child. The exact checkpointed call must finish first.
    plan['M4Chief']=[['delegate',{'agent':'sender','goal':'CHANGED MODEL GOAL MUST NOT REISSUE THE EFFECT'}]];update_plan()
    await request('POST','/tasks/'+interrupted+'/resume');recovered=await settle(interrupted)
    assert recovered['task']['status']=='done' and len(recovered['runs'])==2,recovered
    assert len(ledger.read_text().splitlines())==1
    assert len(recovered['children'])==1 and recovered['children'][0]['id']==child_id
    record('CO05_control_recreate_durable_checkpoint_reconcile_no_duplicate_effect_or_child',{'task':interrupted,'child':child_id,'attempts':len(recovered['runs']),'effects':1,'child_count':1,'changed_model_intent_not_reissued':True,'receipt':receipt})
    # Fresh disposable Chromium context; no personal browser profile or external page.
    from playwright.async_api import async_playwright
    async with async_playwright() as browser_api:
        browser=await browser_api.chromium.launch(headless=True,args=['--no-proxy-server'])
        context=await browser.new_context(viewport={'width':1280,'height':900})
        login=await context.request.post(broker['control_url']+'/api/session',headers={'Authorization':'Bearer '+admin,'Origin':broker['control_url']})
        assert login.status==200,await login.text()
        page=await context.new_page();page_errors=[];page.on('pageerror',lambda error:page_errors.append(str(error)))
        await page.goto(broker['control_url'],wait_until='domcontentloaded')
        await page.get_by_role('button',name='搜索',exact=True).click()
        await page.get_by_role('option',name='插件').click()
        await page.get_by_role('tab',name='已安装的 Skill',exact=True).click()
        await page.get_by_text('版本授权与 Skill 学习',exact=True).click()
        await page.get_by_label('候选',exact=True).select_option(candidate['id'])
        await page.get_by_role('button',name='审阅完整候选与脚本',exact=True).click()
        await page.get_by_text('SKILL.md',exact=True).click()
        await page.get_by_text('TAIL_MUST_READ',exact=False).last.wait_for(state='visible')
        await page.screenshot(path=str(OUT/'m4-skill-review.png'),full_page=True)
        await page.get_by_role('tab',name='已安装的 MCP',exact=True).click()
        await page.get_by_text('按 Bot 授权工具与资源',exact=True).click()
        await page.get_by_label('资源参数允许值').wait_for(state='visible')
        assert not page_errors,page_errors
        await context.close();await browser.close()
    record('UI_real_chromium_skill_complete_review_and_mcp_grant_controls')
    (OUT/'inspect.json').write_text(json.dumps(f['INSPECTED'],indent=2));(OUT/'capacity.json').write_text(json.dumps(COUNTS))
    assert f['PERSONAL_PROCESS'].poll() is None and json.loads(docker('inspect',personal))[0]['State']['Running']
    await client.aclose()

try:asyncio.run(main())
except BaseException as exc:
    f['RESULTS'].append({'id':'m4_integration','status':'fail','reason':type(exc).__name__+':'+str(exc)[:4000]})
    (OUT/'docker-results.json').write_text(json.dumps({'instance':f['INSTANCE'],'run':str(RUN),'tests':f['RESULTS']},indent=2));raise
finally:
    p=f['PERSONAL_PROCESS']
    if p and p.poll() is None:p.terminate();p.wait(timeout=5)
    p=f['BROKER']
    if p and p.poll() is None:
        p.send_signal(signal.SIGTERM)
        try:p.wait(timeout=20)
        except Exception:os.killpg(p.pid,signal.SIGTERM);p.wait(timeout=10)
    for cid in docker('ps','-aq','--filter','label=carme.instance='+f['INSTANCE'],check=False).splitlines():
        if json.loads(docker('inspect',cid))[0]['Config']['Labels'].get('carme.instance')==f['INSTANCE']:docker('rm','-f',cid,check=False)
    for name in reversed(f['NAMES']):
        value=docker('inspect',name,check=False)
        if value and json.loads(value)[0]['Config']['Labels'].get('carme.fixture')==f['INSTANCE']:docker('rm','-f',name,check=False)
    for network in f['NETWORKS']:docker('network','rm',network,check=False)
    (OUT/'cleanup.json').write_text(json.dumps({'instance':f['INSTANCE'],'remaining_workers':docker('ps','-aq','--filter','label=carme.instance='+f['INSTANCE']),'no_global_cleanup':True}))
