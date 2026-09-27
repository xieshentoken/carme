"""M6 real Runtime/Store, synthetic models and a controlled recipient transport."""
import asyncio,base64,copy,json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
import httpx
import test_visitor_context as fixtures
from test_runtime import settle
from carme.llm import LLMResponse,ToolCall
from carme.approval import request_approval
from carme.store import Store
from carme.tools.base import ToolContext
from carme.attachments import archive_binary,file_path

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f=fixtures.ContextTests();await self.f.asyncSetUp()
        self.r,self.s,self.cid=self.f.r,self.f.s,self.f.cid
        for spec in self.r.config.agents.agents.values():
            spec.execution_target='container';spec.execution_target_id='action'
            spec.tools=['files','exec','browser','team'];spec.can_delegate=True
        self.health=patch.object(self.r.execution,'health',return_value={'broker':'ready'});self.health.start()
    async def asyncTearDown(self):
        self.health.stop();await self.f.asyncTearDown()
    def staged(self,goal='submit'):
        _,meta=self.r._task_agent_snapshot('chief')
        turn=self.s.create_conversation_turn(self.cid,'chief',goal,'stage'+str(time.time_ns()),actor_key=self.f.actor,recipients={'chief':meta})
        self.r.bind_envelope(turn['task_id']);return turn['task_id']
    def ctx(self,tid):
        async def approve(**kwargs):
            return await request_approval(self.s,task_id=tid,agent_id='chief',timeout=2,**kwargs)
        return ToolContext(agent=self.f.spec,task_id=tid,store=self.s,approve=approve,
            extras={'policy':json.loads(self.s.get_task(tid)['meta'])['policy'],'check_policy':lambda:self.r.check_task_policy(tid)})
    async def pending(self):
        for _ in range(100):
            rows=self.s.list_approvals(status='pending')
            if rows:return rows[0]
            await asyncio.sleep(.01)
        self.fail('approval did not appear')
    async def test_runtime_visitor_and_owner_shared_outputs_no_private_context(self):
        self.f.g.responses=[LLMResponse(text='safe-one'),LLMResponse(text='safe-two')]
        for actor in (self.f.actor,'owner'):
            result=await self.r.submit_message(self.cid,'@Chief hello','call-'+actor,actor_key=actor)
            await settle(self.r)
            self.assertEqual(self.s.get_task(result['task_id'])['status'],'done')
        self.assertNotIn('SENTINEL',json.dumps(self.f.g.calls,default=str))
        events=self.s.list_events(limit=1000)
        self.assertFalse(any('preview' in e['payload'] for e in events if e['type']=='task.finished'))
    async def test_group_order_and_actor_idempotency(self):
        self.f.g.responses=[LLMResponse(text='chief'),LLMResponse(text='writer')]
        args=(self.cid,'hello','same')
        a,b=await asyncio.gather(self.r.submit_message(*args,actor_key=self.f.actor),self.r.submit_message(*args,actor_key=self.f.actor))
        self.assertEqual(a['task_ids'],b['task_ids']);await settle(self.r)
        self.assertEqual([self.s.get_task(t)['status'] for t in a['task_ids']],['done','done'])
        self.assertEqual(len(self.f.g.calls),2)
        self.assertIn('chief',json.dumps(self.f.g.calls[1],default=str))
    async def test_delegate_stays_in_group_and_inherits_origin(self):
        self.f.g.responses=[LLMResponse(tool_calls=[ToolCall(id='call',name='delegate',arguments={'agent':'writer','goal':'child'})]),LLMResponse(text='child-result'),LLMResponse(text='parent-result')]
        turn=await self.r.submit_message(self.cid,'@Chief delegate','delegate',actor_key=self.f.actor);await settle(self.r)
        parent=self.s.get_task(turn['task_id']);children=self.s.children_of(parent['id'])
        self.assertEqual(parent['status'],'done',parent['error']);self.assertEqual(len(children),1)
        self.assertEqual(children[0]['status'],'done',children[0]['error'])
        self.assertEqual(json.loads(parent['meta'])['origin'],json.loads(children[0]['meta'])['origin'])
        self.assertEqual(children[0]['conversation_id'],self.cid)
    async def test_cross_group_publication_and_rollback(self):
        tid=self.staged();other=self.s.create_conversation(['chief','writer'])['id']
        with self.assertRaises(ValueError):self.s.add_conversation_message(other,'chief','assistant','secret',task_id=tid)
        with self.assertRaises(ValueError):self.s.add_event('task.finished',{'conversation_id':other,'preview':'secret'},task_id=tid)
        n=len(self.s.list_attachments(self.cid))
        with self.assertRaises(RuntimeError):
            with self.s.transaction():
                archive_binary(self.s,self.cid,'atomic.txt',b'bytes',task_id=tid)
                raise RuntimeError('synthetic rollback')
        self.assertEqual(len(self.s.list_attachments(self.cid)),n)
    async def test_removal_cancels_waiting_model_and_blocks_new_publication(self):
        started=asyncio.Event();release=asyncio.Event()
        async def model():started.set();await release.wait();return LLMResponse(text='late-secret')
        self.f.g.responses=[model]
        turn=await self.r.submit_message(self.cid,'@Chief wait','remove',actor_key=self.f.actor);await started.wait()
        await self.r.update_visitor(self.cid,self.f.v['id'],'remove',actor_key='owner',expected_revision=1)
        release.set();await settle(self.r)
        self.assertEqual(self.s.get_task(turn['task_id'])['status'],'cancelled')
        self.assertFalse(any('late-secret' in m['content'] for m in self.s.list_conversation_messages(self.cid)))
        with self.assertRaises(ValueError):self.s.add_conversation_message(self.cid,'chief','assistant','late',task_id=turn['task_id'])
        with self.assertRaises(ValueError):await self.r.resume(turn['task_id'])
    async def test_other_connection_revokes_immediately(self):
        tid=self.staged();peer=Store(self.s.path)
        try:peer.update_visitor(self.cid,self.f.v['id'],'remove',actor_key='owner',expected_revision=1)
        finally:peer.close()
        with self.assertRaises((ValueError,RuntimeError)):self.r.check_task_policy(tid)
        with self.assertRaises(ValueError):archive_binary(self.s,self.cid,'late.txt',b'late',task_id=tid)
    async def test_owner_approval_only_exact_body_and_no_resend(self):
        tid=self.staged();ctx=self.ctx(tid);received=[]
        async def receiver(data,safety,check):check();received.append(copy.deepcopy(data));return {'status':200,'headers':[],'body':base64.b64encode(b'ok').decode()}
        args={'url':'https://example.com/submit','method':'POST','body':'exact payload'}
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public',side_effect=receiver):
            job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',args));row=await self.pending()
            self.assertEqual(received,[]);self.assertEqual(row['detail']['body_text'],'exact payload')
            with self.assertRaises(ValueError):self.s.decide_approval(row['id'],approved=True,actor_key=self.f.actor)
            self.s.decide_approval(row['id'],approved=True);self.assertIn('ok',await job)
            retry=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',args));row=await self.pending();self.s.decide_approval(row['id'],approved=True);await retry
        self.assertEqual(len(received),1);self.assertEqual(base64.b64decode(received[0]['body']),b'exact payload')
    async def test_denial_and_revocation_during_approval_have_zero_effect(self):
        tid=self.staged();ctx=self.ctx(tid)
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public') as transport:
            job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',{'url':'https://example.com/data'}));row=await self.pending()
            self.s.decide_approval(row['id'],approved=False)
            with self.assertRaises(ValueError):await job
            job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',{'url':'https://example.com/data'}));row=await self.pending()
            self.f.change('remove')
            with self.assertRaises((ValueError,RuntimeError)):await job
            self.assertEqual(transport.call_count,0)
    async def test_changed_tool_arguments_invalidate_approval(self):
        tid=self.staged();ctx=self.ctx(tid);ctx.extras['call']={'name':'fetch_page','arguments':{'body':'one'}}
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public') as transport:
            job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',{'url':'https://example.com/submit','method':'POST','body':'one'}));row=await self.pending()
            ctx.extras['call']['arguments']['body']='two';self.s.decide_approval(row['id'],approved=True)
            with self.assertRaises(ValueError):await job
            self.assertFalse(transport.called)
    async def test_unknown_effect_remains_pending_and_cannot_replay(self):
        tid=self.staged();ctx=self.ctx(tid);args={'url':'https://example.com/submit','method':'POST','body':'once'}
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public',side_effect=TimeoutError('unknown')) as transport:
            for attempt in range(2):
                job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',args));row=await self.pending();self.s.decide_approval(row['id'],approved=True)
                with self.assertRaises((TimeoutError,ValueError)):await job
            self.assertEqual(transport.call_count,1)
        self.assertEqual(self.s._query_one('SELECT status FROM task_operations WHERE task_id=?',(tid,))['status'],'pending')

    async def test_real_http_recipient_zero_before_approval_exact_after(self):
        received=[]
        async def recipient(reader,writer):
            head=await reader.readuntil(b'\r\n\r\n')
            length=next(int(line.split(b':',1)[1]) for line in head.split(b'\r\n') if line.lower().startswith(b'content-length:'))
            body=await reader.readexactly(length);received.append((head,body))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok');await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(recipient,'127.0.0.1',0)
        port=server.sockets[0].getsockname()[1]
        original_send=httpx.AsyncHTTPTransport.handle_async_request
        async def local_send(transport,request):
            request.url=request.url.copy_with(port=port)
            return await original_send(transport,request)
        try:
            with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.public_address',return_value='127.0.0.1'),patch.object(httpx.AsyncHTTPTransport,'handle_async_request',local_send):
                job=asyncio.create_task(self.r.execution.common_query(self.ctx(self.staged()),'fetch_page',{'url':'http://example.com/receipt','method':'POST','body':'approved bytes'}))
                row=await self.pending();await asyncio.sleep(.03);self.assertEqual(received,[])
                self.s.decide_approval(row['id'],approved=True);self.assertIn('ok',await job)
            self.assertEqual(len(received),1);self.assertEqual(received[0][1],b'approved bytes')
            self.assertNotIn(b'cookie:',received[0][0].lower());self.assertNotIn(b'authorization:',received[0][0].lower())
        finally:server.close();await server.wait_closed()
    async def test_attachment_changed_while_waiting_never_sent(self):
        tid=self.staged();f=archive_binary(self.s,self.cid,'payload.txt',b'original',task_id=tid)
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public') as transport:
            job=asyncio.create_task(self.r.execution.common_query(self.ctx(tid),'fetch_page',{'url':'https://example.com/upload','method':'POST','attachment_id':f['id']}));row=await self.pending()
            file_path(self.s,f['id']).write_bytes(b'changed');self.s.decide_approval(row['id'],approved=True)
            with self.assertRaises(ValueError):await job
            self.assertFalse(transport.called)
    async def test_expiry_and_redirect_do_not_grant_followup(self):
        tid=self.staged();ctx=self.ctx(tid)
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public') as transport:
            job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',{'url':'https://example.com/expired'}));row=await self.pending()
            detail=row['detail'];detail['expires_at']=time.time()-1
            self.s._write('UPDATE approvals SET detail=? WHERE id=?',(json.dumps(detail),row['id']))
            with self.assertRaises(ValueError):self.s.decide_approval(row['id'],approved=True)
            self.s.decide_approval(row['id'],approved=False)
            with self.assertRaises(ValueError):await job
            self.assertFalse(transport.called)
        reply={'status':302,'headers':[['location','https://example.com/elsewhere']],'body':''}
        with patch('carme.approval.POLL_INTERVAL',.01),patch('carme.docker_browser.fetch_public',return_value=reply) as transport:
            job=asyncio.create_task(self.r.execution.common_query(ctx,'fetch_page',{'url':'https://example.com/redirect'}));row=await self.pending();self.s.decide_approval(row['id'],approved=True)
            with self.assertRaises(ValueError):await job
            self.assertEqual(transport.call_count,1)
    async def test_child_artifact_handoff_and_task_quota(self):
        self.f.g.responses=[LLMResponse(tool_calls=[ToolCall(id='delegate',name='delegate',arguments={'agent':'writer','goal':'file'})]),LLMResponse(tool_calls=[ToolCall(id='file',name='create_artifact',arguments={'name':'child.txt','content':'safe child'})]),LLMResponse(text='child done'),LLMResponse(text='parent done')]
        turn=await self.r.submit_message(self.cid,'@Chief delegate file','file',actor_key=self.f.actor);await settle(self.r)
        self.assertEqual(self.s.get_task(turn['task_id'])['status'],'done')
        child=self.s.children_of(turn['task_id'])[0];self.assertEqual(child['status'],'done',child['error'])
        files=self.s.list_attachments(self.cid);f=next(f for f in files if f['name']=='child.txt')
        self.assertEqual(self.s.artifact_access(turn['task_id'],f['id'])['id'],f['id'])
        for i in range(9):self.staged('quota')
        with self.assertRaisesRegex(ValueError,'visitor_task_rate_limited'):self.staged('over limit')

    async def test_authenticated_api_execution_and_owner_only_removal(self):
        import os
        from fastapi import FastAPI,Depends
        from carme.api.routes import build_router,require_token
        from carme.security import visitor_proof
        token='synthetic-control-token';account='alice';instance='carme-'+'0'*20
        self.r.config.isolation['broker']={'instance_id':instance}
        secret,_=self.s.issue_visitor_session(self.s.visitor_credential(self.f.v['username']),'a'*64,token+':1',time.time()+60)
        app=FastAPI();app.state.config=self.r.config;app.state.store=self.s
        app.include_router(build_router(self.r.config,self.s,self.r),dependencies=[Depends(require_token)])
        async def visitor(client,method,path,data=None):
            body=json.dumps(data).encode() if data else b''
            proof=visitor_proof(token,account=account,instance=instance,method=method,target=path,body=body,secret=secret,subject='a'*64,access_expires=time.time()+60,account_version=1)
            return await client.request(method,path,content=body,headers={'Content-Type':'application/json','X-Carme-Visitor-Session':secret,'X-Carme-Visitor-Proof':proof})
        path=f'/api/visitor/alice/conversations/{self.cid}/messages'
        self.f.g.responses=[LLMResponse(text='api result')]
        with patch.dict(os.environ,{'CARME_TOKEN':token,'CARME_ACCOUNT_ID':account}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
                reply=await visitor(client,'POST',path,{'content':'@Chief hello','request_id':'api','mode':'task'})
                self.assertEqual(reply.status_code,200,reply.text);await settle(self.r)
                tid=reply.json()['task_id'];self.assertEqual(self.s.get_task(tid)['status'],'done')
                self.assertEqual(json.loads(self.s.get_task(tid)['meta'])['origin']['actor_key'],self.f.actor)
                denied=await visitor(client,'POST','/api/approvals/fake/decide',{'approved':True});self.assertEqual(denied.status_code,403)
                remove=f'/api/conversations/{self.cid}/visitors/{self.f.v["id"]}'
                denied=await visitor(client,'PATCH',remove,{'action':'remove','expected_revision':1});self.assertEqual(denied.status_code,403)
                removed=await client.patch(remove,json={'action':'remove','expected_revision':1},headers={'Authorization':'Bearer '+token})
                self.assertEqual(removed.status_code,200,removed.text)
                denied=await visitor(client,'POST',path,{'content':'@Chief late','request_id':'late','mode':'task'});self.assertEqual(denied.status_code,401)
    async def test_owner_cancel_shared_task_no_late_publication(self):
        started=asyncio.Event()
        async def model():started.set();await asyncio.sleep(10);return LLMResponse(text='late')
        self.f.g.responses=[model]
        turn=await self.r.submit_message(self.cid,'@Chief wait','cancel',actor_key=self.f.actor);await started.wait()
        self.assertTrue(await self.r.cancel(turn['task_id']));await settle(self.r)
        self.assertEqual(self.s.get_task(turn['task_id'])['status'],'cancelled')
    async def test_revoked_task_status_and_unscoped_event_cannot_revive(self):
        tid=self.staged();self.f.change('remove');self.s.set_task_status(tid,'running')
        self.assertEqual(self.s.get_task(tid)['status'],'cancelled')
        with self.assertRaises(ValueError):self.s.add_event('task.finished',{'preview':'late'},task_id=tid)

    async def test_two_connection_revoke_publication_race_is_serialized(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        tid=self.staged();peer=Store(self.s.path);barrier=Barrier(2)
        def publish():
            barrier.wait()
            try:return self.s.add_conversation_message(self.cid,'chief','assistant','racing result',task_id=tid)
            except ValueError:return None
        def revoke():
            barrier.wait();return peer.update_visitor(self.cid,self.f.v['id'],'remove',actor_key='owner',expected_revision=1)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                published=pool.submit(publish);revoked=pool.submit(revoke)
                result=published.result(timeout=5);self.assertFalse(revoked.result(timeout=5)['enabled'])
            count=len(self.s.list_conversation_messages(self.cid))
            with self.assertRaises(ValueError):self.s.add_conversation_message(self.cid,'chief','assistant','late result',task_id=tid)
            self.assertEqual(len(self.s.list_conversation_messages(self.cid)),count)
            if result:self.assertEqual(self.s._query_one('SELECT task_id FROM conversation_messages WHERE id=?',(result,))['task_id'],tid)
        finally:peer.close()

if __name__=='__main__':unittest.main(verbosity=2)
