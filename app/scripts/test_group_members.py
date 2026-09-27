"""G3 member management and server-owned provenance, synthetic accounts only."""
import asyncio
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
import httpx
import yaml
from fastapi import FastAPI
from test_runtime import setup, settle
from carme import config as config_module
from carme.api.routes import build_router
from carme.llm import LLMResponse
from carme.store import Store
from prepare_main_group_roster import prepare, KEEP, REMOVE


class GroupMembers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='carme-g3-')
        self.root=Path(self.tmp.name)
        self.runtime,self.store,self.gateway=setup(self.root/'runtime',[])
        self.config=self.runtime.config
        self.config.models.tiers={'balanced':[]}
        for key in ['chief','writer']:self.config.agents.get(key).creation_source='user_created'
        self.config.agents.get('chief').name='同名';self.config.agents.get('writer').name='同名'
        directory=self.root/'config';directory.mkdir()
        (directory/'agents.yaml').write_text(yaml.safe_dump({'agents':{k:asdict(v) for k,v in self.config.agents.agents.items()}}))
        (directory/'models.yaml').write_text(yaml.safe_dump({'tiers':{'balanced':{'candidates':[]}}}))
        self.patch=patch.object(config_module,'CONFIG_DIR',directory);self.patch.start()
        self.app=FastAPI();self.app.include_router(build_router(self.config,self.store,self.runtime))
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://synthetic')
        self.cid=self.store.create_conversation(['chief','writer'])['id']
    async def asyncTearDown(self):
        await self.client.aclose();await self.runtime.shutdown();self.store.close();self.patch.stop();self.tmp.cleanup()
    async def edit(self,ids,revision=0,**extra):
        return await self.client.patch(f'/api/conversations/{self.cid}/members',json={'agent_ids':ids,'expected_revision':revision,**extra})

    async def test_confirmed_main_archive_and_rename(self):
        source={'defaults':{'sentinel':'unchanged'}, 'agents':{
            key:{'name':key,'system_prompt':'preserved '+key} for key in KEEP+REMOVE}}
        source['agents']['future']={'name':'New user bot','creation_source':'user_created'}
        candidate=prepare(source)
        self.assertEqual(set(source['agents']),set(KEEP+REMOVE)|{'future'})
        self.assertEqual(candidate['defaults'],source['defaults'])
        self.assertEqual(candidate['archived_agents'],{k:source['agents'][k] for k in REMOVE})
        self.assertEqual(prepare(candidate),candidate)
        self.assertEqual(candidate['agents']['future'],source['agents']['future'])
        for key in KEEP:
            self.assertEqual(candidate['agents'][key],dict(source['agents'][key],creation_source='user_created'))
        with self.assertRaises(ValueError):prepare({'agents':{}})
        collision=dict(source,archived_agents={'research':{'name':'do not overwrite'}})
        with self.assertRaises(ValueError):prepare(collision)
        directory=config_module.CONFIG_DIR
        (directory/'agents.yaml').write_text(yaml.safe_dump(candidate))
        self.config.agents=config_module.load(reload=True).agents
        rows=(await self.client.get('/api/agents')).json()['agents']
        self.assertEqual({a['id'] for a in rows},set(KEEP)|{'future'})
        self.assertTrue(all(a['group_invitable'] for a in rows))
        old=self.store.create_conversation(['chief','research'],title='历史群')['id']
        self.store.add_conversation_message(old,'research','assistant','preserve history')
        self.store.remember('research','fact','preserve memory')
        before=self.store.list_conversation_messages(old)
        for key in REMOVE:
            response=await self.client.post('/api/conversations',json={'agent_ids':['chief',key]})
            self.assertEqual(response.status_code,422)
            deleted_chat=self.store.create_conversation([key])['id']
            with self.assertRaises(KeyError):await self.runtime.submit_message(deleted_chat,'no','removed-'+key,key)
        for members in [list(KEEP),[KEEP[0]]]:
            group=self.store.create_conversation(list(KEEP),title='before')['id']
            if len(members)==1:self.store.update_conversation_members(group,members,0)
            result=await self.client.patch(f'/api/conversations/{group}',json={'title':'  自定义群名  '})
            self.assertEqual(result.status_code,200,result.text)
            saved=self.store.get_conversation(group)
            self.assertEqual(saved['title'],'自定义群名')
            self.assertEqual(saved['kind'],'group');self.assertEqual(saved['agent_ids'],members)
            self.assertEqual((await self.client.patch(f'/api/conversations/{group}',json={'title':'  '})).status_code,422)
        self.assertEqual(self.store.list_conversation_messages(old),before)
        self.assertEqual(self.store.memory_search('research',key='fact')[0]['value'],'preserve memory')
        self.assertEqual((await self.client.get(f'/api/conversations/{old}')).status_code,200)

    async def test_members_preserve_data_and_single_member_group(self):
        old=self.store.create_conversation_turn(self.cid,'writer','OLD_USER','old')['task_id']
        self.store.add_conversation_message(self.cid,'writer','assistant','OLD_REPLY',task_id=old)
        self.store.finish_task(old,'done');self.store.remember('writer','fact','KEEP')
        before=self.store.list_conversation_messages(self.cid)
        response=await self.edit(['chief']);self.assertEqual(response.status_code,200,response.text)
        group=response.json()['conversation'];self.assertEqual(group['kind'],'group');self.assertEqual(group['members_revision'],1)
        self.assertEqual(self.store.list_conversation_messages(self.cid),before)
        self.assertEqual(self.store.memory_search('writer',key='fact')[0]['value'],'KEEP')
        with self.assertRaises(ValueError):await self.runtime.submit_message(self.cid,'hello','removed','writer')
        self.gateway.responses=[LLMResponse(text='only remaining member')]
        result=await self.runtime.submit_message(self.cid,'hello','new');await settle(self.runtime)
        self.assertEqual(len(result['task_ids']),1)
        reply=await self.runtime._delegate(result['task_id'],'chief','writer','forbidden','',0)
        self.assertIn('派发被拒',reply)
        response=await self.edit(['chief','writer'],1);self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.store.get_conversation(self.cid)['kind'],'group')

    async def test_validation_and_concurrent_edit(self):
        for ids in [[],['chief']*2,['chief','foreign-account'],['chief','outside'],['x']*7]:
            self.assertIn((await self.edit(ids)).status_code,[404,409,422])
        self.assertEqual(self.store.get_conversation(self.cid)['members_revision'],0)
        results=await asyncio.gather(self.edit(['chief']),self.edit(['writer']))
        self.assertEqual(sorted(r.status_code for r in results),[200,409])
        self.assertEqual((await self.edit(['chief','writer'])).status_code,409)
        direct=self.store.create_conversation(['chief'])['id']
        response=await self.client.patch(f'/api/conversations/{direct}/members',json={'agent_ids':['writer'],'expected_revision':0})
        self.assertEqual(response.status_code,409)
        current=self.store.get_conversation(self.cid)
        self.assertEqual((await self.edit(current['agent_ids'],1)).json()['conversation']['members_revision'],1)

    async def test_invites_only_created_and_existing_unknown_retained(self):
        rows=(await self.client.get('/api/agents')).json()['agents']
        self.assertEqual({a['id'] for a in rows if a['group_invitable']},{'chief','writer'})
        for ids in [['chief','outside'],['chief','foreign-account'],['chief','chief']]:
            r=await self.client.post('/api/conversations',json={'agent_ids':ids});self.assertEqual(r.status_code,422)
        self.assertEqual((await self.client.post('/api/conversations',json={'agent_ids':['chief','writer']})).status_code,201)
        cid=self.store.create_conversation(['chief','outside'])['id']
        self.cid=cid
        self.assertEqual((await self.edit(['chief','outside','writer'])).status_code,200)
        self.assertEqual((await self.edit(['chief','writer'],1)).status_code,200)
        self.assertEqual((await self.edit(['chief','outside','writer'],2)).status_code,409)

    async def test_server_owned_creation_copy_and_import(self):
        self.assertEqual((await self.client.patch('/api/agents/outside',json={'creation_source':'user_created'})).status_code,422)
        r=await self.client.patch('/api/agents/outside',json={'name':'改名旧 Bot'})
        self.assertEqual(r.status_code,200,r.text);self.assertEqual(self.config.agents.get('outside').creation_source,'unknown')
        created=await self.client.post('/api/agents',json={'id':'user_new','name':'新建','tools':[],'sandbox':'none'})
        self.assertEqual(created.status_code,201,created.text)
        self.assertEqual(created.json()['agent']['creation_source'],'user_created')
        quick=await self.client.post('/api/agents/quick')
        self.assertEqual(quick.status_code,201,quick.text)
        self.assertEqual(quick.json()['agent']['creation_source'],'user_created')
        copy=await self.client.post('/api/agents/outside/duplicate')
        self.assertEqual(copy.status_code,201,copy.text);self.assertEqual(copy.json()['agent']['creation_source'],'user_created')
        self.assertEqual(self.config.agents.get('outside').creation_source,'unknown')
        body={'bundle':{'agents':[{'id':'imported','profile':{'name':'导入','creation_source':'system'}},
                                  {'id':'outside','profile':{'name':'导入旧项','creation_source':'user_created'}}]},'import_memory':False}
        imported=await self.client.post('/api/import',json=body)
        self.assertEqual(imported.status_code,200,imported.text)
        self.assertEqual(self.config.agents.get('imported').creation_source,'user_created')
        self.assertEqual(self.config.agents.get('outside').creation_source,'unknown')
        raw=yaml.safe_load((config_module.CONFIG_DIR/'agents.yaml').read_text())
        self.assertEqual(raw['agents']['user_new']['creation_source'],'user_created')
        self.assertEqual(config_module.load(reload=True).agents.get('imported').creation_source,'user_created')

    async def test_stop_drain_and_race(self):
        entered, cancelling, release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def blocked():
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelling.set();await release.wait();raise
        self.gateway.responses=[blocked]
        result=await self.runtime.submit_message(self.cid,'active','active')
        await entered.wait()
        self.assertEqual((await self.edit(['chief'])).status_code,409)
        change=asyncio.create_task(self.edit(['chief'],stop_tasks=True))
        await cancelling.wait()
        with self.assertRaisesRegex(ValueError,'正在更新'):await self.runtime.submit_message(self.cid,'race','race')
        self.assertEqual((await self.edit(['writer'],stop_tasks=True)).status_code,409)
        self.assertEqual(self.store.get_conversation(self.cid)['agent_ids'],['chief','writer'])
        release.set();response=await change;self.assertEqual(response.status_code,200,response.text)
        self.assertEqual([self.store.get_task(t)['status'] for t in result['task_ids']],['cancelled','cancelled'])
        self.assertFalse(self.runtime._membership_updates)
        self.assertFalse(self.runtime._jobs)

    async def test_still_running_task_blocks_commit(self):
        entered, release=asyncio.Event(),asyncio.Event()
        async def blocked():
            entered.set()
            try:await asyncio.Event().wait()
            except asyncio.CancelledError:await release.wait();raise
        self.gateway.responses=[blocked]
        await self.runtime.submit_message(self.cid,'active','active');await entered.wait()
        original=asyncio.wait
        async def short_wait(jobs,timeout):return await original(jobs,timeout=0.01)
        with patch('carme.runtime.asyncio.wait',side_effect=short_wait):
            response=await self.edit(['chief'],stop_tasks=True)
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.store.get_conversation(self.cid)['members_revision'],0)
        release.set();await settle(self.runtime)
        self.assertEqual((await self.edit(['chief'])).status_code,200)

    async def test_revision_persisted_and_stale_resume_denied(self):
        with patch.object(self.runtime,'_schedule'):
            turn=await self.runtime.submit_message(self.cid,'old','old','writer')
        task=self.store.get_task(turn['task_id']);self.store.finish_task(task['id'],'cancelled',status='cancelled')
        self.store.checkpoint(task['id'],{'messages':[],'next_step':1,'memory_refs':[]})
        self.assertEqual((await self.edit(['chief'])).status_code,200)
        with self.assertRaisesRegex(ValueError,'群成员'):await self.runtime.resume(task['id'])
        # A distinct account's Store has no access to this group's identity.
        other=Store(self.root/'other-account.db')
        try:
            with self.assertRaises(KeyError):other.update_conversation_members(self.cid,['chief'],1)
        finally:other.close()
        path=self.store.path;self.store.close();self.store=Store(path);self.runtime.store=self.store
        self.assertEqual(self.store.get_conversation(self.cid)['members_revision'],1)
        with self.assertRaisesRegex(ValueError,'其他页面'):self.store.update_conversation_members(self.cid,['writer'],0)


if __name__=='__main__':unittest.main(verbosity=2)
