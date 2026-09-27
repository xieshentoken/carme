"""Real Docker/Chrome/AT-SPI acceptance with synthetic Control identities.

Only explicitly supplied test account disks are touched. No model calls or
personal profiles. Run after provisioning the disposable account disk.
"""
import asyncio, base64, copy, json, os, secrets, sys, time
from pathlib import Path
from unittest.mock import patch
import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_isolation_m1 as m1
from carme.approval import ApprovalOutcome
from carme.broker import Broker
from carme.docker_desktop import stop_desktop
from carme.docker_browser import fetch_public as real_fetch
from carme.execution import build_execution_router
from carme.tools.base import ToolContext

params = json.loads(Path(sys.argv[1]).read_text())
out = Path(params['evidence']); out.mkdir(parents=True, exist_ok=True)
checks=[]
submitted=[]
def record(name, **data):
    checks.append({'name':name, 'status':'pass', **data})
    (out/'live-results.json').write_text(json.dumps({'checks':checks, 'count':len(checks)},indent=2))
    print('PASS',name,flush=True)
HTML='''<!doctype html><title>Carme desktop fixture</title><h1>Independent bot desktop</h1><label>Fixture input<input aria-label="Fixture input" id="input"></label><p id="cookie"></p><script>document.getElementById('cookie').textContent=document.cookie||'NO_COOKIE'</script>'''
async def fixture_fetch(data,safety,check,**kw):
    if data.get('url','').startswith('https://fixture.invalid/'):
        check()
        from carme.docker_browser import is_submission
        if is_submission(data):
            if kw.get('authorize'): await kw['authorize']()
            check(); submitted.append({'method':data['method'],'url':data['url']})
            return {'status':200,'headers':[['content-type','text/plain']],'body':base64.b64encode(b'SUBMITTED').decode()}
        headers=[['content-type','text/html']]
        if data['url'].endswith('/alice'): headers.append(['set-cookie','carme_fixture=ALICE_ONLY; Path=/; Max-Age=3600; Secure; SameSite=Lax'])
        return {'status':200,'headers':headers,'body':base64.b64encode(HTML.encode()).decode()}
    return await real_fetch(data,safety,check,**kw)

