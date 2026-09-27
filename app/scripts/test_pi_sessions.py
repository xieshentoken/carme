"""Native Pi session acceptance with a local synthetic model; no provider credentials or live data."""
from __future__ import annotations
import asyncio, json, os, tempfile, unittest, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['CARME_LOAD_ENV'] = '0'
from carme.engines import _run_cli_process, CliEngineError, CliRunResult
from carme.runtime import Runtime
from carme.store import Store

PI = Path(os.environ.get('CARME_TEST_PI', str(Path(__file__).resolve().parents[2] /
    'runtime/docker/toolchains/computer-use/node_modules/.bin/pi')))


class History(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='carme-history-')
        self.root = Path(self.tmp.name); self.store = Store(self.root/'state.db')
        self.cid = self.store.create_conversation(['a'])['id']
        self.runtime = SimpleNamespace(store=self.store, _meta=Runtime._meta)

    async def asyncTearDown(self):
        self.store.close(); self.tmp.cleanup()

    async def test_memory_update_keeps_history_and_revocation_keeps_user(self):
        old = self.store.create_task('a', 'old', conversation_id=self.cid)
        self.store.remember('a', 'pref', 'old preference')
        _, refs = self.store.memory_context('a')
        self.store.update_task_meta(old, {'memory_refs': refs})
        self.store.add_conversation_message(self.cid, 'a', 'user', 'USER_CONTEXT', task_id=old)
        self.store.add_conversation_message(self.cid, 'a', 'assistant', 'DERIVED_REPLY', task_id=old)
        self.store.finish_task(old, 'done')
        self.store.remember('a', 'pref', 'new preference')
        tid = self.store.create_task('a', 'next', conversation_id=self.cid)
        spec = SimpleNamespace(id='a', engine='api')
        history = await Runtime._history(self.runtime, self.store.get_task(tid), spec)
        self.assertEqual([m['content'] for m in history], ['USER_CONTEXT', 'DERIVED_REPLY'])
        self.store.forget('a', 'pref')
        history = await Runtime._history(self.runtime, self.store.get_task(tid), spec)
        self.assertEqual([m['content'] for m in history], ['USER_CONTEXT'])

    async def test_native_bootstrap_preserves_roles_and_purge_removes_sessions(self):
        old = self.store.create_task('a', 'old', conversation_id=self.cid)
        self.store.add_conversation_message(self.cid, 'a', 'user', 'INITIAL_GOAL', task_id=old)
        self.store.add_conversation_message(self.cid, 'a', 'assistant', 'INITIAL_REPLY', task_id=old)
        self.store.finish_task(old, 'done')
        tid = self.store.create_task('a', 'new', conversation_id=self.cid)
        self.assertEqual(await Runtime._history(self.runtime, self.store.get_task(tid), SimpleNamespace(id='a', engine='pi')), [])
        session = json.loads(self.store.get_task(tid)['meta'])['pi_session']
        path = self.store.pi_session_directory(self.cid, 'a', session['epoch'])
        self.assertEqual([{k:m[k] for k in ('role','content')} for m in json.loads((path/'bootstrap.json').read_text())], [
            {'role':'user','content':'INITIAL_GOAL'}, {'role':'assistant','content':'INITIAL_REPLY'}])
        self.store.finish_task(tid, 'done')
        self.store.update_conversation(self.cid, {'deleted': True})
        self.assertEqual(self.store.purge_conversations([self.cid])['file_cleanup_pending'], 0)
        self.assertFalse(path.exists())

    async def test_symlink_session_is_rejected(self):
        (self.root/'pi-sessions').symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.store.pi_session_directory(self.cid, 'a', 'a'*64)

    async def test_delegate_before_text_does_not_publish_an_empty_stream(self):
        from unittest.mock import patch, AsyncMock
        from carme.llm import LLMGateway
        gateway = SimpleNamespace(config=SimpleNamespace(isolation={'profiles': {}}))
        stream = AsyncMock()
        result = CliRunResult('pi', '', 0, yielded_tool={'id': 'native-call', 'name': 'delegate', 'arguments': {}})
        with patch('carme.llm.run_cli_engine', new=AsyncMock(return_value=result)):
            response = await LLMGateway._cli_chat(gateway, 'pi', 'fixture/model', [],
                system_extra='', effort='', on_stream=stream, native_session=True)
        self.assertEqual(response.tool_calls[0].id, 'native-call')
        stream.assert_not_awaited()

    async def test_native_summary_display_does_not_replace_api_summary(self):
        self.store.save_summary(self.cid, 'API_SUMMARY', 1, 'api-model')
        tid = self.store.create_task('a', 'old', conversation_id=self.cid)
        from carme.security import digest
        session = {'scope': self.cid, 'epoch': digest([])}
        self.store.remember('a', 'pref', 'old')
        _, refs = self.store.memory_context('a')
        summary = {'content': 'PI_SUMMARY', 'model': 'Pi', 'updated_at': 1}
        self.store.update_task_meta(tid, {'agent_engine': 'pi', 'pi_session': session,
            'pi_summary': summary, 'memory_refs': refs})
        next_id = self.store.create_task('a', 'next', conversation_id=self.cid)
        self.store.update_task_meta(next_id, {'agent_engine': 'pi', 'pi_session': session})
        self.assertEqual(self.store.visible_summary(self.cid), summary)
        self.assertEqual(self.store.get_summary(self.cid)['content'], 'API_SUMMARY')
        self.store.remember('a', 'pref', 'changed')
        self.assertEqual(self.store.visible_summary(self.cid), summary)
        self.store.forget('a', 'pref')
        self.assertIsNone(self.store.visible_summary(self.cid))
        self.store.update_task_meta(next_id, {'pi_session': {'scope': self.cid, 'epoch': digest([tid])},
            'pi_summary': {'content': 'REBUILT_WITHOUT_REVOKED_DATA'}})
        self.assertEqual(self.store.visible_summary(self.cid)['content'], 'REBUILT_WITHOUT_REVOKED_DATA')


