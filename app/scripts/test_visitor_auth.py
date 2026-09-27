"""M2: real Gateway -> Control routers, synthetic accounts/SQLite, no provider I/O."""
import os
import secrets
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from test_gateway import GatewayTests, PASSWORD
from carme.gateway import create_gateway, csrf, database, digest
from carme.api.routes import build_router, require_token
from carme.security import visitor_proof, visitor_key, new_visitor_password
from carme.store import Store
import jwt


class VisitorAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        GatewayTests.setUpClass.__func__(cls)

    def setUp(self):
        GatewayTests.setUp(self)
        self.client.__exit__(None, None, None)
        self.stores, self.apps, self.visitors, self.groups = {}, {}, {}, {}
        for i, name in enumerate(('alice', 'bob')):
            store = Store(self.home / (name + '.db'))
            cid = store.create_conversation(['a', 'b'])['id']
            v = store.create_visitor(cid, 'Guest', self.hashed, actor_key='owner', expected_revision=0)
            config = SimpleNamespace(isolation={'broker': {'instance_id': 'carme-' + str(i)*20}})
            app = FastAPI()
            app.state.config, app.state.store = config, store
            app.include_router(build_router(config, store, None), dependencies=[Depends(require_token)])
            self.stores[name], self.apps[name], self.visitors[name], self.groups[name] = store, app, v, cid
        outer = self
        class Transport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if request.url.host == 'fixture.cloudflareaccess.com':
                    return httpx.Response(200, json={'keys': [outer.jwk]})
                outer.requests.append(request)
                name = 'alice' if request.url.port == 19000 else 'bob'
                with patch.dict(os.environ, {'CARME_TOKEN': outer.tokens[name], 'CARME_ACCOUNT_ID': name}):
                    return await httpx.ASGITransport(app=outer.apps[name]).handle_async_request(request)
        self.transport = Transport()
        self.client = TestClient(create_gateway(self.config, transport=self.transport), base_url=self.config['origin'])
        self.client.__enter__()

    def tearDown(self):
        for store in self.stores.values():
            store.close()
        GatewayTests.tearDown(self)

    def headers(self):
        h = {'Origin': str(self.client.base_url).rstrip('/')}
        if self.client.cookies.get('carme_visitor'):
            h['X-Carme-CSRF'] = csrf(self.client.cookies['carme_visitor'])
        return h

    def login(self, name='alice', **changes):
        data = {'username': self.visitors[name]['username'], 'password': PASSWORD, 'conversation_id': self.groups[name]}
        data.update(changes)
        return self.client.post(f'/api/visitor/{name}/login', json=data, headers=self.headers())

    def change(self, action, **kw):
        s = self.stores['alice']; v = self.visitors['alice']; cid = self.groups['alice']
        return s.update_visitor(cid, v['id'], action, actor_key='owner', expected_revision=s.get_conversation(cid)['access_revision'], **kw)

    def direct(self, path='/api/visitor/alice/session', method='GET', body=b'', headers=None):
        with patch.dict(os.environ, {'CARME_TOKEN': self.tokens['alice'], 'CARME_ACCOUNT_ID': 'alice'}):
            with TestClient(self.apps['alice']) as c:
                return c.request(method, path, content=body, headers=headers or {})

    def proof_headers(self, path='/api/visitor/alice/session', method='GET', body=b'', secret=None, **kw):
        secret = secret if secret is not None else self.client.cookies.get('carme_visitor', '')
        args = dict(account='alice', instance='carme-'+'0'*20, method=method, target=path, body=body,
                    secret=secret, subject=digest('local-preview'), access_expires=time.time()+60, account_version=1)
        args.update(kw)
        return {'X-Carme-Visitor-Session': secret, 'X-Carme-Visitor-Proof': visitor_proof(self.tokens['alice'], **args)}

    def test_login_session_logout_and_hash_only(self):
        r = self.login(); self.assertEqual(r.status_code, 200, r.text)
        secret = self.client.cookies['carme_visitor']
        self.assertNotIn(secret, r.text); self.assertNotIn(self.tokens['alice'], r.text)
        self.assertIn('HttpOnly', r.headers['set-cookie']); self.assertIn('SameSite=strict', r.headers['set-cookie'])
        row = self.stores['alice']._query_one('SELECT * FROM visitor_sessions')
        self.assertEqual(row['secret_hash'], digest(secret)); self.assertLessEqual(row['expires_at'], time.time()+604800)
        r = self.client.get('/api/visitor/alice/session'); self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['session']['conversation_id'], self.groups['alice'])
        self.assertEqual(r.headers['cache-control'], 'no-store')
        for req in self.requests:
            self.assertNotIn('authorization', req.headers); self.assertNotIn('cookie', req.headers)
        self.assertEqual(self.client.delete('/api/visitor/alice/session', headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)

    def test_visitor_has_no_owner_or_data_access(self):
        self.assertEqual(self.login().status_code, 200)
        for p in ('/api/session', '/api/conversations', '/api/agents', '/api/visitor/alice/approvals', '/api/visitor/alice/session/extra'):
            self.assertIn(self.client.get(p).status_code, (401, 403))
        for p in ('/api/visitor/alice/session', '/api/conversations'):
            for h in ({'Authorization': 'Bearer '+self.tokens['alice'], 'X-Carme-Visitor-Session': self.client.cookies['carme_visitor']}, {'Cookie': 'carme_visitor='+self.client.cookies['carme_visitor']}, {'X-Carme-Visitor-ID': self.visitors['alice']['id']}):
                self.assertIn(self.direct(p, headers=h).status_code, (401, 403))

    def test_forged_transport_and_no_admin_fallback(self):
        self.login(); h = self.proof_headers()
        self.assertEqual(self.direct(headers=h).status_code, 200)
        for changes in ({'account': 'bob'}, {'instance': 'carme-'+'1'*20}, {'method': 'POST'}, {'target': '/api/visitor/alice/session?different=1'}, {'body': b'changed'}, {'secret': 'x'*43}, {'subject': digest('other')}, {'access_expires': time.time()-1}):
            self.assertEqual(self.direct(headers=self.proof_headers(**changes)).status_code, 401, changes)
        h['Authorization'] = 'Bearer '+self.tokens['alice']
        self.assertEqual(self.direct(headers=h).status_code, 401)
        h = self.proof_headers(); h['X-Carme-Visitor-Proof'] += 'tampered'
        self.assertEqual(self.direct(headers=h).status_code, 401)
        self.assertEqual(self.direct('/api/conversations', headers=self.proof_headers(path='/api/conversations')).status_code, 403)

    def test_csrf_and_untrusted_browser_headers(self):
        self.login()
        self.assertEqual(self.client.delete('/api/visitor/alice/session', headers={'Origin': self.config['origin']}).status_code, 403)
        self.assertEqual(self.client.delete('/api/visitor/alice/session', headers={**self.headers(), 'Origin': 'https://evil.test'}).status_code, 403)
        self.assertEqual(self.client.get('/api/visitor/alice/session', headers={'X-Carme-Visitor-Proof': 'fake'}).status_code, 401)
        self.assertEqual(self.client.get('/api/visitor/alice/session?token=x').status_code, 401)
        self.assertEqual(self.client.get('/api/visitor/alice/session', headers={'Host': 'evil.test'}).status_code, 421)

    def test_cross_account_group_and_disabled_account(self):
        self.assertEqual(self.login(conversation_id=self.groups['bob']).status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get('/api/visitor/bob/session').status_code, 401)
        self.assertEqual(self.client.get('/api/visitor/missing/session').status_code, 401)
        with database(self.home) as db:
            db.execute("UPDATE users SET enabled=0 WHERE account='alice'")
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)

    def test_reset_remove_reinvite_and_expiry(self):
        for action in ('reset_password', 'remove'):
            self.assertEqual(self.login().status_code, 200)
            self.change(action, **({'password_hash': self.hashed} if action == 'reset_password' else {}))
            self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)
        self.assertEqual(self.login().status_code, 401)
        self.change('reinvite', password_hash=self.hashed)
        self.assertEqual(self.login().status_code, 200)
        self.stores['alice']._write('UPDATE visitor_sessions SET expires_at=0')
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)

    def test_account_disable_reenable_does_not_revive_cookie(self):
        self.login()
        with database(self.home) as db:
            db.execute("UPDATE users SET enabled=0,version=version+1 WHERE account='alice'")
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)
        with database(self.home) as db:
            db.execute("UPDATE users SET enabled=1,version=version+1 WHERE account='alice'")
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)
        self.assertEqual(self.login().status_code, 200)

    def test_token_rotation(self):
        self.login()
        self.tokens['alice'] = secrets.token_hex(32)
        (self.home/'accounts/alice/runtime/secrets/control-token').write_text(self.tokens['alice'])
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)

    def test_reset_during_password_work_cannot_issue(self):
        store = self.stores['alice']; old = store.visitor_credential(self.visitors['alice']['username'])
        self.change('reset_password', password_hash=self.hashed)
        with self.assertRaises(ValueError):
            store.issue_visitor_session(old, digest('local-preview'), self.tokens['alice'], time.time()+60)
        self.assertEqual(store._query_one('SELECT COUNT(*) n FROM visitor_sessions')['n'], 0)

    def test_login_rate_limit_is_persistent_and_uniform(self):
        for i in range(8):
            self.assertEqual(self.login(password='wrong').status_code, 401)
        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(self.login().status_code, 429)
        self.assertEqual(self.login(username='not-found').status_code, 401)
        rows = self.stores['alice']._query('SELECT key FROM visitor_login_attempts')
        self.assertNotIn(self.visitors['alice']['username'], str(rows))
        # Closing/reopening storage preserves throttling.
        other = Store(self.stores['alice'].path)
        try:
            self.assertTrue(other.visitor_login_throttle(self.visitors['alice']['username'], digest('local-preview')))
        finally:
            other.close()

    def test_owner_and_visitor_cookie_coexist(self):
        self.assertEqual(GatewayTests.login(self).status_code, 200)
        owner_cookie = self.client.cookies['carme_login']
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.client.get('/api/session').status_code, 200)
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 200)
        self.assertEqual(self.client.cookies['carme_login'], owner_cookie)
        self.assertEqual(self.client.delete('/api/visitor/alice/session', headers=self.headers()).status_code, 200)
        self.assertEqual(self.client.get('/api/session').status_code, 200)

    def test_body_limit_and_invalid_json(self):
        for data, code in ((b'x'*4097, 413), (b'{', 400), (b'[]', 400)):
            r = self.client.post('/api/visitor/alice/login', content=data, headers={**self.headers(), 'Content-Type': 'application/json'})
            self.assertEqual(r.status_code, code)
        h = self.proof_headers(path='/api/visitor/alice/login', method='POST', body=b'x'*4097)
        self.assertEqual(self.direct('/api/visitor/alice/login', 'POST', b'x'*4097, h).status_code, 413)

    def test_jwt_algorithm_claims_and_exact_target(self):
        self.login()
        h = self.proof_headers()
        claims = jwt.decode(h['X-Carme-Visitor-Proof'], options={'verify_signature': False})
        for changes in ({'iss': 'other'}, {'aud': 'other'}, {'aud': ['carme-visitor-control']}, {'exp': time.time()-1},
                        {'exp': time.time()+300}, {'iat': time.time()+60}, {'access_exp': 0}, {'account_version': 0}, {'account_version': True}):
            forged = dict(h)
            forged['X-Carme-Visitor-Proof'] = jwt.encode({**claims, **changes}, visitor_key(self.tokens['alice']), algorithm='HS256')
            self.assertEqual(self.direct(headers=forged).status_code, 401)
        for alg in ('none', 'HS384'):
            forged = dict(h)
            forged['X-Carme-Visitor-Proof'] = jwt.encode(claims, '' if alg == 'none' else visitor_key(self.tokens['alice']) + b'synthetic-padding', algorithm=alg)
            self.assertEqual(self.direct(headers=forged).status_code, 401)
        self.assertEqual(self.direct('/api/visitor/alice/session?x=1', headers=h).status_code, 401)
        self.assertEqual(self.direct(body=b'changed', headers=h).status_code, 401)
        del claims['sub']
        h['X-Carme-Visitor-Proof'] = jwt.encode(claims, visitor_key(self.tokens['alice']), algorithm='HS256')
        self.assertEqual(self.direct(headers=h).status_code, 401)

    def test_new_password_and_session_rotation(self):
        self.assertEqual(self.login().status_code, 200)
        old = self.client.cookies['carme_visitor']
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.direct(headers=self.proof_headers(secret=old)).status_code, 401)
        new_password, verifier = new_visitor_password()
        self.change('reset_password', password_hash=verifier)
        self.assertEqual(self.login().status_code, 401)
        self.assertEqual(self.login(password=new_password).status_code, 200)
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 200)

    def test_generated_initial_password_is_not_persisted_or_read_back(self):
        password, verifier = new_visitor_password()
        s = self.stores['alice']; cid = self.groups['alice']
        v = s.create_visitor(cid, 'Second visitor', verifier, actor_key='owner', expected_revision=s.get_conversation(cid)['access_revision'])
        self.assertGreaterEqual(len(password), 24)
        self.assertNotIn(password, str(s._query('SELECT * FROM visitors')))
        self.assertNotIn('password_hash', s.get_visitor(cid, v['id']))
        self.assertEqual(self.login(username=v['username'], password=password).status_code, 200)

    def test_group_deletion_invalidates_session(self):
        self.login()
        self.stores['alice']._write('UPDATE conversations SET deleted_at=1 WHERE id=?', (self.groups['alice'],))
        self.assertEqual(self.client.get('/api/visitor/alice/session').status_code, 401)
        self.assertEqual(self.login().status_code, 401)

    def test_public_access_required_bound_and_secure(self):
        config = dict(self.config, origin='https://fixture.example', local_only=False, access_team='fixture', access_audience='a'*64)
        with TestClient(create_gateway(config, transport=self.transport), base_url=config['origin']) as c:
            self.assertEqual(c.get('/api/visitor/alice/session').status_code, 401)
            token = GatewayTests.cf_token(self)
            headers = {'Origin': config['origin'], 'Cf-Access-Jwt-Assertion': token}
            r = c.post('/api/visitor/alice/login', json={'username':self.visitors['alice']['username'], 'password':PASSWORD, 'conversation_id':self.groups['alice']}, headers=headers)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn('Secure', r.headers['set-cookie'])
            self.assertLessEqual(r.json()['session']['expires_at'], time.time()+301)
            self.assertEqual(c.get('/api/visitor/alice/session', headers=headers).status_code, 200)
            other = GatewayTests.cf_token(self, sub='another-user')
            self.assertEqual(c.get('/api/visitor/alice/session', headers={**headers, 'Cf-Access-Jwt-Assertion':other}).status_code, 401)


if __name__ == '__main__':
    unittest.main()
