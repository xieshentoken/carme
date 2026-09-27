"""Security boundaries for the bot desktop; no host desktop or Docker calls."""
import asyncio, copy, json, os, sys, tempfile, time, unittest
import httpx
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_isolation_m1 as m1
from carme.account_disk import checked, GIB
from carme.docker_desktop import validate, publish_software
from carme.engines import BRIDGE_TOOL_NAMES

class Desktop(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=m1.M1.asyncSetUp
    asyncTearDown=m1.M1.asyncTearDown
    app=m1.M1.app
    def configure(self):
        spec=self.config.agents.get('bot');spec.tools=['desktop','skill'];spec.execution_target='container';spec.execution_target_id='action'
        self.config.isolation={'desktop':{'version':1},'targets':{'action':{'tools':['desktop','skill']}}}
        self.runtime.execution.broker_seen=time.time()
        return self.runtime.execution
    async def test_scope_missing_mount_and_broker_fail_closed(self):
        e=self.configure()
        for bot in ('../main','missing',''):
            with self.assertRaises(ValueError):await e.desktop_request(bot,'status')
        e.broker_seen=0
        with self.assertRaisesRegex(RuntimeError,'broker_unavailable'):await e.desktop_request('bot','status')
        self.assertFalse(e.desktop_requests)
        home=self.root.resolve();(home/'storage/mount').mkdir(parents=True)
        (home/'account.json').write_text(json.dumps({'instance_id':'fixture'}))
        value={'instance_id':'fixture','mount':str(home/'storage/mount'),'image':str(home/'storage/account.sparseimage'),'usable_before_chrome':2*GIB}
        (home/'storage/disk.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError,'not_mounted'):checked(home)
    async def test_external_control_tokens_revocation_and_expiry(self):
        e=self.configure()
        with patch.object(e,'desktop_request',AsyncMock(return_value={'ok':True})):
            first=await e.set_desktop_control('bot',True)
            with self.assertRaises(RuntimeError):await e.set_desktop_control('bot',True,'other-page')
            with self.assertRaises(RuntimeError):await e.set_desktop_control('bot',False,'other-page')
            control=e.desktop_control('bot')
            request={'bot_id':'bot','future':asyncio.get_running_loop().create_future(),'deadline':time.time()+10,'epoch':control['epoch'],
                'ctx':None,'operation':'mouse','control_id':first['control_id']}
            e.check_desktop_request(request)
            await e.set_desktop_control('bot',False,first['control_id'])
            with self.assertRaisesRegex(RuntimeError,'control_changed'):e.check_desktop_request(request)
            await e.set_desktop_control('bot',True)
            e.desktop_controls['bot']['expires']=0
            self.assertFalse(e.desktop_control('bot')['enabled'])
    async def test_api_pi_capabilities_and_policy_version(self):
        self.configure();before=self.runtime.policy_for('bot',target='container',node={})
        self.assertIn('bot_computer',before['tools']);self.assertIn('bot_computer',BRIDGE_TOOL_NAMES)
        self.config.agents.get('bot').engine='pi'
        self.assertEqual(before['tools'],self.runtime.policy_for('bot',target='container',node={})['tools'])
        self.assertNotIn('bot_computer',self.runtime.policy_for('bot',target='macos',node={})['tools'])
        self.config.isolation['desktop']['version']=2
        self.assertNotEqual(before['permission_version'],self.runtime.policy_for('bot',target='container',node={})['permission_version'])
    async def test_validation_rejects_identity_and_invalid_input(self):
        for op,args in [('shell',{'command':'true','env':{}}),('mouse',{'action':'move','x':float('nan')}),('keyboard',{'keys':'a','text':'b'}),('screenshot',{'bot_id':'other'}),('install',{})]:
            with self.assertRaises(ValueError):validate(op,args)

    async def test_desktop_browser_needs_no_legacy_image_and_keeps_policy_grants(self):
        self.configure()
        self.config.isolation['desktop']['image_digest']='sha256:'+'d'*64
        self.config.agents.get('bot').tools.append('computer')
        self.config.isolation['targets']['action']['tools'].append('computer')
        self.config.browser.enabled=True
        policy=self.runtime.policy_for('bot',target='container',node={})
        self.assertIn('web_open',policy['tools'])
        self.assertEqual(policy['web_execution'],'bot-desktop')
        from carme.browser.session import BrowserError
        with self.assertRaises(BrowserError):
            await self.runtime.browsers._get_session('default')
        self.assertNotIn('web_open',self.runtime.policy_for('bot',target='macos',node={})['tools'])
        self.config.browser.enabled=False
        disabled=self.runtime.policy_for('bot',target='container',node={})
        self.assertNotIn('web_open',disabled['tools'])
        self.assertNotEqual(policy['permission_version'],disabled['permission_version'])

    async def test_duplicate_reads_share_work_and_cancellation_is_scoped(self):
        e=self.configure()
        first=asyncio.create_task(e.desktop_request('bot','status'))
        second=asyncio.create_task(e.desktop_request('bot','status'))
        await asyncio.sleep(.01)
        self.assertEqual(len(e.desktop_requests),1)
        first.cancel();await asyncio.gather(first,return_exceptions=True)
        request=next(iter(e.desktop_requests.values()))
        self.assertFalse(request['future'].cancelled())
        request['future'].set_result({'available':True})
        self.assertTrue((await second)['available'])
        self.assertFalse(e.desktop_requests)

    async def test_desktop_long_poll_wakes_without_the_job_poll_interval(self):
        from carme.execution import build_execution_router, encoded, signature
        e=self.configure();key=b'x'*64;path=self.root/'broker-key';path.write_bytes(key)
        self.config.isolation['broker']={'key_file':str(path),'instance_id':'fixture'}
        app=self.app();app.include_router(build_execution_router(e))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1') as client:
            body=encoded({'op':'desktop_claim','wait':2});nonce=f'{time.time():.3f}:'+os.urandom(16).hex()
            claim=asyncio.create_task(client.post('/internal/broker',content=body,
                headers={'X-Carme-Nonce':nonce,'X-Carme-Signature':signature(key,nonce,body)}))
            await asyncio.sleep(.02);start=time.monotonic()
            pending=asyncio.create_task(e.desktop_request('bot','status'))
            response=await asyncio.wait_for(claim,.2)
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json()['operation'],'status')
            self.assertLess(time.monotonic()-start,.2)
            next(iter(e.desktop_requests.values()))['future'].set_result({'available':True})
            await pending

    async def test_conversation_snapshot_cursor_and_changed_message_only(self):
        cid=self.store.create_conversation(['bot'])['id']
        first=self.store.add_conversation_message(cid,'bot','assistant','old '*1000)
        second=self.store.add_conversation_message(cid,'bot','assistant','streaming',status='streaming')
        self.store.add_event('conversation.message',{'conversation_id':cid,'message_id':second})
        with patch.dict(os.environ,{'CARME_TOKEN':'synthetic-delta-token'}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(self.app()),base_url='http://127.0.0.1',
                    headers={'Authorization':'Bearer synthetic-delta-token'}) as client:
                cursor=(await client.get('/api/events/cursor')).json()['after_id']
                snapshot=(await client.get('/api/conversations/'+cid)).json()
                self.assertEqual(len(snapshot['messages']),2)
                self.assertEqual(snapshot['event_cursor'],cursor)
                self.store.add_conversation_message(cid,'bot','assistant','updated',message_id=second)
                self.store.add_event('conversation.message',{'conversation_id':cid,'message_id':second})
                delta=(await client.get('/api/conversations/'+cid,params={'after_event_id':cursor})).json()
                self.assertTrue(delta['delta'])
                self.assertEqual([m['id'] for m in delta['messages']],[second])
                self.assertEqual(delta['messages'][0]['content'],'updated')
                self.assertNotIn('tasks',delta)
                self.store.add_event('task.finished',{},agent_id='bot')
                full=(await client.get('/api/conversations/'+cid,params={'after_event_id':cursor})).json()
                self.assertFalse(full['delta'])
                self.assertIn(first,[m['id'] for m in full['messages']])
    async def test_publish_blocks_symlink_hardlink_and_source_escape(self):
        self.configure();root=self.root.resolve();home=root/'bot';home.mkdir();(root/'apps').mkdir()
        outside=root/'outside';outside.write_text('DO_NOT_COPY')
        build=home/'build';build.mkdir();(build/'file').symlink_to(outside)
        broker=SimpleNamespace(home=root)
        with patch('carme.account_disk.checked',return_value={'mount':str(root)}),patch('carme.account_disk.bot_home',return_value=home):
            for path in ('../outside','/tmp','build'):
                with self.assertRaises((ValueError,OSError)):publish_software(broker,'bot',{'path':path,'name':'test','version':'1'})
            (build/'file').unlink();os.link(outside,build/'file')
            with self.assertRaises(ValueError):publish_software(broker,'bot',{'path':'build','name':'test','version':'1'})
            (build/'file').unlink();(build/'file').write_text('SAFE')
            result=publish_software(broker,'bot',{'path':'build','name':'test','version':'1'})
            self.assertEqual(result['bytes'],4)
            self.assertEqual((root/'apps/test/1/file').read_text(),'SAFE')
            with self.assertRaises(ValueError):publish_software(broker,'bot',{'path':'build','name':'test','version':'1'})
        self.assertEqual(outside.read_text(),'DO_NOT_COPY')

class Autonomy(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = m1.M1.asyncSetUp
    asyncTearDown = m1.M1.asyncTearDown
    configure = Desktop.configure

    def context(self, approve=None):
        from carme.tools.base import ToolContext
        spec = self.config.agents.get('bot')
        _, meta = self.runtime._task_agent_snapshot('bot')
        tid = self.store.create_task('bot', 'Synthetic local autonomy', meta=meta)
        return ToolContext(spec, tid, self.store, approve=approve, browser_manager=self.runtime.browsers,
            extras={'policy': meta['policy'], 'check_policy': lambda: self.runtime.check_task_policy(tid)})

    async def test_atomic_download_failure_receipt_allows_checkpoint_resume(self):
        from carme.docker_desktop import DesktopNoEffectError, download_software
        e=self.configure(); ctx=self.context(); tid=ctx.task_id
        home=self.root/'download-home'; home.mkdir()
        request={'bot_id':'bot','arguments':{'url':'https://example.com/missing.png','name':'missing.png'},
                 'deadline':time.time()+30,'id':'synthetic'}
        broker=SimpleNamespace(home=self.root,call=AsyncMock(return_value={'active':True}))
        with patch('carme.account_disk.bot_home',return_value=home), patch('carme.docker_browser.fetch_public',
                AsyncMock(return_value={'status':403,'headers':{}})):
            with self.assertRaisesRegex(DesktopNoEffectError,'http_403'):
                await download_software(broker,request,{'session_id':'synthetic'}, {})
        self.assertFalse((home/'Downloads/missing.png').exists())
        self.assertFalse(list((home/'Downloads').glob('.download-*')))
        with patch.object(e,'desktop_request',AsyncMock(side_effect=DesktopNoEffectError('desktop_download_http_403',status_code=403))) as desktop:
            result=await self.runtime.registry.execute(ctx,'bot_computer',
                {'operation':'fetch','arguments':request['arguments']})
            bounded=await self.runtime.registry.execute(ctx,'bot_computer',
                {'operation':'fetch','arguments':request['arguments']})
            desktop.assert_awaited_once()
        self.assertIn('desktop_download_http_403',result)
        self.assertIn('下载来源暂不可用',bounded)
        receipt=self.store._query_one('SELECT status,receipt FROM task_operations WHERE task_id=?',(tid,))
        self.assertEqual(receipt['status'],'finished')
        self.assertEqual(json.loads(receipt['receipt'])['effect'],'not_performed')
        self.store.checkpoint(tid,{'messages':[],'next_step':1,'memory_refs':[]})
        self.store.finish_task(tid,'synthetic failure',status='failed')
        with patch.object(self.runtime,'_schedule'):
            self.assertEqual((await self.runtime.resume(tid))['status'],'queued')

    async def test_uncertain_shell_failure_remains_pending(self):
        e=self.configure(); ctx=self.context(); tid=ctx.task_id
        with patch.object(e,'desktop_request',AsyncMock(side_effect=RuntimeError('synthetic shell partial failure'))):
            await self.runtime.registry.execute(ctx,'bot_computer',
                {'operation':'shell','arguments':{'command':'touch partial; false'}})
        self.assertEqual(self.store._query_one('SELECT status FROM task_operations WHERE task_id=?',(tid,))['status'],'pending')
        self.store.checkpoint(tid,{'messages':[],'next_step':1,'memory_refs':[]})
        self.store.finish_task(tid,'synthetic failure',status='failed')
        with self.assertRaisesRegex(ValueError,'external_effect_reconciliation_required'):
            await self.runtime.resume(tid)

    async def test_control_accepts_only_structured_linux_fetch_no_effect(self):
        from carme.docker_desktop import DesktopNoEffectError
        e=self.configure(); ctx=self.context()
        for operation,effect,expected in [('fetch','not_performed',DesktopNoEffectError),
                                          ('fetch',None,RuntimeError),('shell','not_performed',RuntimeError)]:
            args={'url':'https://example.com/a','name':'a'} if operation=='fetch' else {'command':'false'}
            work=asyncio.create_task(e.desktop_request('bot',operation,args,ctx=ctx))
            await asyncio.sleep(0)
            request=next(iter(e.desktop_requests.values()))
            request['future'].set_result({'error':'synthetic failure','http_status':403,
                                          **({'effect':effect} if effect else {})})
            with self.assertRaises(expected) as result:await work
            self.assertEqual(type(result.exception),expected)
            if expected is DesktopNoEffectError:self.assertEqual(result.exception.status_code,403)

    async def test_cancellation_after_download_publication_is_not_no_effect(self):
        from carme.docker_desktop import download_software
        self.configure(); home=self.root/'published-home';home.mkdir()
        request={'bot_id':'bot','arguments':{'url':'https://example.com/a','name':'a'},
                 'deadline':time.time()+30,'id':'synthetic'}
        broker=SimpleNamespace(home=self.root,call=AsyncMock(return_value={'active':True}))
        real_link=os.link
        def publish_then_cancel(*args,**kwargs):
            real_link(*args,**kwargs)
            raise asyncio.CancelledError()
        with patch('carme.account_disk.bot_home',return_value=home), \
             patch('carme.docker_browser.fetch_public',AsyncMock(return_value={'status':200,'headers':{}})), \
             patch('carme.docker_desktop.os.link',side_effect=publish_then_cancel):
            with self.assertRaises(asyncio.CancelledError):
                await download_software(broker,request,{'session_id':'synthetic'}, {})
        self.assertTrue((home/'Downloads/a').exists())

    async def test_failed_desktop_read_has_no_external_effect_receipt(self):
        e=self.configure();ctx=self.context()
        with patch.object(e,'desktop_request',AsyncMock(side_effect=RuntimeError('synthetic read failure'))):
            await self.runtime.registry.execute(ctx,'bot_computer',{'operation':'status','arguments':{}})
        row=self.store._query_one('SELECT status,receipt FROM task_operations WHERE task_id=?',(ctx.task_id,))
        self.assertEqual(row['status'],'finished')
        self.assertEqual(json.loads(row['receipt'])['effect'],'not_performed')

    async def test_finish_reserve_refuses_new_tool_before_dispatch(self):
        e=self.configure();ctx=self.context();ctx.extras['deadline']=time.time()+30
        with patch.object(e,'desktop_request',AsyncMock()) as desktop:
            result=await self.runtime.registry.execute(ctx,'bot_computer',
                {'operation':'shell','arguments':{'command':'true'}})
            desktop.assert_not_awaited()
        self.assertIn('任务即将截止',result)
        self.assertFalse(self.store._query('SELECT id FROM task_operations WHERE task_id=?',(ctx.task_id,)))

    async def test_share_receiver_is_allowed_but_caller_identity_is_fixed(self):
        from carme.attachments import archive_binary
        from carme.tools.base import ToolContext, ToolRegistry
        from carme.tools.files import ShareAttachmentTool
        self.configure()
        cid=self.store.create_conversation(['bot','receiver','intruder'])['id']
        sender=self.store.create_task('bot','synthetic share',conversation_id=cid)
        receiver=self.store.create_task('receiver','synthetic read',conversation_id=cid)
        intruder=self.store.create_task('intruder','synthetic unauthorized share',conversation_id=cid)
        file=archive_binary(self.store,cid,'fixture.txt',b'SYNTHETIC',task_id=sender)
        ctx=ToolContext(self.config.agents.get('bot'),sender,self.store,
            extras={'policy':{'tools':['share_attachment'],'max_tool_calls':8}})
        registry=ToolRegistry(); registry.register(ShareAttachmentTool())
        with self.assertRaises(ValueError):self.store.artifact_access(receiver,file['id'])
        denied=await registry.execute(ctx,'share_attachment',{'file_id':file['id'],'bot_id':'receiver','task_id':receiver})
        self.assertIn('identity_override_denied',denied)
        denied=await registry.execute(ctx,'share_attachment',{'file_id':file['id'],'bot_id':'outsider'})
        self.assertIn('share_target_not_in_conversation',denied)
        intruder_ctx=ToolContext(SimpleNamespace(id='intruder',tools=['share_attachment']),intruder,self.store,
            extras={'policy':{'tools':['share_attachment'],'max_tool_calls':8}})
        denied=await registry.execute(intruder_ctx,'share_attachment',{'file_id':file['id'],'bot_id':'receiver'})
        self.assertIn('artifact_not_sent',denied)
        with self.assertRaises(ValueError):self.store.artifact_access(receiver,file['id'])
        shared=await registry.execute(ctx,'share_attachment',{'file_id':file['id'],'bot_id':'receiver'})
        self.assertIn('已把',shared)
        self.assertEqual(self.store.artifact_access(receiver,file['id'])['id'],file['id'])
        denied=await registry.execute(ctx,'share_attachment',{'file_id':file['id'],'bot_id':'bot'})
        self.assertIn('share_target_is_self',denied)

    def network(self, sends):
        from contextlib import ExitStack
        async def handle(request):
            sends.append(request)
            return httpx.Response(200, text='SYNTHETIC_OK')
        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(client.aclose)
        stack = ExitStack()
        stack.enter_context(patch('carme.docker_browser.public_address', AsyncMock(return_value='93.184.216.34')))
        stack.enter_context(patch('carme.docker_browser.httpx.AsyncClient', return_value=client))
        return stack

    def request(self, e, ctx):
        e.desktop_sessions['bot'] = 'session'
        item = {'id': 'synthetic', 'bot_id': 'bot', 'ctx': ctx, 'claimed': True, 'target': 'linux',
            'deadline': time.time()+85, 'epoch': e.desktop_control('bot')['epoch'], 'operation': 'shell',
            'future': asyncio.get_running_loop().create_future()}
        e.desktop_requests['synthetic'] = item
        self.addCleanup(e.desktop_requests.clear)
        return item

    def payload(self, method='POST', path='/messages/send'):
        import base64
        return {'url': 'https://example.com'+path, 'method': method,
            'headers': {'Authorization': 'SYNTHETIC_SECRET'},
            'body': base64.b64encode(b'SYNTHETIC_PRIVATE_BODY').decode() if method=='POST' else ''}

    async def test_local_tools_do_not_ask_for_approval(self):
        from carme.approval import ApprovalOutcome
        from carme.sandbox.base import ExecResult
        from carme.tools.shell import ShellTool
        e=self.configure(); approve=AsyncMock(return_value=ApprovalOutcome(False))
        ctx=self.context(approve)
        with patch.object(e,'desktop_request',AsyncMock(return_value={'text':'LOCAL_OK'})):
            for op,args in [('shell',{'command':'mkdir -p app; rm -rf app'}),('fetch',{'url':'https://example.com/a.zip','name':'a.zip'}),
                            ('publish',{'name':'fixture','version':'1','path':'app'}),('act_ui',{'stateId':'fixture','actions':[]})]:
                self.assertEqual(await self.runtime.registry.execute(ctx,'bot_computer',{'operation':op,'arguments':args}),'LOCAL_OK')
        sandbox=SimpleNamespace(exec=AsyncMock(return_value=ExecResult(ok=True,exit_code=0)))
        ctx.sandbox_handle=SimpleNamespace(get=AsyncMock(return_value=sandbox))
        await ShellTool().run(ctx,'true')
        approve.assert_not_awaited(); self.assertTrue(ctx.local_autonomy)

    async def test_host_still_requires_approval_and_stale_linux_policy_fails(self):
        from carme.approval import ApprovalOutcome
        e=self.configure(); old=self.context()
        with patch.object(e,'desktop_target',return_value='host:fixture'):
            approve=AsyncMock(return_value=ApprovalOutcome(False,'denied')); ctx=self.context(approve)
            self.assertFalse(ctx.local_autonomy)
            with patch.object(e,'desktop_request',AsyncMock()) as run:
                self.assertIn('已拒绝',await self.runtime.registry.execute(ctx,'bot_computer',{'operation':'act_ui','arguments':{}}))
                self.assertIn('permission_version_changed',await self.runtime.registry.execute(old,'bot_computer',{'operation':'shell','arguments':{'command':'true'}}))
                run.assert_not_awaited()
            approve.assert_awaited_once()

    async def test_reads_do_not_prompt_but_external_posts_do(self):
        from carme.approval import ApprovalOutcome
        e=self.configure(); approve=AsyncMock(return_value=ApprovalOutcome(True)); ctx=self.context(approve)
        item=self.request(e,ctx); sends=[]
        with self.network(sends):
            await e.desktop_fetch('bot','session',self.payload('GET','/download.zip'))
            approve.assert_not_awaited()
            await e.desktop_fetch('bot','session',self.payload())
        self.assertEqual(len(sends),2);approve.assert_awaited_once()
        self.assertEqual(item['network_sent'],1)
        detail=json.dumps(approve.call_args.kwargs['detail'])
        self.assertNotIn('SYNTHETIC_PRIVATE_BODY',detail);self.assertNotIn('SYNTHETIC_SECRET',detail)
        self.assertIn('request_digest',detail)

    async def test_denial_missing_approval_and_background_never_send(self):
        from carme.approval import ApprovalOutcome
        e=self.configure(); sends=[]
        for approve in (None,AsyncMock(return_value=ApprovalOutcome(False,'denied'))):
            self.request(e,self.context(approve))
            with self.network(sends),self.assertRaises(ValueError):
                await e.desktop_fetch('bot','session',self.payload())
        e.desktop_requests.clear()
        with self.network(sends),self.assertRaisesRegex(ValueError,'active_task'):
            await e.desktop_fetch('bot','session',self.payload())
        self.assertFalse(sends)

    async def test_changed_body_policy_or_target_invalidates_approval(self):
        from carme.approval import ApprovalOutcome
        for change in ('body','policy','target'):
            e=self.configure(); payload=self.payload(); sends=[]
            async def approve(**kwargs):
                if change=='body': payload['body']='bmV3'
                if change=='policy': self.config.agents.get('bot').tools=[]
                if change=='target': e.desktop_sessions['bot']='revoked'
                return ApprovalOutcome(True)
            self.request(e,self.context(approve))
            with self.network(sends),self.assertRaises((ValueError,RuntimeError)):
                await e.desktop_fetch('bot','session',payload)
            self.assertFalse(sends)

    async def test_approval_wait_keeps_command_alive_and_blocks_duplicates(self):
        from carme.approval import ApprovalOutcome
        e=self.configure(); started=asyncio.Event(); release=asyncio.Event(); sends=[]
        async def approve(**kwargs):
            started.set();await release.wait();return ApprovalOutcome(True)
        ctx=self.context(approve)
        command=asyncio.create_task(e.desktop_request('bot','shell',{'command':'fixture'},ctx=ctx))
        await asyncio.sleep(.01); item=next(iter(e.desktop_requests.values()))
        item['claimed']=True;item['deadline']=time.time()+.1;e.desktop_sessions['bot']='session'
        with self.network(sends):
            network=asyncio.create_task(e.desktop_fetch('bot','session',self.payload()))
            await asyncio.wait_for(started.wait(),1)
            item['future'].set_result({'text':'LOCAL_ACTION_FINISHED'})
            await asyncio.sleep(.15)
            self.assertFalse(command.done());self.assertFalse(sends)
            with self.assertRaisesRegex(ValueError,'pending'):
                await e.desktop_fetch('bot','session',self.payload())
            release.set();await network
            result=await command
        self.assertIn('已批准并传输 1',result['text']);self.assertEqual(len(sends),1)

    async def test_cancel_closes_pending_approval_and_never_sends(self):
        from carme.approval import request_approval
        e=self.configure(); sends=[]; started=asyncio.Event()
        ctx=self.context()
        async def event(kind,data):
            if kind=='approval.requested': started.set()
        async def approve(**kwargs):
            return await request_approval(self.store,task_id=ctx.task_id,agent_id='bot',on_event=event,**kwargs)
        ctx.approve=approve
        command=asyncio.create_task(e.desktop_request('bot','shell',{'command':'fixture'},ctx=ctx))
        await asyncio.sleep(.01);item=next(iter(e.desktop_requests.values()));item['claimed']=True;e.desktop_sessions['bot']='session'
        with self.network(sends):
            network=asyncio.create_task(e.desktop_fetch('bot','session',self.payload()))
            await asyncio.wait_for(started.wait(),1)
            command.cancel();await asyncio.gather(command,network,return_exceptions=True)
        self.assertFalse(sends)
        rows=self.store._query('SELECT status FROM approvals WHERE task_id=?',(ctx.task_id,))
        self.assertEqual([r['status'] for r in rows],['rejected'])

    async def test_human_control_has_no_bot_prompt_and_is_revocable(self):
        e=self.configure();e.desktop_sessions['bot']='session';sends=[]
        control=e.desktop_control('bot');control.update(enabled=True,expires=time.time()+90)
        with self.network(sends):
            await e.desktop_fetch('bot','session',self.payload())
        self.assertEqual(len(sends),1)
        control['enabled']=False
        with self.assertRaisesRegex(ValueError,'active_task'):
            await e.desktop_fetch('bot','session',self.payload())

    async def test_high_risk_get_action_is_not_a_plain_download(self):
        from carme.approval import ApprovalOutcome
        e=self.configure();approve=AsyncMock(return_value=ApprovalOutcome(False));self.request(e,self.context(approve));sends=[]
        with self.network(sends),self.assertRaises(ValueError):
            await e.desktop_fetch('bot','session',self.payload('GET','/account?action=delete'))
        approve.assert_awaited_once();self.assertFalse(sends)

if __name__=='__main__':unittest.main(verbosity=2)
