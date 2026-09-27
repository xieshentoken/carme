"""Chat Skill changes and permanent trash cleanup, using disposable accounts only."""
from __future__ import annotations

import asyncio
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['CARME_LOAD_ENV'] = '0'

import httpx
from fastapi import Depends, FastAPI
from carme.api.routes import build_router, require_token
from carme.approval import ApprovalOutcome
from carme.attachments import archive_binary, file_path, save_file
from carme.engines import BRIDGE_TOOL_NAMES
from carme.skills import SkillError, SkillManager
from carme.store import Store
from carme.tools.base import ToolContext
import test_isolation_m1

DOCUMENT = '---\nname: Chat fixture\ndescription: Synthetic test\n---\n\nRead the supplied document.\n'


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return buf.getvalue()


class ChatSkills(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = test_isolation_m1.M1(); await self.fixture.asyncSetUp()
        self.runtime = self.fixture.runtime; self.store = self.fixture.store
        self.runtime._schedule = lambda *a, **k: None
        self.spec = self.fixture.config.agents.get('bot'); self.spec.tools = ['skill', 'files']
        self.cid = self.store.create_conversation(['bot'])['id']
        self.tid = (await self.runtime.submit_message(self.cid, 'Install requested fixture', 'initial'))['task_id']
        self.approvals = []
        async def approve(**kwargs):
            self.approvals.append(kwargs); return ApprovalOutcome(True, 'test consent')
        self.ctx = self.context(approve)

    def context(self, approve=None):
        return ToolContext(agent=self.spec, task_id=self.tid, store=self.store, approve=approve,
            browser_manager=self.runtime.browsers, extras={
                'policy': self.runtime._meta(self.store.get_task(self.tid))['policy'],
                'check_policy': lambda: self.runtime.check_task_policy(self.tid)})

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def install(self, ctx=None, document=DOCUMENT):
        async def downloaded(manager, url, limit): return document.encode()
        with patch.object(SkillManager, '_download_bytes', downloaded):
            return await self.runtime.registry.execute(ctx or self.ctx, 'install_skill',
                {'source': 'url', 'value': 'https://example.com/SKILL.md'})

    async def test_linux_install_use_remove_without_repeated_approval(self):
        self.spec.execution_target='container';self.spec.execution_target_id='action'
        self.fixture.config.isolation={'desktop':{'version':1},'targets':{'action':{'tools':['skill','files']}}}
        self.tid=(await self.runtime.submit_message(self.cid,'Synthetic Linux Skill install','linux'))['task_id']
        self.ctx=self.context(AsyncMock(side_effect=AssertionError('local approval is unnecessary')))
        result=json.loads(await self.install());sid=result['skill_id']
        self.assertEqual(result['status'],'installed');self.runtime.check_task_policy(self.tid)
        with patch.object(self.runtime.execution,'stage_input',AsyncMock()) as stage:
            content=json.loads(await self.runtime.registry.execute(self.ctx,'use_skill',{'name':sid}))
            self.assertTrue(content['complete']);stage.assert_awaited_once()
            self.assertEqual(content['computer_directory'],'/task-files/'+self.tid+content['directory'])
            self.assertIn('skills/'+sid+'/'+result['revision'],stage.call_args.args[1])
        removed=json.loads(await self.runtime.registry.execute(self.ctx,'remove_skill',{'name':sid}))
        self.assertEqual(removed['status'],'removed');self.assertFalse(removed['package_uninstalled'])
        self.assertFalse(self.approvals);self.runtime.check_task_policy(self.tid)

    async def test_approved_install_immediate_use_and_receipt_replay(self):
        result = json.loads(await self.install()); sid = result['skill_id']
        self.assertEqual(result['status'], 'installed'); self.assertEqual(len(self.approvals), 1)
        self.runtime.check_task_policy(self.tid)
        content = await self.runtime.registry.execute(self.ctx, 'use_skill', {'name': sid})
        self.assertIn('Read the supplied document', content)
        self.assertEqual(set(self.runtime.skills.settings()['grants']), {'*'})
        self.assertTrue({'install_skill', 'remove_skill'} <= BRIDGE_TOOL_NAMES)
        # Simulate a crash after the effect/receipt but before the tool-result checkpoint.
        replay = self.context()
        self.assertEqual(json.loads(await self.install(replay)), result)
        self.assertEqual(len(self.runtime.skills.list_skills()), 1)

    async def test_denied_or_missing_approval_never_installs(self):
        for approve in (None, AsyncMock(return_value=ApprovalOutcome(False, 'cancel'))):
            ctx = self.context(approve)
            # Use a new operation ordinal for a genuinely new call.
            ctx.extras['operation_ordinal'] = 20 if approve else 0
            self.assertIn('未安装', await self.install(ctx))
            self.assertFalse(self.runtime.skills.list_skills())
            self.assertFalse(self.runtime.skills.settings().get('grants'))

    async def test_unknown_prior_effect_stays_blocked_for_reconciliation(self):
        await self.install()
        self.store._write("UPDATE task_operations SET status='started' WHERE task_id=?", (self.tid,))
        result = await self.install(self.context())
        self.assertIn('external_effect_reconciliation_required', result)
        self.assertEqual(self.store._query_one('SELECT status FROM task_operations WHERE task_id=?', (self.tid,))['status'], 'started')
        self.assertEqual(len(self.runtime.skills.list_skills()), 1)

    async def test_revocation_during_approval_is_not_refreshed_away(self):
        async def revoke(**kwargs):
            self.spec.tools = ['files']; return ApprovalOutcome(True)
        result = await self.install(self.context(revoke))
        self.assertIn('permission_version_changed', result)
        self.assertFalse(self.runtime.skills.list_skills())

    async def test_local_path_and_private_url_denied(self):
        result = await self.runtime.registry.execute(self.ctx, 'install_skill', {'source': 'path', 'value': '/etc/passwd'})
        self.assertIn('invalid_skill_source', result)
        result = await self.runtime.registry.execute(self.ctx, 'install_skill', {'source': 'url', 'value': 'http://127.0.0.1/SKILL.md'})
        self.assertIn('SkillError', result); self.assertFalse(self.approvals)
        self.assertFalse(self.runtime.skills.list_skills())
        operations = self.store._query('SELECT status,receipt FROM task_operations WHERE task_id=?', (self.tid,))
        self.assertTrue(operations)
        self.assertTrue(all(o['status'] == 'finished' and json.loads(o['receipt'])['effect'] == 'not_performed' for o in operations))

    async def test_attachment_zip_and_cross_conversation_denial(self):
        raw = zip_bytes([('fixture/SKILL.md', DOCUMENT), ('fixture/scripts/a.sh', 'echo no-auto-run')])
        file = archive_binary(self.store, self.cid, 'fixture.zip', raw, task_id=self.tid)
        mid = self.store.add_conversation_message(self.cid, 'bot', 'user', 'Install attached', task_id=self.tid)
        self.store._write('UPDATE attachments SET message_id=? WHERE id=?', (mid, file['id']))
        result = json.loads(await self.runtime.registry.execute(self.ctx, 'install_skill', {'source': 'attachment', 'value': file['id']}))
        self.assertEqual(len(self.runtime.skills.manifest(result['skill_id'], result['revision'])['files']), 2)
        other = self.store.create_conversation(['bot'])['id']
        foreign = save_file(self.store, other, 'SKILL.md', DOCUMENT.encode())
        denied = await self.runtime.registry.execute(self.ctx, 'install_skill', {'source': 'attachment', 'value': foreign['id']})
        self.assertIn('artifact_scope_denied', denied)
        self.assertEqual(len(self.approvals), 1)

    async def test_remove_shared_retains_other_grant_and_package(self):
        result = json.loads(await self.install()); sid = result['skill_id']
        self.runtime.skills.grant('another-bot', sid, result['revision'])
        removed = json.loads(await self.runtime.registry.execute(self.ctx, 'remove_skill', {'name': sid}))
        self.assertFalse(removed['package_uninstalled'])
        self.assertNotIn(sid, self.runtime.skills.settings()['grants']['bot'])
        self.assertIn(sid, self.runtime.skills.settings()['grants']['another-bot'])
        self.assertTrue(self.runtime.skills.get(sid).path.is_dir())
        self.runtime.check_task_policy(self.tid)
        self.assertIn('skill_version_not_granted', await self.runtime.registry.execute(self.ctx, 'remove_skill', {'name': sid}))

    async def test_explicit_account_uninstall_keeps_history(self):
        result = json.loads(await self.install()); sid = result['skill_id']
        original = self.runtime.skills.get(sid).path
        removed = json.loads(await self.runtime.registry.execute(self.ctx, 'remove_skill', {'name': sid, 'scope': 'account'}))
        self.assertTrue(removed['package_uninstalled']); self.assertFalse(original.exists())
        self.runtime.skills.manifest(sid, result['revision'])
        self.runtime.check_task_policy(self.tid)

    async def test_shared_skill_reaches_running_and_future_bots(self):
        import copy
        other = copy.deepcopy(self.spec); other.id = 'second'; self.fixture.config.agents.agents[other.id] = other
        cid = self.store.create_conversation([other.id])['id']
        tid = (await self.runtime.submit_message(cid, 'already running', 'second'))['task_id']
        second = ToolContext(agent=other, task_id=tid, store=self.store, browser_manager=self.runtime.browsers,
            extras={'policy': self.runtime._meta(self.store.get_task(tid))['policy'],
                    'check_policy': lambda: self.runtime.check_task_policy(tid)})
        installed = json.loads(await self.install()); sid = installed['skill_id']
        self.runtime.check_task_policy(tid)  # Addition does not cancel another running bot.
        listed = json.loads(await self.runtime.registry.execute(second, 'list_skills', {}))
        self.assertEqual([x['id'] for x in listed['skills']], [sid])
        result = json.loads(await self.runtime.registry.execute(second, 'use_skill', {'name': sid}))
        self.assertEqual(result['revision'], installed['revision'])
        self.assertEqual(second.extras['policy']['skill_grants'][sid], result['revision'])
        self.assertIn(sid, self.runtime.skills.effective_grants('future-bot'))
        self.assertIn(sid, self.runtime.skills.prompt_block('future-bot'))
        # No new package when another bot installs the same download again.
        second.approve = self.ctx.approve
        repeated = json.loads(await self.install(second))
        self.assertEqual(repeated['skill_id'], sid)
        self.assertEqual(len(self.runtime.skills.list_skills()), 1)
        self.runtime.skills.grant('*', sid, result['revision'], revoke=True)
        with self.assertRaisesRegex(RuntimeError, 'permission_version_changed'):
            self.runtime.check_task_policy(tid)

    async def test_global_remove_denied_in_linux_leaves_all_bots_usable(self):
        self.spec.execution_target = 'container'; self.spec.execution_target_id = 'action'
        self.fixture.config.isolation = {'desktop': {'version': 1}, 'targets': {'action': {'tools': ['skill', 'files']}}}
        self.tid = (await self.runtime.submit_message(self.cid, 'Linux shared', 'global-delete'))['task_id']
        approve = AsyncMock(return_value=ApprovalOutcome(False, 'not approved'))
        self.ctx = self.context(approve)
        installed = json.loads(await self.install()); approve.assert_not_awaited()
        result = await self.runtime.registry.execute(self.ctx, 'remove_skill', {'name': installed['skill_id'], 'scope': 'account'})
        self.assertIn('未删除', result); approve.assert_awaited_once()
        self.assertIn(installed['skill_id'], self.runtime.skills.effective_grants('future-bot'))

    async def test_concurrent_install_deduplicates_entire_bundle(self):
        import asyncio
        manager = self.runtime.skills
        def install(script):
            return manager._install_bundle(DOCUMENT, [('scripts/run.py', script)], source='synthetic')
        same = await asyncio.gather(*[asyncio.to_thread(install, b'print(1)') for _ in range(8)])
        self.assertEqual(len({skill.id for skill in same}), 1)
        different = install(b'print(2)')
        self.assertNotEqual(different.id, same[0].id)
        self.assertEqual(len(manager.list_skills()), 2)
        # Storage and grants are account-local, including the same-name case.
        isolated = SkillManager(self.fixture.root / 'other-account', self.fixture.root / 'other.yaml')
        self.assertFalse(isolated.effective_grants('bot'))
        with self.assertRaises(SkillError): isolated.get(same[0].id)

    async def test_migration_merges_exact_duplicates_preserves_history_and_alias(self):
        import shutil
        manager = self.runtime.skills
        first = manager.install_from_text(DOCUMENT); rev = manager.snapshot(first.id)['revision']
        manager.grant('bot', first.id, rev)
        alias = first.id + '-2'; shutil.copytree(first.path, manager.root / alias)
        settings = manager.settings(); settings['installed'][alias] = {**settings['installed'][first.id], 'installed_at': 9999999999}
        manager._save(settings); manager.snapshot(alias); manager.grant('other', alias, rev)
        self.assertEqual(manager.migrate_shared(), {alias: first.id})
        self.assertEqual(manager.get(alias).id, first.id)
        self.assertEqual(manager.effective_grants('future'), {first.id: rev})
        self.assertEqual(len(manager.list_skills()), 1)
        manager.manifest(alias, rev)  # Exact historical receipts still resolve.
        self.assertEqual(manager.migrate_shared(), {})

    async def test_changed_source_after_review_rejected(self):
        installed = self.runtime.skills.install_from_text(DOCUMENT)
        async def tamper(**kwargs):
            (installed.path / 'SKILL.md').write_text(DOCUMENT + '\nchanged\n')
            return ApprovalOutcome(True)
        result = await self.runtime.registry.execute(self.context(tamper), 'install_skill', {'source': 'installed', 'value': installed.id})
        self.assertIn('skill_changed_after_approval', result)
        self.assertFalse(self.runtime.skills.settings().get('grants'))
        self.assertFalse(self.store._query("SELECT id FROM task_operations WHERE task_id=? AND status!='finished'", (self.tid,)))

    async def test_download_checks_every_redirect(self):
        from carme import docker_browser
        calls = []
        async def fake(data, safety, check, **kwargs):
            calls.append(data['url']); docker_browser.web_url(data['url']); check()
            return {'status': 302, 'headers': [['location', 'http://localhost/private']], 'bytes': 0}
        with patch.object(docker_browser, 'fetch_public', fake):
            with self.assertRaises(SkillError):
                await self.runtime.skills._download_bytes('https://example.com/skill', 1000)
        self.assertEqual(calls, ['https://example.com/skill', 'http://localhost/private'])

    async def test_zip_rejects_traversal_symlink_and_ambiguous_packages(self):
        link = zipfile.ZipInfo('link'); link.external_attr = (stat.S_IFLNK | 0o777) << 16
        bad_packages = [[('SKILL.md', DOCUMENT), ('../escape', 'bad')],
            [('SKILL.md', DOCUMENT), (link, '/etc/passwd')],
            [('one/SKILL.md', DOCUMENT), ('two/SKILL.md', DOCUMENT)]]
        for entries in bad_packages:
            with self.assertRaises(SkillError): self.runtime.skills.install_from_zip(zip_bytes(entries))
        self.assertFalse(self.runtime.skills.list_skills())


class Trash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='carme-trash-test-'); self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {'CARME_ARTIFACTS_DIR': str(self.root / 'artifacts')}); self.env.start()
        self.store = Store(self.root / 'test.db')

    def tearDown(self):
        self.store.close(); self.env.stop(); self.tmp.cleanup()

    def chat(self):
        cid = self.store.create_conversation(['a'])['id']
        tid = self.store.create_conversation_turn(cid, 'a', 'private fixture', 'request')['task_id']
        self.store.finish_task(tid, 'finished')
        return cid, tid

    def test_purge_records_files_and_retain_shared_bytes_and_bot_memory(self):
        cid, tid = self.chat(); kept, kept_tid = self.chat()
        self.store.remember('a', 'memory', 'keep'); self.store.add_message(tid, 'a', 'tool', 'private')
        a = archive_binary(self.store, cid, 'owned.txt', b'owned content', task_id=tid)
        b = archive_binary(self.store, cid, 'shared.txt', b'shared content', task_id=tid)
        c = archive_binary(self.store, kept, 'shared.txt', b'shared content', task_id=kept_tid)
        draft = save_file(self.store, cid, 'draft.txt', b'draft')
        paths = [file_path(self.store, f['id']) for f in (a, b, c, draft)]
        self.store.create_approval(task_id=tid, agent_id='a', kind='skill', summary='private')
        self.store.add_event('conversation.updated', {'conversation_id': cid})
        self.store.add_event('task.finished', {'private': 'content'}, task_id=tid)
        self.store._write('INSERT INTO task_checkpoints VALUES (?,1,?,?,1)', (tid, '{}', 'hash'))
        self.store.update_conversation(cid, {'deleted': True})
        result = self.store.purge_conversations([cid])
        self.assertEqual(result['deleted_count'], 1); self.assertEqual(result['file_cleanup_pending'], 0)
        self.assertFalse(self.store.get_task(tid)); self.assertFalse(self.store.get_conversation(cid))
        for table in ('messages', 'approvals', 'task_checkpoints', 'attachments', 'conversation_requests'):
            key = 'conversation_id' if table in {'attachments', 'conversation_requests'} else 'task_id'
            self.assertFalse(self.store._query(f'SELECT * FROM {table} WHERE {key}=?', (cid if key == 'conversation_id' else tid,)))
        self.assertFalse(paths[0].exists()); self.assertFalse(paths[3].exists()); self.assertTrue(paths[2].exists())
        self.assertTrue(self.store.get_conversation(kept)); self.assertEqual(self.store.recall('a')[0]['value'], 'keep')
        self.assertFalse(self.store._query('SELECT * FROM events'))
        self.assertEqual(self.store.purge_conversations([cid])['deleted_count'], 0)

    def test_restored_hidden_and_running_selection_rejected_atomically(self):
        cid, tid = self.chat(); kept, _ = self.chat()
        self.store.update_conversation(cid, {'deleted': True})
        for changes in ({}, {'hidden': True}, {'deleted': True}, {'deleted': False}):
            self.store.update_conversation(kept, changes)
            if changes == {'deleted': True}: self.store.update_conversation(kept, {'deleted': False})
            with self.assertRaises(ValueError): self.store.purge_conversations([cid, kept])
            self.assertTrue(self.store.get_conversation(cid))
        child = self.store.create_task('a', 'active child', parent_id=tid)
        with self.assertRaises(ValueError): self.store.purge_conversations([cid])
        self.store.finish_task(child, 'done')
        self.store.purge_conversations([cid]); self.assertFalse(self.store.get_task(child))

    def test_filesystem_failure_rolls_back_records_and_files(self):
        cid, tid = self.chat(); files = [save_file(self.store, cid, name, b'content') for name in ('a.txt', 'b.txt')]
        paths = [file_path(self.store, f['id']) for f in files]
        self.store.update_conversation(cid, {'deleted': True})
        rename = Path.rename; calls = []
        def fail_second(path, destination):
            if not path.name.startswith('.carme-purged-'):
                calls.append(path)
                if len(calls) == 2: raise PermissionError('synthetic')
            return rename(path, destination)
        with patch.object(Path, 'rename', fail_second):
            with self.assertRaises(PermissionError): self.store.purge_conversations([cid])
        self.assertTrue(self.store.get_conversation(cid)); self.assertTrue(self.store.get_task(tid))
        self.assertTrue(all(p.read_bytes() == b'content' for p in paths))

    def test_endpoint_checks_auth_and_reviewed_ids(self):
        async def check():
            fixture = test_isolation_m1.M1(); await fixture.asyncSetUp()
            try:
                app = FastAPI(); app.include_router(build_router(fixture.config, fixture.store, fixture.runtime), dependencies=[Depends(require_token)])
                cid = fixture.store.create_conversation(['bot'])['id']
                fixture.store.update_conversation(cid, {'deleted': True})
                with patch.dict(os.environ, {'CARME_TOKEN': 'synthetic-admin-token'}):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        path = '/api/conversations/purge'; body = {'conversation_ids': [cid]}
                        self.assertEqual((await client.post(path, json=body)).status_code, 401)
                        client.headers['Authorization'] = 'Bearer synthetic-admin-token'
                        self.assertEqual((await client.post(path, json={'conversation_ids': []})).status_code, 422)
                        reply = await client.post(path, json=body)
                        self.assertEqual(reply.status_code, 200, reply.text); self.assertEqual(reply.json()['deleted_count'], 1)
                        self.assertEqual((await client.get('/api/conversations/' + cid)).status_code, 404)
            finally: await fixture.asyncTearDown()
        asyncio.run(check())


if __name__ == '__main__':
    unittest.main(verbosity=2)
