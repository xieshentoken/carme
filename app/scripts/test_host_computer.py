"""Signed main-only host routing. Synthetic runner; never touches the Mac UI."""
import asyncio,copy,importlib.util,json,os,secrets,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch,AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
import test_bot_desktop as desktop
from carme.execution import build_execution_router,encoded,signature
from fastapi import FastAPI

class Host(desktop.Desktop):
    async def setup_host(self):
        e=self.configure();key=self.root/'host-key';key.write_bytes(b'h'*32)
        self.config.isolation['mac_runner']={'runner_id':'fixture-mac','key_file':str(key),'computer_use':True}
        broker=self.root/'broker-key';broker.write_bytes(b'b'*32)
        self.config.isolation['broker']={'key_file':str(broker)}
        app=FastAPI();app.include_router(build_execution_router(e))
        client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1')
        async def call(op,**kwargs):
            raw=encoded({'op':op,'runner_id':'fixture-mac',**kwargs});nonce=f'{time.time():.3f}:'+secrets.token_hex(16)
            return await client.post('/internal/mac',content=raw,headers={'X-Carme-Nonce':nonce,'X-Carme-Signature':signature(b'h'*32,nonce,raw)})
        return e,client,call
    async def test_only_main_can_receive_admin_host_grant(self):
        e,c,call=await self.setup_host()
        async with c:
            grant={'bot_id':'bot','generation':'a'*32,'expires':time.time()+60}
            with patch.dict(os.environ,{'CARME_ACCOUNT_ID':'other'}):
                self.assertEqual((await call('computer_claim',grant=grant)).status_code,403)
                self.assertEqual(e.desktop_target('bot'),'linux')
            with patch.dict(os.environ,{'CARME_ACCOUNT_ID':'main'}):
                self.assertEqual((await call('computer_claim',grant=grant)).status_code,200)
                self.assertEqual(e.desktop_target('bot'),'host:'+'a'*32)
                self.assertEqual(e.desktop_target('another'),'linux')
                self.assertEqual((await call('computer_claim',grant={**grant,'expires':time.time()+3600})).status_code,400)
    async def test_signed_queue_result_and_revoke_no_replay(self):
        e,c,call=await self.setup_host()
        async with c:
            with patch.dict(os.environ,{'CARME_ACCOUNT_ID':'main'}):
                grant={'bot_id':'bot','generation':'a'*32,'expires':time.time()+60}
                await call('computer_claim',grant=grant)
                pending=asyncio.create_task(e.desktop_request('bot','status'))
                await asyncio.sleep(.01)
                request=(await call('computer_claim',grant=grant)).json()
                self.assertEqual(request['bot_id'],'bot');self.assertTrue(request['target'].startswith('host:'))
                self.assertTrue((await call('computer_check',id=request['id'])).json()['active'])
                await call('computer_finish',id=request['id'],result={'mode':'host','available':True,'screen_width':1728,'screen_height':1117})
                result=await pending;self.assertEqual(result['mode'],'host')
                self.assertEqual(e.host_screen_size,(1728,1117))
                self.assertEqual(e.health()['mac_runner'],'authorized')
                pending=asyncio.create_task(e.desktop_request('bot','screenshot'));await asyncio.sleep(.01)
                request=(await call('computer_claim',grant=grant)).json()
                await call('computer_claim',grant={})
                self.assertFalse((await call('computer_check',id=request['id'])).json()['active'])
                with self.assertRaisesRegex(RuntimeError,'target_changed_no_replay'):await pending
                self.assertEqual(e.desktop_target('bot'),'linux')
                self.assertFalse(e.jobs)
    async def test_target_change_binds_policy_and_shared_human_control(self):
        e,c,call=await self.setup_host()
        before=self.runtime.policy_for('bot',target='container',node={})
        other=copy.deepcopy(self.config.agents.get('bot'));other.id='other';self.config.agents.agents['other']=other
        async with c:
            with patch.dict(os.environ,{'CARME_ACCOUNT_ID':'main'}):
                grant={'bot_id':'*','generation':'a'*32,'expires':time.time()+60}
                await call('computer_claim',grant=grant)
                after=self.runtime.policy_for('bot',target='container',node={})
                self.assertNotEqual(before['permission_version'],after['permission_version'])
                self.assertEqual(after['computer_target'],'host:'+'a'*32)
                with patch.object(e,'desktop_request',AsyncMock(return_value={'ok':True})):
                    token=(await e.set_desktop_control('bot',True))['control_id']
                    self.assertTrue(e.desktop_control('other')['enabled'])
                    with self.assertRaises(RuntimeError):await e.set_desktop_control('other',True)
                    await e.set_desktop_control('bot',False,token)
                await call('computer_claim',grant={})
                self.assertFalse(e.desktop_control('bot')['enabled'])
    async def test_host_denies_shell_and_override(self):
        e,c,call=await self.setup_host()
        async with c:
            with patch.dict(os.environ,{'CARME_ACCOUNT_ID':'main'}):
                await call('computer_claim',grant={'bot_id':'bot','generation':'a'*32,'expires':time.time()+60})
                for op,args in [('shell',{'command':'true'}),('web',{'name':'web_open','arguments':{'url':'https://example.com'}})]:
                    with self.assertRaisesRegex(RuntimeError,'bot_computer'):await e.desktop_request('bot',op,args)