@unittest.skipUnless(PI.exists(), 'set CARME_TEST_PI to pinned Pi 0.85.1 executable')
class NativePi(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='carme-native-pi-')
        self.root = Path(self.tmp.name); self.session_dir = self.root/'session'; self.session_dir.mkdir()
        self.requests = []; self.compactions = 0; self.mode = 'plain'; self.effects = 0
        self.failures_left = 0; self.failure_status = 503; self.fail_after_tool = False
        self.diagnostics = []; self.drops_left = 0; self.length_after_tool_count = 0
        self.server = await asyncio.start_server(self.client, '127.0.0.1', 0)
        self.base = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1'
        self.limits = {'context_window':32768, 'max_output_tokens':8192}

    async def asyncTearDown(self):
        self.server.close(); await self.server.wait_closed(); self.tmp.cleanup()

    async def client(self, reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            headers = dict(line.split(':',1) for line in header.decode().split('\r\n')[1:] if ':' in line)
            body = json.loads(await reader.readexactly(int(next(v for k,v in headers.items() if k.lower() == 'content-length'))))
            self.requests.append(body)
            system = '\n'.join(str(m['content']) for m in body['messages'] if m['role'] in {'system','developer'})
            summary = 'context summarization assistant' in system
            tool_results = [m for m in body['messages'] if m['role'] == 'tool']
            if self.failures_left and (not self.fail_after_tool or tool_results):
                self.failures_left -= 1
                data = json.dumps({'error':{'message':'Service unavailable' if self.failure_status == 503 else 'insufficient_quota' if self.failure_status == 402 else 'Unauthorized'}}).encode()
                writer.write(f'HTTP/1.1 {self.failure_status} Fault\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode()+data)
                await writer.drain(); return
            if self.drops_left:
                self.drops_left -= 1
                data = b'data: {"id":"fixture","choices":[{"index":0,"delta":{"content":"PARTIAL_NOT_FINAL"},"finish_reason":null}]}\n\n'
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: 99999\r\nConnection: close\r\n\r\n'+data)
                await writer.drain(); return
            if summary:
                self.compactions += 1
                delta = {'content':'## Goal\nRETAINED_GOAL\n## Progress\n- [x] PRIOR_SUCCESS\n## Next Steps\nContinue latest user request.'}
                finish = 'stop'
            elif self.mode in {'tool','artifact_length_once','artifact_length_always'} and not tool_results:
                delta = {'tool_calls':[{'index':0,'id':'native_call_1','type':'function',
                    'function':{'name':'carme_create_artifact' if self.mode.startswith('artifact_') else 'carme_recall',
                                'arguments':'{"name":"fixture.md","content":"SYNTHETIC_ARTIFACT"}' if self.mode.startswith('artifact_') else '{"key":"fixture"}'}}]}; finish = 'tool_calls'
            elif self.mode.startswith('artifact_') and (self.mode == 'artifact_length_always' or self.length_after_tool_count == 0):
                self.length_after_tool_count += 1; delta = {}; finish = 'length'
            elif self.mode == 'length':
                delta = {}; finish = 'length'
            elif self.mode == 'thinking_only':
                delta = {'reasoning_content':'synthetic private reasoning'}; finish = 'stop'
            else:
                delta = {'content':'NATIVE_REPLY_' + str(len(self.requests))}; finish = 'stop'
                if self.mode == 'thinking': delta['reasoning_content']='synthetic private reasoning'
            tokens = 50000 if self.mode=='length' and not summary else 7000 if self.mode == 'compact' and not summary else 50
            completion = 8192 if self.mode.startswith('artifact_') and finish == 'length' else 10
            chunks = [{'id':'fixture','object':'chat.completion.chunk','created':1,'model':body['model'],
                'choices':[{'index':0,'delta':delta,'finish_reason':None}]},
                {'id':'fixture','object':'chat.completion.chunk','created':1,'model':body['model'],
                 'choices':[{'index':0,'delta':{},'finish_reason':finish}],
                 'usage':{'prompt_tokens':tokens,'completion_tokens':completion,'total_tokens':tokens+completion}}]
            data = (''.join('data: '+json.dumps(chunk)+'\n\n' for chunk in chunks)+'data: [DONE]\n\n').encode()
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n'.encode()+data)
            await writer.drain()
        finally:
            writer.close(); await writer.wait_closed()

    async def run_pi(self, messages, task='task-a', execute=None):
        models = {'providers':{'carme':{'baseUrl':self.base,'api':'openai-completions','apiKey':'synthetic-fixture',
            'models':[{'id':'fixture','name':'fixture','reasoning':False,'input':['text'],
                'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0},
                'contextWindow':self.limits['context_window'],'maxTokens':self.limits['max_output_tokens']}]}}}
        tool_name = 'create_artifact' if self.mode.startswith('artifact_') else 'recall'
        tools = [{'type':'function','function':{'name':tool_name,'description':'fixture tool',
                  'parameters':{'type':'object','properties':{'key':{'type':'string'}}}}}] if execute else []
        return await _run_cli_process('pi','',binary=str(PI),model='carme/fixture',effort='off',timeout=40,
            runtime_models=models,tool_specs=tools,tool_execute=execute,on_diagnostic=self.capture_diagnostic,native_context={
                'session_dir':str(self.session_dir),'task_id':task,'messages':messages,
                'system_prompt':'SYNTHETIC_MANAGED_SYSTEM', 'model_limits':self.limits})

    async def capture_diagnostic(self, event):
        self.diagnostics.append(event)

    async def test_thinking_is_not_misrepresented_as_an_image_or_cached_answer(self):
        self.mode='thinking'
        messages=[{'role':'user','content':'WITH_REASONING'}]
        first=await self.run_pi(messages)
        repeated=await self.run_pi(messages)
        self.assertEqual(first.text, repeated.text)
        self.assertEqual(first.text,'NATIVE_REPLY_1')
        self.assertNotIn('图片',repeated.text)
        self.assertNotIn('synthetic private reasoning',repeated.text)
        self.assertEqual(len(self.requests),1)

    async def test_thinking_without_visible_text_is_not_marked_complete(self):
        self.mode='thinking_only'
        with self.assertRaisesRegex(CliEngineError,'pi_empty_response'):
            await self.run_pi([{'role':'user','content':'NO_VISIBLE_REPLY'}])
        rows=[json.loads(line) for line in (self.session_dir/'session.jsonl').read_text().splitlines()]
        self.assertFalse(any(r.get('customType')=='carme_complete' for r in rows))

    async def test_truncated_turn_never_reuses_previous_success(self):
        await self.run_pi([{'role':'user','content':'OLD_GOOD_TURN'}])
        self.mode='length'
        with self.assertRaisesRegex(CliEngineError,'pi_turn_incomplete'):
            await self.run_pi([{'role':'user','content':'NEW_TRUNCATED_TURN'}],task='length-task')
        rows=[json.loads(line) for line in (self.session_dir/'session.jsonl').read_text().splitlines()]
        self.assertEqual(sum(r.get('customType')=='carme_complete' for r in rows),1)

    async def test_length_after_artifact_recovers_once_without_repeating_write(self):
        self.mode='artifact_length_once'; artifact=self.root/'fixture.md'
        async def execute(name, args, call_id):
            self.effects += 1
            self.assertEqual(name,'create_artifact')
            artifact.write_text(args['content'])
            return 'ARCHIVED_ARTIFACT_RECEIPT'
        result=await self.run_pi([{'role':'user','content':'CREATE_FIXTURE_ARTIFACT'}],execute=execute)
        self.assertEqual(self.effects,1)
        self.assertEqual(artifact.read_text(),'SYNTHETIC_ARTIFACT')
        self.assertIn('NATIVE_REPLY_',result.text)
        self.assertEqual(sum(d['event']=='turn_recovery_start' for d in self.diagnostics),1)
        self.assertFalse(any('content' in d or 'arguments' in d or 'error' in d for d in self.diagnostics))

    async def test_length_after_artifact_persistent_failure_is_bounded(self):
        self.mode='artifact_length_always'; artifact=self.root/'fixture.md'
        async def execute(name, args, call_id):
            self.effects += 1; artifact.write_text(args['content']); return 'ARCHIVED_ARTIFACT_RECEIPT'
        with self.assertRaisesRegex(CliEngineError,'pi_turn_incomplete'):
            await self.run_pi([{'role':'user','content':'PERSISTENT_LENGTH'}],execute=execute)
        self.assertEqual(self.effects,1)
        self.assertEqual(artifact.read_text(),'SYNTHETIC_ARTIFACT')
        self.assertEqual(sum(d['event']=='turn_recovery_start' for d in self.diagnostics),1)
        self.assertLessEqual(len(self.requests),3)

    async def test_native_retry_recovers_two_transient_failures(self):
        self.failures_left = 2
        result = await self.run_pi([{'role':'user','content':'TRANSIENT_FIXTURE'}])
        self.assertEqual(len(self.requests), 3)
        self.assertIn('NATIVE_REPLY_3', result.text)
        self.assertEqual([r['attempt'] for r in self.diagnostics if r['event']=='auto_retry_start'], [1,2])
        self.assertFalse(any('error' in r or 'errorMessage' in r for r in self.diagnostics))

    async def test_retries_never_repeat_completed_tool(self):
        self.mode = 'tool'; self.failures_left = 1; self.fail_after_tool = True
        async def execute(name, args, call_id):
            self.effects += 1; return 'COMPLETED_SIDE_EFFECT_RECEIPT'
        await self.run_pi([{'role':'user','content':'TOOL_THEN_FAULT'}], execute=execute)
        self.assertEqual(self.effects, 1)
        self.assertEqual(len(self.requests), 3)
        self.assertIn('COMPLETED_SIDE_EFFECT_RECEIPT', json.dumps(self.requests[-1]))

    async def test_auth_and_quota_are_not_retried(self):
        for status in [401,402]:
            self.failure_status = status; self.failures_left = 1; before = len(self.requests)
            with self.assertRaises(CliEngineError):
                await self.run_pi([{'role':'user','content':'DENIED_FIXTURE'}], task='status-'+str(status))
            self.assertEqual(len(self.requests)-before, 1)

    async def test_midstream_disconnect_recovers_and_drops_partial_answer(self):
        self.drops_left = 1
        result = await self.run_pi([{'role':'user','content':'DROP_STREAM'}])
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn('PARTIAL_NOT_FINAL', result.text)

    async def test_retry_budget_is_bounded(self):
        self.failures_left = 10
        with self.assertRaises(CliEngineError):
            await self.run_pi([{'role':'user','content':'PERMANENT_OUTAGE'}])
        self.assertEqual(len(self.requests), 4)

    async def test_large_cjk_import_compacts_before_first_normal_inference(self):
        self.limits = {'context_window':8192,'max_output_tokens':1024}
        (self.session_dir/'bootstrap.json').write_text(json.dumps([
            {'role':'user','content':'旅行要求与已经执行的操作记录。'*1500},
            {'role':'assistant','content':'已记住'}], ensure_ascii=False))
        await self.run_pi([{'role':'user','content':'KEEP_THE_GOAL'}])
        self.assertGreaterEqual(self.compactions, 1)
        self.assertIn('context summarization assistant', json.dumps(self.requests[0]))
        self.assertIn('KEEP_THE_GOAL', json.dumps(self.requests[-1]))

    async def test_turns_restore_native_roles_and_repeated_request_does_not_infer_twice(self):
        first = [{'role':'user','content':'MY_INITIAL_GOAL'}]
        await self.run_pi(first)
        before = len(self.requests)
        await self.run_pi(first)
        self.assertEqual(len(self.requests), before)
        await self.run_pi([{'role':'user','content':'FOLLOW_UP'}], 'task-b')
        messages = self.requests[-1]['messages']
        self.assertEqual(sum(m['role']=='user' and 'MY_INITIAL_GOAL' in str(m['content']) for m in messages), 1)
        self.assertTrue(any(m['role']=='assistant' and 'NATIVE_REPLY_1' in str(m['content']) for m in messages))
        self.assertNotIn('[user]', json.dumps(messages))

    async def test_tool_receipt_survives_worker_restart(self):
        self.mode = 'tool'
        async def execute(name, arguments, call_id):
            self.effects += 1; self.assertEqual(call_id, 'native_call_1'); return 'VERIFIED_TOOL_RESULT'
        await self.run_pi([{'role':'user','content':'DO_TOOL'}], execute=execute)
        await self.run_pi([{'role':'user','content':'WHAT_DID_IT_RETURN'}], 'task-b', execute)
        self.assertEqual(self.effects, 1)
        messages = self.requests[-1]['messages']
        self.assertTrue(any(m['role']=='tool' and 'VERIFIED_TOOL_RESULT' in str(m['content']) for m in messages))

    async def test_interrupted_tool_reconciles_exact_native_call_without_repeating_effect(self):
        self.mode = 'tool'; reached = asyncio.Event()
        async def execute(name, arguments, call_id):
            self.effects += 1; reached.set(); await asyncio.Event().wait()
        first = [{'role':'user','content':'INTERRUPTED_GOAL'}]
        work = asyncio.create_task(self.run_pi(first, execute=execute))
        await asyncio.wait_for(reached.wait(), 15)
        work.cancel()
        done, pending = await asyncio.wait({work}, timeout=8)
        if pending:
            for task in asyncio.all_tasks(): task.print_stack()
            self.fail('cancelled Pi worker did not finish')
        await asyncio.gather(work, return_exceptions=True)
        recovered = first + [{'role':'assistant','content':'','tool_calls':[{'id':'native_call_1','type':'function',
            'function':{'name':'recall','arguments':{'key':'fixture'}}}]},
            {'role':'tool','tool_call_id':'native_call_1','name':'recall','content':'RECONCILED_RECEIPT'}]
        async def deny(*args): self.fail('completed effect must not repeat')
        await self.run_pi(recovered, execute=deny)
        self.assertEqual(self.effects, 1)
        messages = self.requests[-1]['messages']
        self.assertEqual(sum(m['role']=='user' and 'INTERRUPTED_GOAL' in str(m['content']) for m in messages), 1)
        self.assertTrue(any(m['role']=='tool' and 'RECONCILED_RECEIPT' in str(m['content']) for m in messages))

    async def test_native_compaction_keeps_summary_and_recent_request(self):
        self.mode = 'compact'; self.limits = {'context_window':8192,'max_output_tokens':1024}
        history = []
        for i in range(30):
            history += [{'role':'user','content':'RETAINED_GOAL ' + ('history '+str(i)+' ')*150},
                        {'role':'assistant','content':'PRIOR_SUCCESS '+('result ')*150}]
        (self.session_dir/'bootstrap.json').write_text(json.dumps(history))
        result = await self.run_pi([{'role':'user','content':'LATEST_REQUEST'}])
        self.assertGreater(self.compactions, 0)
        self.assertLessEqual(self.compactions,3)
        self.assertTrue(any(d['event']=='compaction_start' for d in self.diagnostics))
        self.assertTrue(any(d['event']=='compaction_end' for d in self.diagnostics))
        self.assertTrue(all(type(d['elapsed_ms']) is int and d['elapsed_ms'] >= 0 for d in self.diagnostics
                            if d['event']=='compaction_end'))
        self.assertIn('RETAINED_GOAL', result.context_summary['content'])
        self.assertGreater(result.context_summary['updated_at'], 0)
        self.mode = 'plain'
        await self.run_pi([{'role':'user','content':'FOLLOW_UP_AFTER_COMPACTION'}], 'task-b')
        body = json.dumps(self.requests[-1]['messages'])
        self.assertIn('RETAINED_GOAL', body); self.assertIn('FOLLOW_UP_AFTER_COMPACTION', body)
        self.assertIn('"type":"compaction"', (self.session_dir/'session.jsonl').read_text())

    async def test_group_rounds_use_member_native_sessions(self):
        from test_runtime import setup
        from unittest.mock import patch
        runtime, store, _ = setup(self.root/'group-runtime', [])
        try:
            for spec in runtime.config.agents.agents.values(): spec.engine='pi'
            cid=store.create_conversation(['chief','writer'])['id']
            with patch.object(runtime,'_schedule'):
                first=await runtime.submit_message(cid,'GROUP_ROUND_ONE','one')
                second=await runtime.submit_message(cid,'GROUP_ROUND_TWO','two')
            store.remember('chief','private','PRIVATE_CHIEF')
            store.remember('writer','private','PRIVATE_WRITER')
            folders={}
            for index, tid in enumerate(first['task_ids']+second['task_ids']):
                task=store.get_task(tid); spec=runtime.config.agents.get(task['agent_id'])
                await runtime._history(task,spec)
                meta=runtime._meta(store.get_task(tid))['pi_session']
                self.session_dir=store.pi_session_directory(cid,spec.id,meta['epoch'])
                folders[spec.id]=self.session_dir
                result=await self.run_pi([{'role':'user','content':task['goal']}],tid)
                text=json.dumps(self.requests[-1]['messages'])
                self.assertEqual(text.count(task['goal']),1)
                if index < 2: self.assertNotIn('GROUP_ROUND_TWO',text)
                if index >= 2: self.assertEqual(text.count('GROUP_ROUND_ONE'),1)
                if index==1: self.assertIn('NATIVE_REPLY_1',text)
                if index==2: self.assertIn('NATIVE_REPLY_2',text)
                if index==3: self.assertIn('NATIVE_REPLY_3',text)
                self.assertNotIn('PRIVATE_CHIEF',text)
                self.assertNotIn('PRIVATE_WRITER',text)
                _, refs=store.memory_context(spec.id,task_id=tid)
                store.update_task_meta(tid,{'memory_refs':refs})
                store.add_conversation_message(cid,spec.id,'assistant',result.text,task_id=tid)
                store.finish_task(tid,result.text)
            self.assertNotEqual(folders['chief'],folders['writer'])
            retry=await runtime.submit_message(cid,'GROUP_ROUND_ONE','one')
            self.assertFalse(retry['created']); self.assertEqual(len(self.requests),4)
            # A queued Pi task that never accepted a prompt must still enter later context.
            self.session_dir=folders['writer']
            path=self.session_dir/'external.json'
            path.write_text(json.dumps([{'id':'never-accepted-source','role':'user',
                'content':'INTERRUPTED_BEFORE_NATIVE_PROMPT','native_task_ids':['never-started']}]))
            await self.run_pi([{'role':'user','content':'FOLLOW_UP'}],'next')
            self.assertEqual(json.dumps(self.requests[-1]['messages']).count('INTERRUPTED_BEFORE_NATIVE_PROMPT'),1)
        finally:
            await runtime.shutdown(); store.close()

    async def test_import_and_group_messages_are_not_duplicated(self):
        old = {'id':'source-1','source_id':'source-1','role':'user','content':'PRIOR_API_GOAL'}
        (self.session_dir/'bootstrap.json').write_text(json.dumps([old]))
        await self.run_pi([{'role':'user','content':'FIRST_PI_TURN'}])
        external = [old, {'id':'source-2','role':'assistant','content':'OTHER_BOT_RESULT'}]
        (self.session_dir/'external.json').write_text(json.dumps(external))
        await self.run_pi([{'role':'user','content':'GROUP_FOLLOWUP'}], 'task-b')
        await self.run_pi([{'role':'user','content':'GROUP_FOLLOWUP_AGAIN'}], 'task-c')
        text = json.dumps(self.requests[-1]['messages'])
        self.assertEqual(text.count('PRIOR_API_GOAL'), 1)
        self.assertEqual(text.count('OTHER_BOT_RESULT'), 1)

    async def test_separate_session_does_not_receive_other_bot_history(self):
        await self.run_pi([{'role':'user','content':'PRIVATE_BOT_A'}])
        self.session_dir = self.root/'other'; self.session_dir.mkdir()
        await self.run_pi([{'role':'user','content':'BOT_B'}], 'task-b')
        self.assertNotIn('PRIVATE_BOT_A', json.dumps(self.requests[-1]))

    async def test_corrupt_session_fails_without_model_call(self):
        (self.session_dir/'session.jsonl').write_text('{broken')
        with self.assertRaises(CliEngineError):
            await self.run_pi([{'role':'user','content':'DO_NOT_RESET'}])
        self.assertEqual(self.requests, [])


if __name__ == '__main__': unittest.main(verbosity=2)
