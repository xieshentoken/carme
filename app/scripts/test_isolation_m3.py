"""M3 boundary tests. Temporary data and synthetic desktop callbacks only."""
import asyncio, base64, contextlib, json, os, sqlite3, sys, tempfile, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]));os.environ['CARME_LOAD_ENV']='0'
from carme import projects as p, migrate as m
from carme.macos_runner import DesktopLease,validate_action,native_window
from carme.store import Store

class M3(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='carme-m3-unit-');self.root=Path(self.tmp.name).resolve()
    def tearDown(self):self.tmp.cleanup()
    def project(self):
        root=self.root/'project';root.mkdir();(root/'main.txt').write_text('before');(root/'other.txt').write_text('other')
        (root/'.pi').mkdir();(root/'.pi/auth.json').write_text('synthetic-never-copy');(root/'.env').write_text('synthetic-never-copy')
        snapshot=self.root/'snapshot';p.snapshot(root,['main.txt','new.txt'],snapshot)
        bundle=json.loads((snapshot/'bundle.json').read_text());return root,snapshot,bundle
    def review(self,bundle):
        artifact=self.root/'artifact.json';validation=self.root/'validation.json';review=self.root/'review.json'
        p.save(artifact,{'snapshot_id':bundle['snapshot_id'],'files':{'main.txt':base64.b64encode(b'after').decode()}})
        p.save(validation,{'snapshot_id':bundle['snapshot_id'],'artifact_sha256':p.sha(artifact.read_bytes()),'checks':[{'name':'synthetic expected output','status':'pass'}]})
        result=p.proposal(self.root/'snapshot',artifact,validation,review);return review,result['approval_digest']
    def test_snapshot_only_selected_no_personal_material(self):
        root,snap,bundle=self.project();workspace=self.root/'workspace';workspace.mkdir();p.materialize(bundle,workspace)
        self.assertEqual(sorted(x.name for x in workspace.iterdir()),['main.txt']);(workspace/'main.txt').write_text('changed')
        self.assertEqual((root/'main.txt').read_text(),'before');self.assertNotIn(str(root),json.dumps(bundle))
    def test_snapshot_traversal_links_hardlinks(self):
        root,snap,bundle=self.project()
        for name in ('../bad','.pi/auth.json','.PI/AUTH.JSON','.env','.Env','/etc/passwd','a/../b','a//b'):
            with self.assertRaises(ValueError):p.relative(name)
        (root/'link').symlink_to(root/'main.txt')
        with self.assertRaises(OSError):p.read(root,'link')
        os.link(root/'main.txt',root/'hard')
        with self.assertRaises(ValueError):p.read(root,'hard')
    def test_patch_approval_roundtrip_rollback(self):
        root,snap,bundle=self.project();review,approval=self.review(bundle);journal=self.root/'journal.json'
        with self.assertRaises(ValueError):p.apply(snap,review,'unapproved',journal)
        p.apply(snap,review,approval,journal);self.assertEqual((root/'main.txt').read_text(),'after')
        p.rollback(journal);self.assertEqual((root/'main.txt').read_text(),'before')
    def test_patch_refuses_other_worktree_change(self):
        root,snap,bundle=self.project();review,approval=self.review(bundle);(root/'other.txt').write_text('human edit')
        with self.assertRaisesRegex(ValueError,'project_conflict'):p.apply(snap,review,approval,self.root/'journal')
        self.assertEqual((root/'main.txt').read_text(),'before')
    def test_patch_refuses_git_revision_change(self):
        root,snap,bundle=self.project();(root/'.git').mkdir();(root/'.git/HEAD').write_text('ref: refs/heads/main')
        review,approval=self.review(bundle)
        with self.assertRaisesRegex(ValueError,'project_conflict'):p.apply(snap,review,approval,self.root/'journal')
    def test_patch_refuses_permission_change(self):
        root,snap,bundle=self.project();review,approval=self.review(bundle);(root/'main.txt').chmod(0o700)
        with self.assertRaisesRegex(ValueError,'project_conflict'):p.apply(snap,review,approval,self.root/'journal')
    def test_rollback_never_overwrites_new_edit(self):
        root,snap,bundle=self.project();review,approval=self.review(bundle);j=self.root/'journal'
        p.apply(snap,review,approval,j);(root/'main.txt').write_text('human later')
        with self.assertRaisesRegex(ValueError,'rollback_conflict'):p.rollback(j)
        self.assertEqual((root/'main.txt').read_text(),'human later')
    def test_interrupted_patch_recovers(self):
        root,snap,bundle=self.project();review,approval=self.review(bundle);j=self.root/'journal'
        with patch('carme.projects.os.link',side_effect=OSError('synthetic interrupted')):
            with self.assertRaises(OSError):p.apply(snap,review,approval,j)
        self.assertEqual(json.loads(j.read_text())['state'],'needs_recovery')
        p.rollback(j);self.assertEqual((root/'main.txt').read_text(),'before')
    def test_changed_validation_and_unknown_patch_denied(self):
        root,snap,bundle=self.project();self.review(bundle)
        v=json.loads((self.root/'validation.json').read_text());v['checks'][0]['status']='fail';p.save(self.root/'validation.json',v)
        with self.assertRaises(ValueError):p.proposal(snap,self.root/'artifact.json',self.root/'validation.json',self.root/'review2')
    def source(self):
        app=self.root/'app';(app/'config').mkdir(parents=True);data=self.root/'nondefault-data';data.mkdir()
        (app/'.local/active').mkdir(parents=True);(app/'.local/active/.env').write_text('CARME_DATA_DIR='+str(data)+'\nCARME_TOKEN=synthetic-not-in-report\n')
        p.save(app/'config/agents.yaml',{'agents':{'alpha':{'name':'Alpha','engine':'pi','tools':['files','exec'],'engine_workspace':'/unapproved','execution_target':'macos'}}})
        p.save(app/'config/skills.yaml',{'installed':{'s1':{'enabled':True}},'roots':[]})
        (data/'skills/s1').mkdir(parents=True);(data/'skills/s1/SKILL.md').write_text('synthetic skill')
        store=Store(data/'carme.db');store.remember('alpha','private','synthetic memory');cid=store.create_conversation(['alpha'])['id']
        store.add_conversation_message(cid,'alpha','user','history')
        from carme.attachments import save_file
        save_file(store,cid,'history.txt',b'synthetic old attachment')
        store.create_session('synthetic-token');store.close()
        return app,data,cid
    def test_nondefault_active_profile_requires_confirmation(self):
        app,data,cid=self.source();report=m.plan(app,self.root/'plan.json',allow_roots=[self.root])
        self.assertIn('active_env_path_hints_require_explicit_confirmation',report['blocked'])
    def test_migration_distinguishes_host_container_and_ssh_paths(self):
        app,data,cid=self.source()
        p.save(app/'config/sandbox.yaml',{'modes':{'docker':{'workdir':'/workspace'},'remote':{'root':'~/not-this-mac'}},'nodes':[{'root':'~/not-this-mac'}]})
        (data/'screenshots').mkdir();(data/'screenshots/fixture.png').write_bytes(b'synthetic-history-not-a-real-screen')
        report=m.plan(app,self.root/'plan',{'CARME_DATA_DIR':str(data)},[self.root]);self.assertFalse(report['blocked'])
        self.assertEqual({r['namespace'] for r in report['configured_paths']},{'container','ssh'})
        self.assertEqual(len(report['files']['screenshots']),1)
    def test_migration_wal_backup_with_concurrent_writes(self):
        db=self.root/'wal.db';c=sqlite3.connect(db);c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA wal_autocheckpoint=0');c.execute('CREATE TABLE pair(a,b)');c.commit()
        for i in range(30):c.execute('INSERT INTO pair VALUES (?,?)',(i,i))
        c.commit();self.assertGreater(Path(str(db)+'-wal').stat().st_size,0)
        stop=threading.Event()
        def writer():
            with contextlib.closing(sqlite3.connect(db)) as w:
                for i in range(30,80):
                    if stop.is_set():break
                    w.execute('INSERT INTO pair VALUES (?,?)',(i,i));w.commit();time.sleep(.002)
        t=threading.Thread(target=writer);t.start()
        try:manifest=m.backup(db,self.root/'copy.db')
        finally:stop.set();t.join();c.close()
        with contextlib.closing(sqlite3.connect(self.root/'copy.db')) as check:
            self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok');self.assertEqual(check.execute('SELECT count(*) FROM pair WHERE a!=b').fetchone()[0],0)
        self.assertGreaterEqual(manifest['pair']['rows'],30)
    def test_migration_preserves_ids_acl_and_revokes_authority(self):
        app,data,cid=self.source();plan=m.plan(app,self.root/'plan',{'CARME_DATA_DIR':str(data)},[self.root]);self.assertFalse(plan['blocked'])
        self.assertNotIn('synthetic-not-in-report',json.dumps(plan));before=(data/'carme.db').read_bytes()
        result=m.stage(self.root/'plan',plan['approval_digest'],self.root/'stage',quiesced=True)
        self.assertEqual(result['state'],'verified');self.assertEqual((data/'carme.db').read_bytes(),before)
        with m.connect_ro(self.root/'stage/runtime/control/carme.db') as db:
            self.assertEqual(db.execute('SELECT id FROM conversations').fetchone()[0],cid)
            self.assertEqual(db.execute('SELECT count(*) FROM memory').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT revoked FROM browser_sessions').fetchone()[0],1)
        a=json.loads((self.root/'stage/config/agents.yaml').read_text())['agents']['alpha'];self.assertEqual(a['execution_target'],'none');self.assertFalse(a['tools'])
        account=self.root/'account';account.mkdir();m.install(self.root/'stage',account,result['approval_digest'])
        with self.assertRaises(ValueError):m.install(self.root/'stage',account,result['approval_digest'])
        self.assertFalse((self.root/'stage/runtime/control/browser').exists())
    def test_migration_changed_source_and_partial_stage_preserved(self):
        app,data,cid=self.source();plan=m.plan(app,self.root/'plan',{'CARME_DATA_DIR':str(data)},[self.root])
        (data/'skills/s1/SKILL.md').write_text('new user content')
        with self.assertRaisesRegex(ValueError,'source_changed'):m.stage(self.root/'plan',plan['approval_digest'],self.root/'failed-stage',quiesced=True)
        self.assertEqual(json.loads((self.root/'failed-stage/migration.json').read_text())['state'],'incomplete_preserved')
        self.assertTrue((data/'carme.db').exists())
    def test_migration_requires_quiescence_and_no_source_overlap(self):
        app,data,cid=self.source();plan=m.plan(app,self.root/'plan',{'CARME_DATA_DIR':str(data)},[self.root])
        with self.assertRaisesRegex(ValueError,'pause_admission'):m.stage(self.root/'plan',plan['approval_digest'],self.root/'stage')
        with self.assertRaisesRegex(ValueError,'overlap'):m.stage(self.root/'plan',plan['approval_digest'],data/'stage',quiesced=True)
    def mac(self,who='a',run='run1'):
        lease=DesktopLease(self.root/who,self.root/'physical.lock')
        p.save(lease.home/'grant.json',{'bot_id':who,'bundle_id':'fixture.editor','window_id':7,'expires':time.time()+60,'generation':'gen1'})
        action={'op':'click','bundle_id':'fixture.editor','window_id':7,'x':20,'y':30}
        job={'bot_id':who,'run_id':run,'deadline':time.time()+30,'action':action,'action_sha256':p.digest(action)}
        window={'frontmost':True,'bundle_id':'fixture.editor','window_id':7,'bounds':[0,0,100,100]}
        return lease,job,window
    def test_human_takeover_cancels_queued_actions(self):
        lease,job,window=self.mac();calls=[]
        try:
            lease.perform(job,lambda:window,lambda a:calls.append(a));lease.takeover()
            with self.assertRaises(ValueError):lease.perform(job,lambda:window,lambda a:calls.append(a))
            self.assertEqual(len(calls),1);self.assertFalse(lease.grant())
        finally:lease.release()
    def test_human_takeover_during_window_check_prevents_input(self):
        lease,job,window=self.mac();lease.acquire(job,window);threads=[];calls=[]
        def window_check():
            t=threading.Thread(target=lease.takeover);threads.append(t);t.start()
            until=time.monotonic()+1
            while (lease.home/'grant.json').exists() and time.monotonic()<until:time.sleep(.001)
            self.assertFalse((lease.home/'grant.json').exists())
            return window
        try:
            with self.assertRaises(ValueError):lease.perform(job,window_check,lambda a:calls.append(a))
            self.assertFalse(calls)
        finally:
            lease.release()
            for t in threads:t.join(timeout=1)
    def test_cross_account_desktop_mutex(self):
        a,ja,w=self.mac();b,jb,_=self.mac('b')
        try:
            a.acquire(ja,w)
            with self.assertRaisesRegex(ValueError,'desktop_busy'):b.acquire(jb,w)
            a.takeover();b.acquire(jb,w)
        finally:a.release();b.release()
    def test_native_window_guard_and_arbitrary_shell_denied(self):
        lease,job,w=self.mac()
        try:
            with self.assertRaises(ValueError):lease.perform(job,lambda:{**w,'window_id':8},lambda a:self.fail('must not execute'))
            for action in ({'op':'shell','command':'id'},{'op':'key','bundle_id':'fixture.editor','window_id':7,'key':'cmd+v'}):
                with self.assertRaises(ValueError):validate_action(action)
        finally:lease.release()
    def test_expired_native_grant_denies(self):
        lease,job,w=self.mac();job['deadline']=time.time()-1
        with self.assertRaises(ValueError):lease.acquire(job,w)
        lease.release()
    def test_native_occluded_click_denied(self):
        lease,job,w=self.mac();w['blocked_regions']=[[10,10,50,50]]
        try:
            with self.assertRaisesRegex(ValueError,'native_click_occluded'):lease.perform(job,lambda:w,lambda a:self.fail('must not click overlay'))
        finally:lease.release()
    def native_modules(self,top='window',duplicate=False):
        front=SimpleNamespace(processIdentifier=lambda:42,bundleIdentifier=lambda:'fixture.editor')
        items=[{'layer':0,'pid':42,'id':8,'bounds':{'X':10,'Y':10,'Width':66,'Height':20}},
               {'layer':0,'pid':42,'id':7,'bounds':{'X':0,'Y':0,'Width':100,'Height':100}}]
        if duplicate:items.append({**items[-1],'id':9})
        q=SimpleNamespace(kCGWindowListOptionOnScreenOnly=1,kCGWindowListExcludeDesktopElements=2,kCGNullWindowID=0,
            kCFRunLoopDefaultMode='default',CFRunLoopRunInMode=lambda *args:None,
            kCGWindowLayer='layer',kCGWindowOwnerPID='pid',kCGWindowNumber='id',kCGWindowBounds='bounds',kCGWindowAlpha='alpha',CGWindowListCopyWindowInfo=lambda *a:items)
        values={('app','focused'):'window',('app','element'):'content',('content','top'):top,('window','position'):SimpleNamespace(x=0,y=0),('window','size'):SimpleNamespace(width=100,height=100)}
        ax=SimpleNamespace(AXUIElementCreateApplication=lambda pid:'app',AXUIElementCopyAttributeValue=lambda e,k,o:(0,values.get((e,k))),
            kAXFocusedWindowAttribute='focused',kAXFocusedUIElementAttribute='element',kAXTopLevelUIElementAttribute='top',kAXPositionAttribute='position',kAXSizeAttribute='size',
            kAXValueCGPointType=1,kAXValueCGSizeType=2,AXValueGetValue=lambda value,kind,out:(True,value))
        return {'Quartz':q,'HIServices':ax,'CoreFoundation':SimpleNamespace(CFEqual=lambda a,b:a==b),
                'AppKit':SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda:SimpleNamespace(frontmostApplication=lambda:front)))}
    def test_native_ax_focus_selects_page_and_retains_control_occlusion(self):
        with patch.dict(sys.modules,self.native_modules()):w=native_window()
        self.assertEqual(w['window_id'],7);self.assertEqual(w['blocked_regions'],[[10,10,66,20]])
    def test_native_ax_modal_and_ambiguous_window_denied(self):
        for modules in (self.native_modules(top='sheet'),self.native_modules(duplicate=True)):
            with patch.dict(sys.modules,modules):self.assertEqual(native_window(),{})

