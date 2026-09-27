"""M7 owner provisioning and visitor shell contracts, synthetic data only."""
import asyncio,json,os,unittest
from unittest.mock import patch
import httpx
from fastapi import FastAPI,Depends
import test_visitor_execution as execution
from carme.api.routes import build_router,require_token
from carme.security import password_matches

class InviteTests(unittest.IsolatedAsyncioTestCase):
 async def asyncSetUp(self):
  self.f=execution.ExecutionTests();await self.f.asyncSetUp();self.s,self.r,self.cid=self.f.s,self.f.r,self.f.cid
  self.env=patch.dict(os.environ,{'CARME_TOKEN':'synthetic-token','CARME_ACCOUNT_ID':'alice'});self.env.start()
  app=FastAPI();app.state.config=self.r.config;app.state.store=self.s;app.include_router(build_router(self.r.config,self.s,self.r),dependencies=[Depends(require_token)])
  self.c=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test',headers={'Authorization':'Bearer synthetic-token'})
  self.path=f'/api/conversations/{self.cid}/visitors'
 async def asyncTearDown(self):
  await self.c.aclose();self.env.stop();await self.f.asyncTearDown()
 async def test_create_password_once_capacity_and_cas(self):
  row=(await self.c.get(self.path)).json();self.assertNotIn('password',json.dumps(row));self.assertEqual(row['invite_path'],f'/visit/alice/{self.cid}')
  created=await self.c.post(self.path,json={'display_name':'Two','expected_revision':1});self.assertEqual(created.status_code,200,created.text)
  result=created.json();self.assertTrue(password_matches(result['password'],self.s.visitor_credential(result['visitor']['username'])['password_hash']))
  self.assertNotIn(result['password'],(await self.c.get(self.path)).text);self.assertEqual(created.headers['cache-control'],'no-store')
  self.assertEqual((await self.c.post(self.path,json={'display_name':'stale','expected_revision':1})).status_code,409)
  self.assertEqual((await self.c.post(self.path,json={'display_name':'Three','expected_revision':2})).status_code,200)
  self.assertEqual((await self.c.post(self.path,json={'display_name':'Four','expected_revision':3})).status_code,409)
 async def test_reset_remove_reinvite_and_history(self):
  v=self.f.f.v;path=self.path+'/'+v['id']
  reset=(await self.c.patch(path,json={'action':'reset_password','expected_revision':1})).json()
  self.assertTrue(password_matches(reset['password'],self.s.visitor_credential(v['username'])['password_hash']))
  self.assertEqual((await self.c.patch(path,json={'action':'history','expected_revision':1,'allow_history':True})).status_code,200)
  self.assertEqual((await self.c.patch(path,json={'action':'remove','expected_revision':2})).status_code,200)
  result=await self.c.patch(path,json={'action':'reinvite','expected_revision':3});self.assertEqual(result.status_code,200,result.text)
  self.assertIn('password',result.json());self.assertEqual(result.json()['visitor']['membership_version'],3)
 async def test_no_client_identity_secret_or_direct_group(self):
  for extra in ({'password':'chosen'},{'actor_key':'owner'},{'username':'chosen'},{'enabled':True}):
   r=await self.c.post(self.path,json={'display_name':'x','expected_revision':1,**extra});self.assertEqual(r.status_code,422)
  direct=self.s.create_conversation(['chief'])['id'];self.assertEqual((await self.c.get(f'/api/conversations/{direct}/visitors')).status_code,404)
  self.assertEqual((await self.c.patch(self.path+'/'+self.f.f.v['id'],content=b'{')).status_code,422)
 async def test_create_revokes_old_tasks(self):
  tid=self.f.staged();r=await self.c.post(self.path,json={'display_name':'Two','expected_revision':1});self.assertEqual(r.status_code,200)
  self.assertEqual(self.s.get_task(tid)['status'],'cancelled')


class ShellTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  import test_visitor_auth as auth
  auth.VisitorAuthTests.setUpClass()
 def setUp(self):
  import test_visitor_auth as auth
  self.f=auth.VisitorAuthTests();self.f.setUp()
 def tearDown(self):self.f.tearDown()
 def test_shell_no_identity_no_store_and_access_required(self):
  from carme.gateway import create_gateway
  from fastapi.testclient import TestClient
  path='/visit/alice/'+self.f.groups['alice']
  result=self.f.client.get(path);self.assertEqual(result.status_code,200);self.assertEqual(result.headers['cache-control'],'no-store')
  self.assertNotIn(self.f.tokens['alice'],result.text);self.assertNotIn('data-carme-account',result.text)
  self.assertEqual(self.f.client.get('/visit/alice/not-a-group').status_code,404)
  config={**self.f.config,'origin':'https://carme.example.test','local_only':False,'access_team':'fixture','access_audience':'a'*64}
  with TestClient(create_gateway(config,transport=self.f.transport),base_url=config['origin']) as c:
   self.assertEqual(c.get(path).status_code,401)
 def test_expired_session_cookie_cleared_for_new_login(self):
  self.assertEqual(self.f.login().status_code,200);self.f.change('reset_password',password_hash=self.f.hashed)
  self.assertEqual(self.f.client.get('/api/visitor/alice/session').status_code,401)
  self.assertFalse(self.f.client.cookies.get('carme_visitor'));self.assertEqual(self.f.login().status_code,200)

if __name__=='__main__':unittest.main(verbosity=2)