async def main():
    f=m1.M1(); await f.asyncSetUp(); f.root=f.root.resolve()
    home=Path(params['account']); identity=json.loads((home/'account.json').read_text())['instance_id']
    key=home/'broker-key'; key.write_text(secrets.token_hex(32)); key.chmod(0o600)
    cfg=f.config; cfg.browser.enabled=True
    for bot in ('bot','other'):
        spec=copy.deepcopy(cfg.agents.get('bot')); spec.id=bot;spec.name='Test '+bot
        spec.execution_target='container';spec.execution_target_id='action';spec.tools=['files','exec','browser','computer','desktop','skill']
        cfg.agents.agents[bot]=spec
    cfg.isolation={'broker':{'key_file':str(key),'instance_id':identity},'desktop':{'version':1,'image_digest':params['images']['desktop']},
        'targets':{'action':{'image_digest':params['images']['action'],'tools':['files','exec','browser','computer','desktop','skill']}}}
    app=f.app(); app.include_router(build_execution_router(f.runtime.execution))
    broker=Broker({'home':str(home),'instance_id':identity,'key_file':str(key),'control_url':'http://127.0.0.1:9999',
        'docker_binary':params['docker'],'docker_context':params['docker_context'],'docker_config':params['docker_config'],
        'images':params['images'],'targets':['action'],'desktop':True,'max_desktop':2})
    await broker.client.aclose(); broker.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:9999')
    task=asyncio.create_task(broker.serve())
    approvals=[]; consent={'approved':True}
    async def context(bot):
        _,meta=f.runtime._task_agent_snapshot(bot);cid=f.store.create_conversation([bot])['id']
        tid=f.store.create_task(bot,'desktop fixture',meta=meta,conversation_id=cid);f.store.set_task_status(tid,'running')
        async def approval(**kwargs):
            approvals.append(kwargs['kind']);return ApprovalOutcome(consent['approved'],'synthetic test only')
        return ToolContext(cfg.agents.get(bot),tid,f.store,approve=approval,browser_manager=f.runtime.browsers,
            extras={'policy':meta['policy'],'check_policy':lambda:f.runtime.check_task_policy(tid)})
    async def op(bot,name,args=None,ctx=None):
        value=await f.runtime.execution.desktop_request(bot,name,args or {},ctx=ctx)
        (out/'last-operation.json').write_text(json.dumps({'op':name,'result':value},ensure_ascii=False,indent=2))
        return value
    try:
        for _ in range(100):
            if f.runtime.execution.health()['broker']=='ready':break
            if task.done():task.result()
            await asyncio.sleep(.1)
        else:raise RuntimeError('broker_timeout')
        ca=await context('bot');cb=await context('other')
        with patch('carme.docker_browser.fetch_public',side_effect=fixture_fetch):
            status=await op('bot','status');assert status['mode']=='docker' and status['available'],status
            record('desktop_starts',free_bytes=status['free_bytes'])
            image=await op('bot','screenshot');(out/'desktop.jpg').write_bytes(base64.b64decode(image['body']))
            assert (out/'desktop.jpg').stat().st_size>5000
            record('real_desktop_screenshot')
            version=await op('bot','shell',{'command':'/software/chrome/chrome --version'},ca)
            assert '153.0.8010.52' in version['text'],version
            record('official_chrome_arm64',version=version['text'].strip())
            roots=await op('bot','find_roots',{},ca)
            (out/'roots.json').write_text(json.dumps(roots,ensure_ascii=False,indent=2))
            assert not roots.get('isError'),roots
            record('upstream_pi_computer_use_native_helper',result=roots)
            first=await f.runtime.execution.browser_tool(ca,'web_open',{'url':'https://fixture.invalid/alice'})
            assert 'Independent bot desktop' in first,first
            record('browser_tools_use_same_desktop')
            roots=await op('bot','find_roots',{'text':'Carme desktop fixture'},ca)
            root=next(w['windowRef'] for w in roots['details']['windows'] if w['kind']=='browser_page')
            observed=await op('bot','observe_ui',{'root':root,'mode':'semantic'},ca)
            assert not observed.get('isError') and observed.get('details',{}).get('stateId'),observed
            (out/'observed.json').write_text(json.dumps(observed,ensure_ascii=False,indent=2))
            record('computer_use_observes_live_chrome_dom')
            import re
            found=await op('bot','search_ui',{'stateId':observed['details']['stateId'],'text':'Fixture input','role':'textbox'},ca)
            text='\n'.join(x.get('text','') for x in found['content'])
            ref=re.search(r'(@e\d+).*Fixture input',text)
            assert ref,text
            acted=await op('bot','act_ui',{'stateId':observed['details']['stateId'],'actions':[{'action':'setText','ref':ref.group(1),'text':'UPSTREAM_ACTION_OK'}]},ca)
            assert not acted.get('isError'),acted
            snapshot=await f.runtime.execution.browser_tool(ca,'web_snapshot',{})
            assert '已填写' in snapshot and 'UPSTREAM_ACTION_OK' in json.dumps(acted), (snapshot,acted)
            record('upstream_computer_use_controls_live_browser')
            # A shell can drive this Chrome through CDP. Its outward submission still
            # reaches Control before any bytes leave, independent of the shell approval.
            command = """python - <<'PYCODE'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser=p.chromium.connect_over_cdp('http://127.0.0.1:9222')
    page=next(page for context in browser.contexts for page in context.pages if page.url.startswith('https://fixture.invalid/'))
    page.evaluate("() => {window.carmeFixtureSend='WAIT'; fetch('https://fixture.invalid/api/messages', {method:'POST',body:'SYNTHETIC_MESSAGE'}).then(r=>r.text()).then(t=>window.carmeFixtureSend=t).catch(()=>window.carmeFixtureSend='BLOCKED')}")
    page.wait_for_function("window.carmeFixtureSend !== 'WAIT'", timeout=15000)
    print(page.evaluate('window.carmeFixtureSend'))
PYCODE"""
            consent['approved']=False
            denied=await f.runtime.registry.execute(ca,'bot_computer',{'operation':'shell','arguments':{'command':command}})
            assert not submitted and approvals==['external_submission'] and 'BLOCKED' in denied,(denied,approvals,submitted)
            record('shell_driven_browser_submission_rejected_before_send')
            consent['approved']=True
            allowed=await f.runtime.registry.execute(ca,'bot_computer',{'operation':'shell','arguments':{'command':command}})
            assert len(submitted)==1 and approvals==['external_submission']*2 and 'SUBMITTED' in allowed,(allowed,approvals,submitted)
            record('one_approval_for_outbound_request_no_shell_approval')
            # Installing and using a Skill stages its real files into the Action
            # container; no personal account or model provider is involved.
            from carme.attachments import archive_binary
            document=b'---\nname: Linux autonomy fixture\ndescription: Synthetic acceptance\n---\n\nRead this supplied fixture.\n'
            cid=f.store.get_task(ca.task_id)['conversation_id']
            import io, zipfile, shlex
            bundle=io.BytesIO()
            with zipfile.ZipFile(bundle,'w') as archive:
                archive.writestr('fixture/SKILL.md',document)
                archive.writestr('fixture/scripts/run.py','print("SKILL_SCRIPT_OK")')
            attachment=archive_binary(f.store,cid,'fixture.zip',bundle.getvalue(),task_id=ca.task_id)
            mid=f.store.add_conversation_message(cid,'bot','user','Install synthetic fixture',task_id=ca.task_id)
            f.store._write('UPDATE attachments SET message_id=? WHERE id=?',(mid,attachment['id']))
            installed=json.loads(await f.runtime.registry.execute(ca,'install_skill',{'source':'attachment','value':attachment['id']}))
            content=json.loads(await f.runtime.registry.execute(ca,'use_skill',{'name':installed['skill_id']}))
            assert content['complete'] and 'Read this supplied fixture' in json.dumps(content),content
            executed=await f.runtime.registry.execute(ca,'bot_computer',{'operation':'shell','arguments':{
                'command':'python '+shlex.quote(content['computer_directory']+'/scripts/run.py')}})
            assert 'SKILL_SCRIPT_OK' in executed and approvals==['external_submission']*2,executed
            record('registered_skill_script_runs_in_bot_linux_at_returned_path')
            f.runtime.check_task_policy(cb.task_id)
            catalog=json.loads(await f.runtime.registry.execute(cb,'list_skills',{}))
            assert [v['id'] for v in catalog['skills']]==[installed['skill_id']],catalog
            shared_content=json.loads(await f.runtime.registry.execute(cb,'use_skill',{'name':installed['skill_id']}))
            assert shared_content['revision']==content['revision'] and shared_content['computer_directory']!=content['computer_directory']
            shared_run=await f.runtime.registry.execute(cb,'bot_computer',{'operation':'shell','arguments':{
                'command':'python '+shlex.quote(shared_content['computer_directory']+'/scripts/run.py')}})
            assert 'SKILL_SCRIPT_OK' in shared_run,shared_run
            record('second_running_bot_uses_shared_skill_and_script_without_install')
            removed=json.loads(await f.runtime.registry.execute(ca,'remove_skill',{'name':installed['skill_id']}))
            assert removed['status']=='removed' and approvals==['external_submission']*2,removed
            assert not removed['package_uninstalled']
            f.runtime.check_task_policy(cb.task_id)
            assert installed['skill_id'] in f.runtime.skills.effective_grants('other')
            record('skill_install_use_and_remove_without_approval_in_real_container')
            other=await f.runtime.execution.browser_tool(cb,'web_open',{'url':'https://fixture.invalid/other'})
            assert 'NO_COOKIE' in other and 'ALICE_ONLY' not in other,other
            record('separate_bot_browser_cookies')
            value=await op('bot','shell',{'command':"mkdir -p fixture-software; printf '#include <stdio.h>\\nint main(){puts(\"SHARED_OK\");}\\n' > hello.c; cc hello.c -o fixture-software/hello; printf BOT_ONLY > private.txt"},ca)
            shared=await op('bot','publish',{'name':'fixture','version':secrets.token_hex(3),'path':'fixture-software'},ca)
            value=await op('other','shell',{'command':shared['path']+"/hello; test ! -e /home/bot/private.txt; test ! -e /var/run/docker.sock; test ! -e /run/secrets/broker-key"},cb)
            assert 'SHARED_OK' in value['text'],value
            record('same_account_shared_software_private_homes')
            ctl=await f.runtime.execution.set_desktop_control('bot',True)
            try:await op('bot','find_roots',{},ca);raise AssertionError('bot was allowed during external control')
            except RuntimeError as exc:assert 'external_control' in str(exc)
            try:await f.runtime.execution.desktop_request('bot','mouse',{'action':'click'},control_id='wrong');raise AssertionError('wrong token accepted')
            except RuntimeError as exc:assert 'external_control' in str(exc)
            await f.runtime.execution.desktop_request('bot','mouse',{'action':'move','x':200,'y':220},control_id=ctl['control_id'])
            moved=await f.runtime.execution.desktop_request('bot','mouse',{'action':'move_rel','dx':25,'dy':30},control_id=ctl['control_id'])
            assert (moved['x'],moved['y'])==(225,250),moved
            await f.runtime.execution.set_desktop_control('bot',False,ctl['control_id'])
            try:await f.runtime.execution.desktop_request('bot','keyboard',{'text':'denied'},control_id=ctl['control_id']);raise AssertionError('old token accepted')
            except RuntimeError:pass
            record('external_control_token_and_relative_mouse')
            import statistics
            samples=[]
            ctl=await f.runtime.execution.set_desktop_control('bot',True)
            for index in range(40):
                start=time.perf_counter()
                await asyncio.gather(
                    f.runtime.execution.desktop_request('bot','mouse',{'action':'move','x':200+index,'y':250},control_id=ctl['control_id']),
                    f.runtime.execution.desktop_request('bot','screenshot',control_id=ctl['control_id']))
                samples.append((time.perf_counter()-start)*1000)
            await f.runtime.execution.set_desktop_control('bot',False,ctl['control_id'])
            start=time.perf_counter()
            await asyncio.gather(*(op('bot','status') for _ in range(8)))
            record('warm_desktop_latency',mouse_and_frame_p50_ms=round(statistics.median(samples),1),
                   mouse_and_frame_p95_ms=round(sorted(samples)[37],1),
                   eight_status_ms=round((time.perf_counter()-start)*1000,1))
            await op('bot','shell',{'command':"printf ORIGINAL > notes.txt; mousepad /home/bot/notes.txt >/tmp/mousepad.log 2>&1 &"},ca)
            await asyncio.sleep(1)
            native=await op('bot','find_roots',{'text':'notes.txt'},ca)
            roots=native.get('details',{}).get('windows',[])
            assert roots,native
            observed=await op('bot','observe_ui',{'root':roots[0]['windowRef'],'mode':'semantic'},ca)
            assert not observed.get('isError') and 'ORIGINAL' in json.dumps(observed),observed
            record('computer_use_reads_native_linux_editor')
            await op('bot','shell',{'command':'xdotool search --onlyvisible --class mousepad windowactivate --sync'},ca)
            ctl=await f.runtime.execution.set_desktop_control('bot',True)
            for args in ({'keys':'ctrl+a'},{'text':'手机键盘 mobile keyboard'},{'keys':'ctrl+s'}):
                await f.runtime.execution.desktop_request('bot','keyboard',args,control_id=ctl['control_id'])
            await f.runtime.execution.set_desktop_control('bot',False,ctl['control_id'])
            content=await op('bot','shell',{'command':'cat notes.txt'},ca)
            assert content['text']=='手机键盘 mobile keyboard',content
            record('external_unicode_keyboard_saves_native_file')
            await stop_desktop(broker,'bot')
            persistent=await f.runtime.execution.browser_tool(ca,'web_open',{'url':'https://fixture.invalid/persisted'})
            assert 'ALICE_ONLY' in persistent,persistent
            record('browser_login_persists_after_container_recreate')
            public=await f.runtime.execution.browser_tool(ca,'web_open',{'url':'https://example.com'})
            assert 'Example Domain' in public,public
            record('real_public_https')
            downloaded=await op('bot','fetch',{'url':'https://www.python.org/ftp/python/3.13.12/Python-3.13.12.tar.xz','name':'python-source-'+secrets.token_hex(4)+'.tar.xz'},ca)
            assert downloaded['bytes']>4*1024*1024,downloaded
            await op('bot','shell',{'command':"rm -- "+downloaded['path']},ca)
            record('streamed_public_software_archive',bytes=downloaded['bytes'],sha256=downloaded['sha256'])
            for bot_id,entry in broker.desktops.items():
                item=json.loads(await broker.docker('inspect',entry['name']))[0];host=item['HostConfig']
                assert host['NetworkMode']=='none' and host['ReadonlyRootfs'] and not host['Privileged']
                assert len(item['Mounts'])==3 and all('/storage/mount/' in m['Source'] for m in item['Mounts'])
            record('containers_have_only_private_home_and_shared_readonly_software')
            if params.get('ui_port'):
                os.environ['CARME_DEV_NO_AUTH']='1'
                import uvicorn
                from fastapi.staticfiles import StaticFiles
                app.mount('/',StaticFiles(directory=params.get('web_dir',str(Path(__file__).resolve().parents[1]/'web/dist')),html=True),name='web')
                server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=params['ui_port'],log_level='warning',access_log=False))
                print('UI_READY',params['ui_port'],flush=True)
                await server.serve()
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
        await f.asyncTearDown()

asyncio.run(main())