class AdminHost(unittest.IsolatedAsyncioTestCase):
    async def test_admin_login_csrf_consent_main_scope_and_revoke(self):
        # A separate admin database and synthetic runner; no real grant or password.
        spec=importlib.util.spec_from_file_location('carme_test_admin',Path(__file__).resolve().parents[2]/'admin/admin_app.py')
        admin=importlib.util.module_from_spec(spec);spec.loader.exec_module(admin)
        with tempfile.TemporaryDirectory(prefix='carme-admin-test-') as raw:
            root=Path(raw).resolve();area=root/'admin';area.mkdir()
            for name in ('main','other'):
                home=root/'runtime/docker/accounts'/name;home.mkdir(parents=True);(home/'account.json').write_text('{}')
            with patch.object(admin,'ROOT',root),patch.object(admin,'ADMIN',area):
                admin.initialize()
                with admin.admin_database() as db:
                    db.execute("INSERT INTO admins(name,password,must_change) VALUES('fixture','unused',0)")
                app=admin.create_app()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1:8897') as client:
                    url='/api/accounts/main/mac/grant-computer'
                    self.assertEqual((await client.post(url,json={})).status_code,401)
                    secret='x'*43;client.cookies.set(admin.COOKIE,secret)
                    with patch.object(admin,'session_row',return_value={'must_change':0,'admin':'fixture'}),patch.object(admin,'run_mac_cli',return_value={}) as run,patch.object(admin,'mac_status',return_value={'runner_running':True,'state_live':True,'state':'ready'}):
                        self.assertEqual((await client.post(url,json={})).status_code,403)
                        client.headers['X-Admin-CSRF']=admin.admin_csrf(secret)
                        self.assertEqual((await client.post('/api/accounts/other/mac/grant-computer',json={'allow_gui':True})).status_code,403)
                        self.assertEqual((await client.post(url,json={})).status_code,400)
                        self.assertEqual((await client.post(url,json={'allow_gui':True,'seconds':601})).status_code,400)
                        run.assert_not_called()
                        self.assertEqual((await client.post(url,json={'allow_gui':True,'seconds':60,'bot_id':'fixture'})).status_code,200)
                        self.assertIn('--computer-use',run.call_args.args)
                        self.assertEqual((await client.post('/api/accounts/main/mac/revoke-computer',json={})).status_code,200)
                        self.assertEqual(run.call_args.args[-1],'revoke')

if __name__=='__main__':unittest.main(verbosity=2)
