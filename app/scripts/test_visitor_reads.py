"""M3: synthetic account/group reads plus native ASGI streaming revocation checks."""
import asyncio
import json
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request
import test_visitor_auth as auth
from carme.attachments import save_file, file_path, archive_binary
from carme.gateway import create_gateway, database


class VisitorReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth.VisitorAuthTests.setUpClass.__func__(cls)

    headers = auth.VisitorAuthTests.headers
    login = auth.VisitorAuthTests.login
    change = auth.VisitorAuthTests.change
    direct = auth.VisitorAuthTests.direct
    proof_headers = auth.VisitorAuthTests.proof_headers
    tearDown = auth.VisitorAuthTests.tearDown

    def setUp(self):
        auth.VisitorAuthTests.setUp(self)
        self.s = self.stores['alice']; self.cid = self.groups['alice']
        self.old = self.s.create_conversation_turn(self.cid, 'a', 'HIDDEN_OLD', 'old')
        self.old_stream = self.s.add_conversation_message(self.cid, 'a', 'assistant', 'HIDDEN_STREAM', status='streaming')
        self.old_file = save_file(self.s, self.cid, 'old.txt', b'HIDDEN_FILE', message_id=self.old['message_id'])
        self.change('remove'); self.change('reinvite', password_hash=self.hashed)
        self.new = self.s.create_conversation_turn(self.cid, 'a', 'PUBLIC_NEW', 'new')
        self.mid = self.new['message_id']
        self.file = save_file(self.s, self.cid, 'new.txt', b'PUBLIC_BYTES', message_id=self.mid)
        self.draft = save_file(self.s, self.cid, 'draft.txt', b'HIDDEN_DRAFT')
        self.other = self.s.create_conversation(['a', 'b'])['id']
        self.other_mid = self.s.add_conversation_message(self.other, 'a', 'user', 'OTHER_GROUP_SECRET')
        self.s.save_summary(self.cid, 'PRIVATE_SUMMARY', 99, 'PRIVATE_MODEL')
        self.s._write("UPDATE tasks SET goal='PRIVATE_GOAL',result='PRIVATE_RESULT',error='PRIVATE_ERROR',meta=? WHERE id=?", (json.dumps({**json.loads(self.s.get_task(self.new['task_id'])['meta']),'private':'PRIVATE_META'}), self.new['task_id']))
        self.child = self.s.create_task('a', 'PRIVATE_CHILD', parent_id=self.new['task_id'], conversation_id=self.cid)
        self.members = {aid: SimpleNamespace(name='Public '+aid, avatar={}, prompt='PRIVATE_PROMPT', tools=['PRIVATE_TOOL']) for aid in ('a', 'b', 'outside')}
        self.apps['alice'].state.config.agents = SimpleNamespace(get=self.members.__getitem__)
        self.assertEqual(self.login().status_code, 200)
        self.prefix = f'/api/visitor/alice/conversations/{self.cid}'

    def get(self, suffix=''):
        return self.client.get(self.prefix + suffix)

    def test_only_invited_group_and_public_members(self):
        r = self.client.get('/api/visitor/alice/conversations')
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([g['id'] for g in r.json()['conversations']], [self.cid])
        self.assertEqual(self.get().status_code, 200)
        self.assertNotIn('PRIVATE', self.get().text)
        self.assertNotIn('username', self.get().text)
        for path in (f'/api/visitor/alice/conversations/{self.other}', f'/api/visitor/bob/conversations/{self.groups["bob"]}'):
            self.assertIn(self.client.get(path).status_code, (401, 404))
        self.assertEqual(r.headers['cache-control'], 'no-store')
        self.assertEqual(self.login('bob').status_code,200)
        self.assertEqual(self.client.get('/api/visitor/bob/conversations').json()['conversations'][0]['id'],self.groups['bob'])
        self.assertEqual(self.get().status_code,401)

    def test_publication_cutoff_pagination_and_old_stream_update(self):
        self.s.add_conversation_message(self.cid, 'a', 'assistant', 'HIDDEN_UPDATED', message_id=self.old_stream, status='done')
        r = self.get('/messages?after=0&limit=1'); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([m['id'] for m in r.json()['messages']], [self.mid])
        cursor = r.json()['next_after']
        self.assertEqual(self.get(f'/messages?after={cursor}').json()['messages'], [])
        self.assertEqual(self.get('/messages/'+self.old_stream).status_code, 404)
        self.assertEqual(self.get('/messages/'+self.other_mid).status_code, 404)
        for q in ('after=-1', 'limit=101', 'after=999999999999999999999999'):
            self.assertEqual(self.get('/messages?'+q).status_code, 400)
        self.assertNotIn('HIDDEN', self.get('/messages').text)

    def test_history_toggle_independent_visitors_and_reinvite(self):
        v = self.s.create_visitor(self.cid, 'Later', self.hashed, actor_key='owner', expected_revision=self.s.get_conversation(self.cid)['access_revision'])
        self.change('history', allow_history=True)
        self.assertIn('HIDDEN_OLD', self.get('/messages').text)
        self.assertEqual(self.get('/attachments/'+self.old_file['id']).status_code, 200)
        self.assertEqual(self.login(username=v['username']).status_code, 200)
        self.assertEqual(self.get('/messages').json()['messages'], [])
        self.assertEqual(self.get('/attachments/'+self.file['id']).status_code, 404)
        self.assertEqual(self.login().status_code, 200)
        self.change('history', allow_history=False)
        self.assertNotIn('HIDDEN', self.get('/messages').text)
        self.change('remove'); self.change('reinvite', password_hash=self.hashed)
        self.assertEqual(self.get('/messages').status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.get('/messages').json()['messages'], [])

    def test_task_projection_requires_visible_human_root(self):
        r = self.get('/tasks/'+self.new['task_id']); self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(r.json()['task']), {'id', 'agent_id', 'status'})
        for tid in (self.old['task_id'], self.child):
            self.assertEqual(self.get('/tasks/'+tid).status_code, 404)
        with self.assertRaises(ValueError):
            self.s.add_conversation_message(self.cid, 'a', 'assistant', 'PUBLIC_REPLY', task_id=self.old['task_id'])
        self.assertEqual(self.get('/tasks/'+self.old['task_id']).status_code, 404)
        self.assertNotIn('PRIVATE', r.text)

    def test_attachments_binding_range_and_integrity(self):
        for fid in (self.old_file['id'], self.draft['id']):
            for suffix in ('', '/download'):
                self.assertEqual(self.get('/attachments/'+fid+suffix).status_code, 404)
        meta = self.get('/attachments/'+self.file['id']).json()['file']
        self.assertEqual(set(meta), {'id', 'name', 'mime', 'size', 'kind'})
        path = self.prefix+'/attachments/'+self.file['id']+'/download'
        r = self.client.get(path); self.assertEqual(r.content, b'PUBLIC_BYTES')
        self.assertEqual(r.headers['content-type'], 'application/octet-stream')
        for range_, content in (('bytes=0-5', b'PUBLIC'), ('bytes=-5', b'BYTES'), ('bytes=7-', b'BYTES')):
            r = self.client.get(path, headers={'Range': range_})
            self.assertEqual(r.status_code, 206, r.text); self.assertEqual(r.content, content)
        for range_ in ('bytes=999-', 'bytes=0-1,3-4', 'bytes=-0'):
            self.assertEqual(self.client.get(path, headers={'Range': range_}).status_code, 416)
        self.s._write('UPDATE attachments SET sha256=? WHERE id=?', ('0'*64, self.file['id']))
        self.assertEqual(self.client.get(path).status_code, 404)

    def test_generated_artifact_hash_and_published_binding(self):
        file=archive_binary(self.s,self.cid,'result.txt',b'PUBLIC_RESULT_FILE',task_id=self.new['task_id'])
        path=self.prefix+'/attachments/'+file['id']+'/download'
        self.assertEqual(self.client.get(path).status_code,404)
        self.s._write('UPDATE attachments SET message_id=? WHERE id=?',(self.mid,file['id']))
        self.assertEqual(self.client.get(path).content,b'PUBLIC_RESULT_FILE')
        file_path(self.s,file['id']).write_bytes(b'CORRUPTED_RESULT_FILE')
        self.assertEqual(self.client.get(path).status_code,409)

    def test_summary_search_owner_extensions_and_write_routes_closed(self):
        for suffix in ('/summary', '/search', '/approvals', '/export', '/tasks', '/attachments/'+self.file['id']+'/preview'):
            self.assertEqual(self.get(suffix).status_code, 403)
        for path in ('/api/tasks', '/api/approvals', '/api/agents', '/api/extensions', '/api/conversations/'+self.cid):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post(self.prefix+'/messages', json={'content':'no'}, headers=self.headers()).status_code, 422)

    def events(self):
        self.s.add_event('conversation.message', {'conversation_id':self.cid,'message_id':self.old['message_id'], 'private':'RAW_EVENT_SECRET'})
        self.s.add_event('conversation.message', {'conversation_id':self.other,'message_id':self.other_mid})
        e = self.s.add_event('conversation.message', {'conversation_id':self.cid,'message_id':self.mid, 'private':'RAW_EVENT_SECRET'})
        self.s.add_event('task.finished', {'private':'RAW_EVENT_SECRET'}, task_id=self.new['task_id'])
        self.s.add_event('task.finished', {'private':'PRIVATE_CHILD'}, task_id=self.child)
        self.s.add_event('conversation.summary', {'content':'PRIVATE_SUMMARY'})
        return e['id']

    def test_failure_diagnostics_redacted_for_messages_and_events(self):
        mid=self.s.add_conversation_message(self.cid,'a','assistant','执行失败：PRIVATE_DIAGNOSTIC',status='failed')
        self.s.add_event('conversation.message',{'conversation_id':self.cid,'message_id':mid})
        self.assertNotIn('PRIVATE_DIAGNOSTIC',self.get('/messages').text)
        self.assertNotIn('PRIVATE_DIAGNOSTIC',self.get('/messages/'+mid).text)
        path=self.prefix+'/events'
        r=self.direct(path,headers=self.proof_headers(path=path,access_expires=time.time()+1.1))
        self.assertEqual(r.status_code,200)
        self.assertNotIn('PRIVATE_DIAGNOSTIC',r.text)
        self.assertIn('message',r.text)

    def test_sse_replay_reconnect_rebuilds_safe_projection(self):
        cursor = self.events(); path = self.prefix+'/events'
        r = self.direct(path, headers=self.proof_headers(path=path, access_expires=time.time()+1.1))
        self.assertEqual(r.status_code, 200)
        self.assertIn('PUBLIC_NEW', r.text)
        for marker in ('HIDDEN', 'RAW_EVENT_SECRET', 'OTHER_GROUP', 'PRIVATE'):
            self.assertNotIn(marker, r.text)
        h = self.proof_headers(path=path, access_expires=time.time()+1.1); h['Last-Event-ID'] = str(cursor)
        r = self.direct(path, headers=h)
        self.assertNotIn('PUBLIC_NEW', r.text); self.assertIn('"type": "task"', r.text)
        self.change('history', allow_history=True)
        r = self.direct(path, headers=self.proof_headers(path=path, access_expires=time.time()+1.1))
        self.assertIn('HIDDEN_OLD', r.text)
        self.change('history', allow_history=False)
        r = self.direct(path, headers=self.proof_headers(path=path, access_expires=time.time()+1.1))
        self.assertNotIn('HIDDEN', r.text)

    async def capture_stream(self, path, on_chunk, *, headers=None):
        h = headers or self.proof_headers(path=path)
        scope = {'type':'http','asgi':{'version':'3.0','spec_version':'2.3'}, 'http_version':'1.1',
            'method':'GET','scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'',
            'headers':[(k.lower().encode(),v.encode()) for k,v in h.items()],
            'client':('127.0.0.1',1234),'server':('localhost',80)}
        sent = []; body_sent = False
        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {'type':'http.request','body':b'', 'more_body':False}
            await asyncio.Event().wait()
        async def send(message):
            sent.append(message)
            if message['type']=='http.response.body' and message.get('body'):
                on_chunk(message['body'])
        with patch.dict(os.environ, {'CARME_TOKEN':self.tokens['alice'],'CARME_ACCOUNT_ID':'alice'}):
            await asyncio.wait_for(self.apps['alice'](scope,receive,send), 2)
        return b''.join(m.get('body',b'') for m in sent), sent

    def test_gateway_to_control_sse_full_chain(self):
        self.events()
        config=dict(self.config,origin='https://fixture.example',local_only=False,access_team='fixture',access_audience='a'*64)
        # httpx.ASGITransport buffers an entire SSE response; this test adapter
        # carries native ASGI body frames incrementally through the real gateway.
        outer=self
        class LiveTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.host=='fixture.cloudflareaccess.com':
                    return httpx.Response(200,json={'keys':[outer.jwk]})
                body=await request.aread(); queue=asyncio.Queue(); started=asyncio.Event(); disconnected=asyncio.Event()
                meta={}; received=False
                scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},'http_version':'1.1',
                    'method':request.method,'scheme':'http','path':request.url.path,
                    'raw_path':request.url.raw_path.split(b'?')[0],'query_string':request.url.query,
                    'headers':[(k.lower(),v) for k,v in request.headers.raw],'client':('127.0.0.1',1234),'server':('localhost',19000)}
                async def receive():
                    nonlocal received
                    if not received:
                        received=True
                        return {'type':'http.request','body':body,'more_body':False}
                    await disconnected.wait()
                    return {'type':'http.disconnect'}
                async def send(message):
                    if message['type']=='http.response.start':
                        meta.update(message);started.set()
                    elif message['type']=='http.response.body':
                        await queue.put(message.get('body',b''))
                        if not message.get('more_body',False):await queue.put(None)
                async def serve():
                    try:
                        with patch.dict(os.environ,{'CARME_TOKEN':outer.tokens['alice'],'CARME_ACCOUNT_ID':'alice'}):
                            await outer.apps['alice'](scope,receive,send)
                    finally:
                        started.set();await queue.put(None)
                task=asyncio.create_task(serve())
                await started.wait()
                if 'status' not in meta:
                    await task
                    raise AssertionError('Control did not start response')
                class Body(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        while True:
                            chunk=await queue.get()
                            if chunk is None:break
                            if chunk:yield chunk
                    async def aclose(self):
                        disconnected.set()
                        if not task.done():task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                return httpx.Response(meta['status'],headers=meta['headers'],stream=Body())
        with TestClient(create_gateway(config,transport=LiveTransport()),base_url=config['origin']) as c:
            token=auth.GatewayTests.cf_token(self,exp=int(time.time())+3)
            headers={'Origin':config['origin'],'Cf-Access-Jwt-Assertion':token}
            response=c.post('/api/visitor/alice/login',json={'username':self.visitors['alice']['username'],
                'password':auth.PASSWORD,'conversation_id':self.cid},headers=headers)
            self.assertEqual(response.status_code,200,response.text)
            r=c.get(self.prefix+'/events',headers=headers)
            self.assertEqual(r.status_code,200)
            self.assertIn('PUBLIC_NEW',r.text)
            for marker in ('HIDDEN','PRIVATE','RAW_EVENT_SECRET','OTHER_GROUP'):
                self.assertNotIn(marker,r.text)
            self.assertEqual(r.headers['cache-control'],'no-store')

    def test_peer_connection_revokes_idle_stream(self):
        from concurrent.futures import ThreadPoolExecutor
        from carme.store import Store
        peer=Store(self.s.path)
        called=False
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                def revoke(chunk):
                    nonlocal called
                    if called:return
                    called=True
                    pool.submit(peer.update_visitor,self.cid,self.visitors['alice']['id'],'remove',actor_key='owner',
                        expected_revision=peer.get_conversation(self.cid)['access_revision']).result(timeout=1)
                start=time.monotonic()
                asyncio.run(self.capture_stream(self.prefix+'/events',revoke))
                self.assertLess(time.monotonic()-start,1.5)
        finally:
            peer.close()
        self.assertEqual(self.get('/messages').status_code,401)

    def test_live_sse_idle_revocation_and_history_change(self):
        for action in ('history', 'remove'):
            called = False
            def revoke(chunk):
                nonlocal called
                if not called:
                    called = True
                    self.change(action, **({'allow_history':True} if action=='history' else {}))
            started=time.monotonic()
            content, _ = asyncio.run(self.capture_stream(self.prefix+'/events', revoke))
            self.assertLess(time.monotonic()-started, 1.5)
            self.assertTrue(called); self.assertNotIn(b'HIDDEN',content)

    def test_live_download_stops_after_revoke(self):
        big = save_file(self.s, self.cid, 'large.txt', b'A'*131072, message_id=self.mid)
        calls = 0
        def revoke(chunk):
            nonlocal calls
            calls += 1
            self.change('remove')
        content, sent = asyncio.run(self.capture_stream(self.prefix+'/attachments/'+big['id']+'/download', revoke))
        self.assertEqual(calls,1); self.assertEqual(len(content),32768)
        self.assertEqual(sent[0]['status'],200)

    def test_history_revocation_stops_old_attachment_download(self):
        self.change('history',allow_history=True)
        big=save_file(self.s,self.cid,'old-large.txt',b'H'*131072,message_id=self.old['message_id'])
        def revoke(chunk):self.change('history',allow_history=False)
        content,_=asyncio.run(self.capture_stream(self.prefix+'/attachments/'+big['id']+'/download',revoke))
        self.assertEqual(len(content),32768)
        self.assertEqual(self.get('/attachments/'+big['id']).status_code,404)

    def test_logout_and_password_reset_close_existing_streams(self):
        for action in ('logout','reset_password'):
            self.assertEqual(self.login().status_code,200)
            called=False
            def revoke(chunk):
                nonlocal called
                if called:return
                called=True
                if action=='logout':self.s.revoke_visitor_session(self.client.cookies['carme_visitor'])
                else:self.change('reset_password',password_hash=self.hashed)
            start=time.monotonic()
            asyncio.run(self.capture_stream(self.prefix+'/events',revoke))
            self.assertLess(time.monotonic()-start,1.5)
            self.assertEqual(self.get('/messages').status_code,401)

    def test_download_closes_when_binding_deleted(self):
        big = save_file(self.s, self.cid, 'large.txt', b'A'*131072, message_id=self.mid)
        def delete(chunk):
            self.s._write('DELETE FROM attachments WHERE id=?',(big['id'],))
        content,_=asyncio.run(self.capture_stream(self.prefix+'/attachments/'+big['id']+'/download',delete))
        self.assertEqual(len(content),32768)

    def test_avatar_requires_current_group_membership(self):
        name='a'*32+'.webp'; directory=self.s.path.parent/'avatars'; directory.mkdir(exist_ok=True)
        (directory/name).write_bytes(b'synthetic-avatar')
        self.members['a'].avatar={'kind':'image','file':name,'private':'PRIVATE_AVATAR_METADATA'}
        self.members['outside'].avatar={'kind':'image','file':name}
        self.members['b'].avatar={'kind':'bot','shape':'circle','color':'#112233','private':'PRIVATE_SHAPE'}
        r=self.get(); self.assertNotIn('PRIVATE',r.text)
        self.assertEqual(next(m for m in r.json()['conversation']['members'] if m['id']=='b')['avatar'],
                         {'kind':'bot','shape':'circle','color':'#112233'})
        self.assertEqual(self.get('/avatars/a').content,b'synthetic-avatar')
        self.assertEqual(self.get('/avatars/outside').status_code,404)
        self.s._write('UPDATE conversations SET agent_ids=? WHERE id=?',(json.dumps(['b']),self.cid))
        self.assertEqual(self.get('/avatars/a').status_code,404)

    def test_gateway_stream_rechecks_account_version(self):
        outer=self
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'PUBLIC_FIRST'
                with database(outer.home) as db:
                    db.execute("UPDATE users SET version=version+1 WHERE account='alice'")
                yield b'MUST_NOT_DELIVER'
        def backend(request):
            self.assertNotIn('authorization',request.headers)
            self.assertNotIn('cookie',request.headers)
            return httpx.Response(200,stream=Bytes(),headers={'Content-Type':'text/event-stream'})
        with TestClient(create_gateway(self.config,transport=httpx.MockTransport(backend)),base_url=self.config['origin']) as c:
            c.cookies.set('carme_visitor',self.client.cookies['carme_visitor'],path='/api/visitor')
            r=c.get(self.prefix+'/events')
            self.assertEqual(r.content,b'PUBLIC_FIRST')

    def test_stream_limit_and_cleanup(self):
        async def run():
            path=self.prefix+'/events'; h=self.proof_headers(path=path)
            scope={'type':'http','method':'GET','scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'',
                'headers':[(k.lower().encode(),v.encode()) for k,v in h.items()], 'app':self.apps['alice'],
                'client':('127.0.0.1',1),'server':('localhost',80)}
            async def receive():return {'type':'http.request','body':b''}
            from carme.api.routes import build_router
            router=build_router(self.apps['alice'].state.config,self.s,None)
            endpoint=next(r.endpoint for r in router.routes if getattr(r,'path','')=='/api/visitor/{account}/conversations/{conversation_id}/events')
            with patch.dict(os.environ,{'CARME_TOKEN':self.tokens['alice'],'CARME_ACCOUNT_ID':'alice'}):
                a=await endpoint('alice',self.cid,Request(dict(scope),receive))
                b=await endpoint('alice',self.cid,Request(dict(scope),receive))
                with self.assertRaises(HTTPException) as exc:
                    await endpoint('alice',self.cid,Request(dict(scope),receive))
                self.assertEqual(exc.exception.status_code,429)
                await a.background();await b.background()
                c=await endpoint('alice',self.cid,Request(dict(scope),receive));await c.background()
        asyncio.run(run())


if __name__=='__main__':unittest.main()
