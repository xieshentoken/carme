"""M5 common-context components; public execution remains gated until M6."""
import asyncio
import base64
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_runtime import setup
from carme.agents.base import Agent
from carme.attachments import archive_binary, save_file, file_path
from carme.llm import LLMResponse
from carme.tools.base import ToolContext
from carme.security import password_hash

class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='carme-m5-');self.root=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'CARME_ARTIFACTS_DIR':str(self.root/'cas')});self.env.start()
        self.r,self.s,self.g=setup(self.root/'home',[])
        self.cid=self.s.create_conversation(['chief','writer'])['id']
        self.spec=self.r.config.agents.get('chief');self.spec.prompt='PRIVATE_PROMPT_SENTINEL'
        self.spec.tools=['files','exec','memory','team','skill','mcp','desktop','computer','browser']
        self.spec.execution_target='container';self.spec.execution_target_id='action'
        self.r.config.isolation={'targets':{'action':{}},'desktop':{'image_digest':'sha256:'+'a'*64}}
        self.r.config.browser.enabled=True
        self.old=self.s.create_conversation_turn(self.cid,'chief','OLD_HISTORY_SENTINEL','old')
        self.s.save_summary(self.cid,'PRIVATE_SUMMARY_SENTINEL',1,'fixture')
        self.s.remember('chief','secret','PRIVATE_MEMORY_SENTINEL')
        self.oldfile=archive_binary(self.s,self.cid,'old.txt',b'PRIVATE_FILE',task_id=self.old['task_id'])
        self.s._write('UPDATE attachments SET message_id=? WHERE id=?',(self.old['message_id'],self.oldfile['id']))
        self.v=self.s.create_visitor(self.cid,'Visitor',password_hash('synthetic-password'),actor_key='owner',expected_revision=0)
        self.actor=f"visitor:{self.v['id']}:1"
    async def asyncTearDown(self):
        await self.r.shutdown();self.s.close();self.env.stop();self.temp.cleanup()
    def task(self,goal='current',actor='owner',bot='chief'):
        return self.s.create_conversation_turn(self.cid,bot,goal,'r'+str(self.s._query_one('SELECT COUNT(*) n FROM tasks')['n']),actor_key=actor)['task_id']
    def policy(self,tid):
        return self.r.common_policy(tid,self.r.policy_for('chief',target='container',node={}))
    def change(self,action,**kwargs):
        return self.s.update_visitor(self.cid,self.v['id'],action,actor_key='owner',expected_revision=self.s.get_conversation(self.cid)['access_revision'],**kwargs)
    async def test_owner_visitor_common_history_and_memory(self):
        self.s.create_human_message(self.cid,'PUBLIC_NEW','plain',actor_key=self.actor)
        for actor in ('owner',self.actor):
            tid=self.task(actor=actor);history=await self.r._history(self.s.get_task(tid),self.spec)
            raw=json.dumps(history);self.assertIn('PUBLIC_NEW',raw);self.assertNotIn('SENTINEL',raw)
            self.assertEqual(self.s.memory_context('chief',task_id=tid),('',[]))
        self.assertIn('PRIVATE_MEMORY_SENTINEL',self.s.memory_context('chief')[0])
    async def test_strictest_of_three_visitors(self):
        self.s.create_human_message(self.cid,'between','one')
        v2=self.s.create_visitor(self.cid,'Two',password_hash('synthetic-password'),actor_key='owner',expected_revision=1)
        self.s.create_human_message(self.cid,'after-two','two')
        self.s.create_visitor(self.cid,'Three',password_hash('synthetic-password'),actor_key='owner',expected_revision=2)
        self.s.create_human_message(self.cid,'all-see','three')
        tid=self.task();text=json.dumps(await self.r._history(self.s.get_task(tid),self.spec))
        self.assertIn('all-see',text);self.assertNotIn('between',text);self.assertNotIn('after-two',text)
    async def test_history_grant_rebuilds_context_and_rejects_old(self):
        old=self.task();self.change('history',allow_history=True)
        with self.assertRaises(ValueError):self.s.task_context(old)
        new=self.task();history=await self.r._history(self.s.get_task(new),self.spec)
        self.assertIn('OLD_HISTORY_SENTINEL',json.dumps(history));self.assertNotIn('PRIVATE_SUMMARY_SENTINEL',json.dumps(history))
    async def test_pi_epochs_rotate_on_join_leave_reinvite(self):
        spec=copy.deepcopy(self.spec);spec.engine='pi'
        tid=self.task();await self.r._history(self.s.get_task(tid),spec)
        meta=json.loads(self.s.get_task(tid)['meta']);epoch=meta['pi_session']['epoch']
        path=self.s.pi_session_directory(self.cid,'chief',epoch)
        self.assertNotIn('SENTINEL',(path/'bootstrap.json').read_text())
        (path/'session.jsonl').write_text('old native state')
        self.s.create_human_message(self.cid,'PLAIN_INCREMENT','plain')
        newer=self.task();await self.r._history(self.s.get_task(newer),spec)
        self.assertIn('PLAIN_INCREMENT',(path/'external.json').read_text())
        self.change('remove')
        with self.assertRaises(ValueError):self.s.task_context(tid)
        left=self.task();await self.r._history(self.s.get_task(left),spec)
        self.assertNotEqual(json.loads(self.s.get_task(left)['meta'])['pi_session']['epoch'],epoch)
        self.change('reinvite',password_hash=password_hash('new-synthetic-password'))
        again=self.task();self.assertNotEqual(self.s.context_epoch(again),epoch)
    async def test_summary_compaction_only_filtered_history(self):
        for i in range(55):self.s.create_human_message(self.cid,'PUBLIC '+str(i),'p'+str(i))
        self.g.responses=[LLMResponse(text='safe scoped summary',provider='fixture',model='fixture')]*3
        tid=self.task();history=await self.r._history(self.s.get_task(tid),self.spec)
        self.assertNotIn('SENTINEL',json.dumps(self.g.calls));self.assertIn('safe scoped summary',json.dumps(history))
        self.assertEqual(self.s.get_summary(self.cid)['content'],'PRIVATE_SUMMARY_SENTINEL')
        self.assertIsNotNone(self.s.get_summary(self.s.task_summary_key(tid)))
    async def test_checkpoint_bound_to_context_and_legacy_denied(self):
        tid=self.task();self.s.checkpoint(tid,{'messages':[{'content':'safe'}]})
        self.assertIn('context_epoch',self.s.checkpoint(tid))
        self.change('history',allow_history=True)
        with self.assertRaises(ValueError):self.s.checkpoint(tid)
    async def test_hidden_attachment_cannot_be_granted_back(self):
        oldfile=self.oldfile
        self.s._write('UPDATE attachments SET message_id=? WHERE id=?',(self.old['message_id'],oldfile['id']))
        tid=self.task();self.assertFalse(self.s.attachment_visible_to(self.s.get_task(tid),oldfile))
        self.s._write('INSERT OR IGNORE INTO attachment_shares VALUES (?,?,?,?,?)',(self.cid,oldfile['id'],'chief',tid,1))
        with self.assertRaisesRegex(ValueError,'common_scope'):self.s.artifact_access(tid,oldfile['id'])
        own=archive_binary(self.s,self.cid,'own.txt',b'42',task_id=tid)
        self.assertEqual(self.s.artifact_access(tid,own['id'])['id'],own['id'])
        newer=self.task()
        with self.assertRaises(ValueError):self.s.artifact_access(newer,own['id'])
    async def test_policy_intersection_no_private_tools(self):
        tid=self.task();p=self.policy(tid)
        self.assertIn('shell',p['tools']);self.assertIn('create_artifact',p['tools'])
        self.assertFalse(set(p['tools'])&{'recall','remember','use_skill','bot_computer','web_open','share_attachment'})
        self.assertEqual(p['browser_identity'],'none');self.assertFalse(p['local_autonomy'])
        with self.assertRaises(ValueError):self.r.common_policy(tid,{**p,'target':'ssh'})
    async def test_agent_private_prompt_memory_absent_and_public_identity_kept(self):
        tid=self.task();p=self.policy(tid);self.g.responses=[LLMResponse(text='safe')]
        agent=Agent(self.spec,self.r.config,self.g,self.r.registry,self.s,self.r.skills)
        await agent.run('current',task_id=tid,policy=p,history=await self.r._history(self.s.get_task(tid),self.spec))
        raw=json.dumps(self.g.calls);self.assertNotIn('SENTINEL',raw);self.assertIn('Chief',raw)
        self.assertEqual(self.spec.prompt,'PRIVATE_PROMPT_SENTINEL')
    async def test_registry_rejects_forged_policy_private_tools(self):
        tid=self.task();p=self.policy(tid)
        ctx=ToolContext(agent=self.spec,task_id=tid,store=self.s,extras={'policy':{**p,'tools':p['tools']+['recall']}})
        self.assertIn('common_capability_denied',await self.r.registry.execute(ctx,'recall',{}))
        self.change('remove');result=await self.r.registry.execute(ctx,'recall',{})
        self.assertNotIn('PRIVATE_MEMORY_SENTINEL',result)
    async def test_non_container_group_stays_closed(self):
        for actor in ('owner',self.actor):
            with self.assertRaisesRegex(ValueError,'visitor_container_required'):
                await self.r.submit_message(self.cid,'calculate','closed'+actor,actor_key=actor)
    async def test_queries_use_exact_plan_and_no_desktop_or_cookies(self):
        tid=self.task('公开查询：weather Taipei');p=self.policy(tid)
        ctx=ToolContext(agent=self.spec,task_id=tid,store=self.s,extras={'policy':p})
        requests=[]
        async def fetch(data,safety,check):
            requests.append(data);check()
            body=b'<a class="result__a" href="https://example.com/weather">Weather</a>' if 'duckduckgo' in data['url'] else b'<p>public weather</p>'
            return {'status':200,'headers':[],'body':base64.b64encode(body).decode()}
        with patch.object(self.r,'check_task_policy',side_effect=lambda t:self.s.task_context(t)),patch('carme.docker_browser.fetch_public',side_effect=fetch):
            result=await self.r.execution.common_query(ctx,'web_search',{'query':'weather Taipei'})
            self.assertIn('Weather',result)
            result=await self.r.execution.common_query(ctx,'fetch_page',{'url':'https://example.com/weather'})
            self.assertIn('public weather',result)
            for name,args in [('web_search',{'query':'PRIVATE expanded'}),('fetch_page',{'url':'https://evil.example/?secret=x'}),('fetch_page',{'url':'https://example.com/weather','render':True}),('web_open',{'url':'https://example.com'})]:
                with self.assertRaises(ValueError):await self.r.execution.common_query(ctx,name,args)
        self.assertEqual(len(requests),2);self.assertTrue(all(r['headers']=={} and r['body']=='' for r in requests))
        self.assertEqual(self.r.execution.browser_sessions,{})
    async def test_compaction_race_cannot_commit_stale_summary(self):
        for i in range(55):self.s.create_human_message(self.cid,'safe '+str(i),'p'+str(i))
        tid=self.task();key=self.s.task_summary_key(tid)
        async def response():
            self.change('history',allow_history=True)
            return LLMResponse(text='must not save',provider='fixture',model='fixture')
        self.g.responses=[response]
        with self.assertRaisesRegex(ValueError,'context_changed'):
            await self.r._history(self.s.get_task(tid),self.spec)
        self.assertIsNone(self.s.get_summary(key))
    async def test_agent_revalidates_after_model_and_refuses_stale_output(self):
        tid=self.task();p=self.policy(tid);published=[]
        async def response():
            self.change('remove')
            return LLMResponse(text='must not publish')
        self.g.responses=[response]
        async def emit(kind,payload):published.append((kind,payload))
        agent=Agent(self.spec,self.r.config,self.g,self.r.registry,self.s,self.r.skills)
        with self.assertRaisesRegex(ValueError,'context_changed'):
            await agent.run('current',task_id=tid,policy=p,emit=emit)
        self.assertFalse(any(k=='assistant.message' for k,p in published))
    async def test_envelope_narrows_and_excludes_hidden_inputs(self):
        _,meta=self.r._task_agent_snapshot('chief')
        turn=self.s.create_conversation_turn(self.cid,'chief','goal','envelope',recipients={'chief':meta})
        tid=turn['task_id'];self.r.bind_envelope(tid)
        saved=json.loads(self.s.get_task(tid)['meta'])
        self.assertEqual(saved['policy']['context_mode'],'visitor_group')
        self.assertEqual(saved['envelope']['permission_snapshot'],saved['policy'])
        self.assertEqual(self.s.memory_search('chief',task_id=tid),[])
    async def test_common_shell_refuses_non_docker_handle(self):
        tid=self.task();p=self.policy(tid)
        async def get():return SimpleNamespace(spec=SimpleNamespace(mode='local'))
        ctx=ToolContext(agent=self.spec,task_id=tid,store=self.s,sandbox_handle=SimpleNamespace(get=get),extras={'policy':p})
        text=await self.r.registry.execute(ctx,'shell',{'command':'echo should-not-run'})
        self.assertIn('visitor_container_required',text)

    async def test_execution_rejects_private_mounts_and_desktop(self):
        _,meta=self.r._task_agent_snapshot('chief')
        tid=self.s.create_conversation_turn(self.cid,'chief','goal','worker',recipients={'chief':meta})['task_id']
        self.r.bind_envelope(tid)
        ctx=ToolContext(agent=self.spec,task_id=tid,store=self.s)
        with self.assertRaisesRegex(ValueError,'common_desktop'):
            await self.r.execution.desktop_request('chief','status',ctx=ctx)
        with patch.object(self.r.execution,'key',return_value=b'synthetic'),patch.object(self.r,'check_task_policy',side_effect=lambda t:self.s.task_context(t)):
            for role,payload,code in [('browser',{},'common_worker'),('action',{'op':'snapshot'},'common_action'),
                                     ('action',{'op':'mcp'},'common_action'),('action',{'op':'stage_input','name':'skills/private/file'},'common_input')]:
                with self.assertRaisesRegex(ValueError,code):await self.r.execution.submit(tid,role,payload)

    async def test_common_stage_input_upload_and_archive_hash_binding(self):
        # Real upload storage keeps an empty legacy hash; the task grant still binds bytes.
        for archived in (False, True):
            with self.subTest(archived=archived):
                raw=b'public attachment'
                file=(archive_binary(self.s,self.cid,'input.txt',raw,task_id=self.task()) if archived
                      else save_file(self.s,self.cid,'input.txt',raw))
                self.assertEqual(bool(file.get('sha256')),archived)
                if archived:
                    message=self.s._query_one('SELECT id FROM conversation_messages WHERE task_id=?',(file['task_id'],))
                    self.s._write('UPDATE attachments SET message_id=? WHERE id=?',(message['id'],file['id']))
                _,meta=self.r._task_agent_snapshot('chief')
                tid=self.s.create_conversation_turn(self.cid,'chief','read input','input-'+str(archived),
                    recipients={'chief':meta},attachment_ids=[] if archived else [file['id']])['task_id']
                envelope=self.r.bind_envelope(tid,inputs=[file['id']])
                name='artifacts/'+file['id']+'/'+file['name']
                with patch.object(self.r.execution,'key',return_value=b'synthetic'),patch.object(self.r,'check_task_policy',side_effect=lambda t:self.s.task_context(t)):
                    # With no Broker, an accepted payload must reach the existing fail-closed relay gate.
                    with self.assertRaisesRegex(RuntimeError,'container_runner_unavailable'):
                        await self.r.execution.stage_input(tid,name,raw)
                    with self.assertRaisesRegex(ValueError,'common_input_denied'):
                        await self.r.execution.stage_input(tid,name,b'tampered payload')
                    with self.assertRaisesRegex(ValueError,'common_input_denied'):
                        await self.r.execution.stage_input(tid,name+'-wrong',raw)
                    # A visible file which was never bound as input must not be staged.
                    self.s.update_task_meta(tid,{'envelope':{**envelope,'input_artifacts':[]}})
                    with self.assertRaisesRegex(ValueError,'common_input_denied'):
                        await self.r.execution.stage_input(tid,name,raw)
                    self.s.update_task_meta(tid,{'envelope':envelope})
                    file_path(self.s,file['id']).write_bytes(b'tampered stored bytes')
                    with self.assertRaisesRegex(ValueError,'artifact_version_conflict'):
                        await self.r.execution.stage_input(tid,name,raw)

    async def test_query_redirect_does_not_follow(self):
        tid=self.task('公开查询：x');ctx=ToolContext(agent=self.spec,task_id=tid,store=self.s)
        async def redirect(*args,**kwargs):return {'status':302,'headers':[['location','https://evil.example']], 'body':''}
        with patch.object(self.r,'check_task_policy',side_effect=lambda t:self.s.task_context(t)),patch('carme.docker_browser.fetch_public',side_effect=redirect) as fetch:
            with self.assertRaisesRegex(ValueError,'redirect'):await self.r.execution.common_query(ctx,'web_search',{'query':'x'})
        self.assertEqual(fetch.call_count,1)

if __name__=='__main__':unittest.main(verbosity=2)