import test_isolation_m2 as m2
from carme.execution import encoded,signature
import secrets

class M3Runtime(m2.M2):
    async def mac_pair(self):
        self.mac_secret=secrets.token_hex(32).encode();key=self.root/'mac-key';key.write_bytes(self.mac_secret)
        self.config.isolation['mac_runner']={'runner_id':'fixture-mac','key_file':str(key)}
        self.config.isolation['targets']['fixture-mac']={'tools':['mac_action']}
        spec=self.config.agents.get('bot');spec.engine='api';spec.execution_target='macos';spec.execution_target_id='fixture-mac';spec.tools=['mac_action']
        _,meta=self.runtime._task_agent_snapshot('bot');self.mac_run=self.store.create_task('bot','synthetic native',meta=meta)
        self.store.set_task_status(self.mac_run,'running')
    async def mac_call(self,data,key=None,nonce=None):
        data={'runner_id':'fixture-mac',**data};raw=encoded(data);nonce=nonce or f'{time.time()}:'+secrets.token_hex(16)
        return await self.client.post('/internal/mac',content=raw,headers={'X-Carme-Nonce':nonce,'X-Carme-Signature':signature(key or self.mac_secret,nonce,raw)})
    async def test_m3_native_identity_separation_and_replay(self):
        await self.mac_pair();data={'op':'claim','authorized':True};nonce=f'{time.time()}:'+secrets.token_hex(16)
        self.assertEqual((await self.mac_call(data,key=self.key)).status_code,403)
        self.assertEqual((await self.mac_call(data,nonce=nonce)).status_code,200)
        self.assertEqual((await self.mac_call(data,nonce=nonce)).status_code,403)
        with patch.dict(os.environ,{'CARME_TOKEN':'synthetic-admin','CARME_DEV_NO_AUTH':'0'}):
            self.assertEqual((await self.client.post('/api/session',headers={'Authorization':'Bearer '+self.mac_secret.decode()})).status_code,401)
        raw=encoded({'op':'claim'});nonce=f'{time.time()}:'+secrets.token_hex(16)
        self.assertEqual((await self.client.post('/internal/broker',content=raw,headers={'X-Carme-Nonce':nonce,'X-Carme-Signature':signature(self.mac_secret,nonce,raw)})).status_code,403)
    async def test_m3_control_takeover_cancels_queue_and_policy_revokes(self):
        await self.mac_pair();await self.mac_call({'op':'claim','authorized':True})
        action={'op':'key','bundle_id':'fixture','window_id':7,'key':'tab'}
        task=asyncio.create_task(self.runtime.execution.native(self.mac_run,action));await asyncio.sleep(0)
        answer=(await self.mac_call({'op':'claim','authorized':True})).json();self.assertIn('job',answer)
        await self.mac_call({'op':'claim','authorized':False});self.assertIn('error',await task);self.assertFalse(self.runtime.execution.mac_jobs)
        await self.mac_call({'op':'claim','authorized':True})
        task=asyncio.create_task(self.runtime.execution.native(self.mac_run,action));await asyncio.sleep(0)
        self.config.agents.get('bot').tools=[]
        with self.assertRaises(RuntimeError):await task
        self.assertFalse(self.runtime.execution.mac_jobs)
    async def test_m3_unpaired_native_and_cli_combination_denied(self):
        spec=self.config.agents.get('bot');spec.execution_target='macos';spec.execution_target_id='missing'
        with self.assertRaises(ValueError):self.runtime._task_agent_snapshot('bot')
        await self.mac_pair();spec.engine='pi'
        with self.assertRaisesRegex(ValueError,'native_engine_combination_unsupported'):self.runtime._task_agent_snapshot('bot')
    async def test_m3_persistent_maintenance_denies_admission(self):
        p.save(self.store.path.parent/'admission-paused.json',{'paused':True})
        with self.assertRaisesRegex(ValueError,'maintenance_paused'):await self.runtime.submit('bot','must not start')
        self.assertFalse(self.runtime._jobs)

if __name__=='__main__':
    suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(M3),
        unittest.TestSuite(M3Runtime(name) for name in M3Runtime.__dict__ if name.startswith('test_m3_'))])
    result=unittest.TextTestRunner(verbosity=2).run(suite);raise SystemExit(not result.wasSuccessful())
