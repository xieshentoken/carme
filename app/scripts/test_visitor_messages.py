"""M4 human-only posting, immutable task provenance, and closed execution boundary."""
import asyncio
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, FastAPI
import test_visitor_auth as auth
from carme.api.routes import build_router, require_token
from carme.bus import EventBus
from carme.store import Store
from test_runtime import setup, settle
from carme.llm import LLMResponse


class VisitorMessageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):auth.VisitorAuthTests.setUpClass.__func__(cls)
    headers=auth.VisitorAuthTests.headers
    login=auth.VisitorAuthTests.login
    change=auth.VisitorAuthTests.change
    tearDown=auth.VisitorAuthTests.tearDown

    def setUp(self):
        auth.VisitorAuthTests.setUp(self)
        self.s=self.stores['alice'];self.cid=self.groups['alice']
        self.bus=EventBus()
        async def unavailable(*args,**kwargs):raise ValueError('executor_unconfigured')
        for account in ('alice','bob'):
            old=self.apps[account]; app=FastAPI()
            app.state.config=old.state.config;app.state.store=old.state.store
            app.include_router(build_router(old.state.config,old.state.store,SimpleNamespace(bus=self.bus,submit_message=unavailable)),dependencies=[Depends(require_token)])
            self.apps[account]=app
        self.assertEqual(self.login().status_code,200)
        self.path=f'/api/visitor/alice/conversations/{self.cid}/messages'

    def post(self,request_id='same',content='Hello',**extra):
        return self.client.post(self.path,json={'request_id':request_id,'content':content,'mode':'message',**extra},headers=self.headers())

    def actor(self,v=None):
        v=v or self.s.get_visitor(self.cid,self.visitors['alice']['id'])
        return f"visitor:{v['id']}:{v['membership_version']}"

    def test_plain_message_signature_retry_and_atomic_event(self):
        r=self.post();self.assertEqual(r.status_code,200,r.text)
        self.assertEqual(r.json()['task_ids'],[])
        second=self.post();self.assertFalse(second.json()['created']);self.assertEqual(second.json()['message_id'],r.json()['message_id'])
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM tasks')['n'],0)
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM events')['n'],1)
        row=self.client.get(self.path).json()['messages'][0]
        self.assertEqual((row['sender_kind'],row['sender_id']),('visitor',self.visitors['alice']['id']))
        self.assertEqual(row['task_ids'],[])
        self.assertNotIn('secret',r.text)
        self.assertEqual(self.post(content='different').status_code,409)
        self.assertEqual(self.post(request_id='long',content='中文消息'*1500).status_code,200)

    def test_owner_and_three_visitors_retry_keys_are_distinct(self):
        first=self.post().json()['message_id']
        ids=[first]
        for i in range(2):
            v=self.s.create_visitor(self.cid,f'Guest{i}',self.hashed,actor_key='owner',expected_revision=self.s.get_conversation(self.cid)['access_revision'])
            self.assertEqual(self.login(username=v['username']).status_code,200)
            ids.append(self.post().json()['message_id'])
        ids.append(self.s.create_human_message(self.cid,'Hello','same',actor_key='owner')['message_id'])
        self.assertEqual(len(set(ids)),4)
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM tasks')['n'],0)

    def test_no_identity_options_upload_or_task_steering(self):
        turn=self.s.create_conversation_turn(self.cid,'a','owner active','owner')
        for extra in ({'actor_key':'owner'},{'sender_id':'owner'},{'agent_id':'a'}, {'active_task_id':turn['task_id']},
                      {'attachment_ids':[]},{'envelope':{}},{'project_snapshot':{}}):
            self.assertEqual(self.post(**extra).status_code,422)
        self.assertEqual(self.post(mode='task').status_code,409)
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM tasks')['n'],1)
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM conversation_messages')['n'],1)
        self.assertEqual(self.s.get_task(turn['task_id'])['goal'],'owner active')

    def test_scope_csrf_reset_and_reinvite(self):
        data={'content':'Hello','request_id':'same','mode':'message'}
        self.assertEqual(self.client.post(self.path,json=data,headers={'Origin':self.config['origin']}).status_code,403)
        other=self.s.create_conversation(['a','b'])['id']
        self.assertEqual(self.client.post(f'/api/visitor/alice/conversations/{other}/messages',json=data,headers=self.headers()).status_code,404)
        first=self.post().json()['message_id']
        self.change('reset_password',password_hash=self.hashed)
        self.assertEqual(self.post().status_code,401)
        self.assertEqual(self.login().status_code,200)
        self.assertEqual(self.post().json()['message_id'],first)
        self.change('remove');self.change('reinvite',password_hash=self.hashed)
        self.assertEqual(self.login().status_code,200)
        self.assertNotEqual(self.post().json()['message_id'],first)

    def test_message_rate_limit_and_retries(self):
        for n in range(20):self.assertEqual(self.post(request_id=str(n)).status_code,200)
        self.assertEqual(self.post(request_id='21').status_code,429)
        self.assertEqual(self.post(request_id='1').status_code,200)
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM conversation_messages')['n'],20)

    def test_storage_concurrent_retry_and_transaction_rollback(self):
        peers=[Store(self.s.path),Store(self.s.path)]
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda s:s.create_human_message(self.cid,'same','concurrent',actor_key=self.actor()),peers))
            self.assertEqual(sum(r['created'] for r in results),1)
            self.assertEqual(len({r['message_id'] for r in results}),1)
        finally:
            for s in peers:s.close()
        events=self.s._query_one('SELECT COUNT(*) n FROM events')['n']
        with self.assertRaises(RuntimeError):
            with self.s.transaction():
                self.s.create_human_message(self.cid,'rollback','rollback',actor_key=self.actor())
                raise RuntimeError('synthetic fault')
        self.assertIsNone(self.s.get_conversation_request(self.cid,'rollback',actor_key=self.actor()))
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM events')['n'],events)

    def test_storage_origin_cannot_be_forged_and_children_inherit(self):
        root=self.s.create_conversation_turn(self.cid,'a','request','origin',actor_key=self.actor(),
            recipients={'a':{'origin':{'sender_id':'owner'}},'b':{}})
        origins=[json.loads(self.s.get_task(t)['meta'])['origin'] for t in root['task_ids']]
        self.assertEqual(origins[0],origins[1]);origin=origins[0]
        self.assertEqual(origin['sender_id'],self.visitors['alice']['id'])
        self.assertEqual(origin['conversation_id'],self.cid)
        self.assertEqual(origin['context_mode'],'visitor_group')
        child=self.s.create_task('b','child',parent_id=root['task_id'],meta={'origin':{'sender_id':'owner'}})
        grandchild=self.s.create_task('a','grandchild',parent_id=child)
        for tid in (child,grandchild):
            self.assertEqual(json.loads(self.s.get_task(tid)['meta'])['origin'],origin)
            self.assertEqual(self.s.get_task(tid)['conversation_id'],self.cid)
        with self.assertRaisesRegex(ValueError,'task_origin_immutable'):
            self.s.update_task_meta(child,{'origin':{'sender_id':'owner'}})
        other=self.s.create_conversation(['a','b'])['id']
        with self.assertRaisesRegex(ValueError,'child_conversation_conflict'):
            self.s.create_task('a','cross group',parent_id=child,conversation_id=other)

    def test_modes_share_retry_namespace_without_conversion(self):
        self.s.create_human_message(self.cid,'plain','mode')
        with self.assertRaisesRegex(ValueError,'request_id_mode_conflict'):
            self.s.create_conversation_turn(self.cid,'a','plain','mode')
        self.s.create_conversation_turn(self.cid,'a','task','task-mode')
        with self.assertRaisesRegex(ValueError,'request_id_mode_or_content_conflict'):
            self.s.create_human_message(self.cid,'task','task-mode')


class RuntimeBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='carme-m4-visitor-')
        self.runtime,self.s,self.gateway=setup(Path(self.tmp.name)/'runtime',[LLMResponse(text='owner OK'),LLMResponse(text='writer OK')])
        self.cid=self.s.create_conversation(['chief','writer'])['id']
        self.hash='scrypt$131072$8$1$'+'0'*32+'$'+'0'*64

    async def asyncTearDown(self):
        await self.runtime.shutdown();self.s.close();self.tmp.cleanup()

    def visitor(self):
        return self.s.create_visitor(self.cid,'Guest',self.hash,actor_key='owner',expected_revision=self.s.get_conversation(self.cid)['access_revision'])

    async def test_plain_owner_message_never_calls_model_even_with_mention(self):
        r=await self.runtime.submit_message(self.cid,'@Chief text only','plain',mode='message')
        self.assertEqual(r['task_ids'],[]);self.assertFalse(self.runtime._jobs);self.assertEqual(self.gateway.calls,[])
        retry=await self.runtime.submit_message(self.cid,'@Chief text only','plain',mode='message')
        self.assertFalse(retry['created'])
        with self.assertRaisesRegex(ValueError,'request_id_mode_conflict'):
            await self.runtime.submit_message(self.cid,'@Chief text only','plain')

    async def test_owner_group_routing_and_origin_preserved(self):
        result=await self.runtime.submit_message(self.cid,'all reply','owner')
        await settle(self.runtime)
        self.assertEqual(len(result['task_ids']),2)
        self.assertEqual(len(self.gateway.calls),2)
        for tid in result['task_ids']:
            origin=json.loads(self.s.get_task(tid)['meta'])['origin']
            self.assertEqual(origin['actor_key'],'owner');self.assertEqual(origin['context_mode'],'owner')
            self.assertEqual(origin['conversation_id'],self.cid)

    async def test_visitor_and_owner_in_shared_group_cannot_execute(self):
        v=self.visitor();actor=f"visitor:{v['id']}:{v['membership_version']}"
        for who in ('owner',actor):
            with self.assertRaisesRegex(ValueError,'visitor_container_required'):
                await self.runtime.submit_message(self.cid,'AI request','task-'+who,actor_key=who)
        plain=await self.runtime.submit_message(self.cid,'only human','plain',actor_key=actor,mode='message')
        self.assertEqual(plain['task_ids'],[])
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM tasks')['n'],0)
        self.assertEqual(self.gateway.calls,[]);self.assertFalse(self.runtime._jobs)

    async def test_restored_tasks_and_child_entry_fail_closed_after_removal(self):
        v=self.visitor();actor=f"visitor:{v['id']}:{v['membership_version']}"
        root=self.s.create_conversation_turn(self.cid,'chief','synthetic staged request','future',actor_key=actor)
        self.s.update_visitor(self.cid,v['id'],'remove',actor_key='owner',expected_revision=self.s.get_conversation(self.cid)['access_revision'])
        with self.assertRaises((ValueError,RuntimeError)):
            self.runtime.check_task_policy(root['task_id'])
        with self.assertRaises((ValueError,RuntimeError)):
            await self.runtime.submit('writer','child',parent_id=root['task_id'])
        with self.assertRaises((ValueError,RuntimeError)):
            await self.runtime._delegate(root['task_id'],'chief','writer','child','child',0)
        self.assertEqual(self.gateway.calls,[])
        self.assertEqual(self.s._query_one('SELECT COUNT(*) n FROM tasks')['n'],1)


if __name__=='__main__':unittest.main()
