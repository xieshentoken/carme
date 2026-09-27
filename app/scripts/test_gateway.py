"""Authentication and cross-account routing regressions, using temporary synthetic data."""
import json
import secrets
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from carme.gateway import COOKIE, account_backend, create_gateway, database, digest, initialize, password_hash, set_password

PASSWORD = 'synthetic-account-password-123'
NEW_PASSWORD = 'synthetic-new-password-456'


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hashed = password_hash(PASSWORD)
        cls.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(cls.private.public_key()))
        cls.jwk.update(kid='synthetic-key', alg='RS256', use='sig')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='carme-login-unit-')
        self.home = Path(self.tmp.name).resolve()
        self.web = self.home/'web'; self.web.mkdir()
        (self.web/'index.html').write_text('<html lang="zh-CN"><body>Carme fixture</body></html>')
        (self.home/'installation.json').write_text(json.dumps({'id':'fixture','home':str(self.home)}))
        initialize(self.home)
        self.tokens={}
        for index, name in enumerate(('alice','bob')):
            base=self.home/'accounts'/name; (base/'runtime/secrets').mkdir(parents=True)
            instance='carme-'+str(index)*20
            acc={'id':name,'home':str(base),'installation':'fixture','instance_id':instance,
                 'port':19000+index,'origin':f'http://c{index:016x}.localhost:{19000+index}'}
            (base/'account.json').write_text(json.dumps(acc))
            self.tokens[name]=secrets.token_hex(32)
            (base/'runtime/secrets/control-token').write_text(self.tokens[name])
            with database(self.home) as db:
                db.execute('INSERT INTO users(account,instance,password) VALUES(?,?,?)',(name,instance,self.hashed))
        self.requests=[]
        def backend(request):
            if request.url.host=='fixture.cloudflareaccess.com':
                return httpx.Response(200,json={'keys':[self.jwk]})
            self.requests.append(request)
            owner='alice' if request.url.port==19000 else 'bob'
            if request.url.path=='/api/redirect':return httpx.Response(302,headers={'Location':'http://backend.invalid'})
            if request.url.path=='/api/file':return httpx.Response(200,content=b'private-file-'+owner.encode(),headers={'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename=fixture.txt'})
            if request.url.path=='/api/conversations/alice-private' and owner=='bob':return httpx.Response(404,json={'detail':'missing'})
            return httpx.Response(200,json={'owner':owner},headers={'Set-Cookie':'leaked=secret; Path=/'})
        self.transport=httpx.MockTransport(backend)
        self.config={'home':str(self.home),'web_dir':str(self.web),'origin':'http://127.0.0.1:18998','port':18998,'local_only':True}
        self.client=TestClient(create_gateway(self.config,transport=self.transport),base_url=self.config['origin'])
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None,None,None);self.tmp.cleanup()

    def login(self,name='alice',password=PASSWORD,client=None):
        client=client or self.client
        headers={'Origin':str(client.base_url).rstrip('/')}
        if COOKIE in client.cookies:
            r=client.get('/api/session')
            if r.status_code==200:headers['X-Carme-CSRF']=r.json()['csrf']
        return client.post('/api/login',json={'username':name,'password':password},headers=headers)

    def headers(self):
        return {'Origin':self.config['origin'],'X-Carme-CSRF':self.client.get('/api/session').json()['csrf']}

    def test_anonymous_cannot_access_any_account_data(self):
        self.assertIn('登录 Carme',self.client.get('/').text)
        for path in ('/api/stats','/api/conversations','/api/events','/api/file'):
            self.assertEqual(self.client.get(path).status_code,401)
        self.assertEqual(self.requests,[])

    def test_password_is_required_not_cloudflare_or_backend_token(self):
        self.assertEqual(self.login(password='incorrect-password-123').status_code,401)
        for name in ('notfound','alice'):
            self.assertEqual(self.login(name,password='wrong-password-value').json()['detail'],'账号或密码不正确')
        self.assertEqual(self.client.get('/api/stats',headers={'Authorization':'Bearer '+self.tokens['alice']}).status_code,401)
        self.assertEqual(self.client.get('/?token='+self.tokens['alice']).status_code,401)

    def test_login_cookie_secret_never_exposes_backend_token(self):
        r=self.login();self.assertEqual(r.status_code,200)
        self.assertIn('HttpOnly',r.headers['set-cookie']);self.assertIn('SameSite=strict',r.headers['set-cookie'])
        self.assertNotIn('Domain=',r.headers['set-cookie'])
        self.assertIn('Max-Age=604800',r.headers['set-cookie'])
        for token in self.tokens.values():self.assertNotIn(token,r.text+r.headers['set-cookie'])
        self.assertIn('data-carme-account="alice"',self.client.get('/').text)
        with database(self.home) as db:
            row=db.execute('SELECT * FROM sessions').fetchone()
            self.assertEqual(row['secret_hash'],digest(self.client.cookies[COOKIE]))
            self.assertNotIn(self.client.cookies[COOKIE],str(dict(row)))
            self.assertEqual(round(row['expires_at']-row['created_at']),604800)

    def test_csrf_and_origin_on_mutations_and_login(self):
        self.assertEqual(self.client.post('/api/login',json={}).status_code,403)
        self.assertEqual(self.client.post('/api/login',json={},headers={'Origin':'https://evil.invalid'}).status_code,403)
        self.login()
        self.assertEqual(self.client.post('/api/conversations',json={},headers={'Origin':self.config['origin']}).status_code,403)
        self.assertEqual(self.client.post('/api/conversations',json={},headers=self.headers()).status_code,200)

    def test_unicode_password_and_malformed_csrf_fail_safely(self):
        password='仅用于本轮合成验收的中文长密码_123'
        set_password(self.home,'alice',password)
        self.assertEqual(self.login(password=password).status_code,200)
        headers=[(b'Origin',self.config['origin'].encode()),(b'X-Carme-CSRF',b'\xff')]
        self.assertEqual(self.client.post('/api/conversations',json={},headers=headers).status_code,403)

    def test_account_selected_only_by_authenticated_session(self):
        self.login('alice')
        r=self.client.get('/api/stats',headers={'X-Account':'bob','Cookie':'carme_session=forged; '+COOKIE+'='+self.client.cookies[COOKIE]})
        self.assertEqual(r.json()['owner'],'alice')
        self.assertEqual(self.requests[-1].headers['authorization'],'Bearer '+self.tokens['alice'])
        self.assertEqual(self.requests[-1].headers['host'],'c0000000000000000.localhost:19000')
        self.assertNotIn('cookie',self.requests[-1].headers)
        self.assertNotIn('set-cookie',r.headers)
        self.login('bob')
        self.assertEqual(self.client.get('/api/stats').json()['owner'],'bob')
        self.assertNotIn('cookie',self.requests[-1].headers)
        self.assertEqual(self.client.get('/api/conversations/alice-private').status_code,404)

    def test_stale_tab_cannot_read_or_write_as_other_account(self):
        self.login('alice');old=dict(self.headers());old['X-Carme-Account']='alice'
        self.login('bob')
        for method in ('get','post'):
            self.assertEqual(getattr(self.client,method)('/api/stats',headers=old).status_code,409)
        self.assertEqual(self.client.get('/api/events?account=alice').status_code,409)

    def test_logout_revokes_cookie_server_side(self):
        self.login();cookie=self.client.cookies[COOKIE]
        self.assertEqual(self.client.delete('/api/session',headers=self.headers()).status_code,200)
        self.client.cookies.set(COOKIE,cookie)
        self.assertEqual(self.client.get('/api/stats').status_code,401)

    def test_session_revocation_is_scoped_to_current_account(self):
        self.login('alice');sid=self.client.get('/api/session').json()['session']['id']
        self.client.cookies.clear();self.login('bob')
        self.assertEqual(self.client.delete('/api/sessions/'+sid,headers=self.headers()).status_code,404)
        self.assertEqual(len(self.client.get('/api/sessions').json()['sessions']),1)
        own=self.client.get('/api/session').json()['session']['id']
        self.assertEqual(self.client.delete('/api/sessions/'+own,headers=self.headers()).status_code,200)
        self.assertEqual(self.client.get('/api/stats').status_code,401)

    def test_initial_password_must_change_and_file_removed(self):
        with database(self.home) as db:db.execute('UPDATE users SET must_change=1 WHERE account=?',('alice',))
        initial=self.home/'accounts/alice/login-initial.txt';initial.write_text('synthetic')
        r=self.login();self.assertTrue(r.json()['must_change'])
        self.assertEqual(self.client.get('/api/stats').status_code,403)
        self.assertEqual(self.client.get('/',follow_redirects=False).headers['location'],'/account')
        r=self.client.post('/api/password',headers=self.headers(),json={'current_password':PASSWORD,'new_password':NEW_PASSWORD})
        self.assertEqual(r.status_code,200);self.assertFalse(initial.exists())
        self.assertEqual(self.client.get('/api/stats').status_code,401)
        self.assertEqual(self.login(password=PASSWORD).status_code,401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code,200)

    def test_reset_disable_expiry_and_token_rotation_invalidate_sessions(self):
        self.login()
        set_password(self.home,'alice',NEW_PASSWORD)
        self.assertEqual(self.client.get('/api/stats').status_code,401)
        self.login(password=NEW_PASSWORD)
        with database(self.home) as db:db.execute('UPDATE users SET enabled=0 WHERE account=?',('alice',))
        self.assertEqual(self.client.get('/api/stats').status_code,401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code,401)
        with database(self.home) as db:db.execute('UPDATE users SET enabled=1 WHERE account=?',('alice',))
        self.login(password=NEW_PASSWORD)
        with database(self.home) as db:db.execute('UPDATE sessions SET expires_at=0')
        self.assertEqual(self.client.get('/api/stats').status_code,401)
        self.login(password=NEW_PASSWORD)
        (self.home/'accounts/alice/runtime/secrets/control-token').write_text(secrets.token_hex(32))
        self.assertEqual(self.client.get('/api/stats').status_code,401)

    def test_brute_force_throttle_persists(self):
        with database(self.home) as db:db.execute('INSERT INTO attempts VALUES(?,?,?)',('user:'+digest('alice'),8,time.time()+900))
        r=self.login();self.assertEqual(r.status_code,429)
        self.assertEqual(r.headers['retry-after'],'900')
        self.assertEqual(self.login('bob').status_code,200)

    def test_internal_docs_paths_and_pairing_cannot_be_proxied(self):
        self.login()
        for path in ('/internal/poll','/docs','/openapi.json','/config','/assets/../auth.db'):
            self.assertEqual(self.client.get(path).status_code,404)
        self.assertEqual(self.client.post('/api/session',json={},headers=self.headers()).status_code,405)
        self.assertEqual(self.client.get('/api/redirect').status_code,502)

    def test_files_and_no_cache_response(self):
        self.login('bob');r=self.client.get('/api/file')
        self.assertEqual(r.content,b'private-file-bob');self.assertIn('attachment',r.headers['content-disposition'])
        self.assertEqual(r.headers['cache-control'],'no-store')
        self.assertEqual(self.client.get('/').headers['cache-control'],'no-store')

    def test_registration_is_local_admin_only_and_strict_identity(self):
        self.assertEqual(self.client.post('/api/register',json={},headers={'Origin':self.config['origin']}).status_code,401)
        for name in ('../alice','ALICE','http://evil.invalid',''):
            with self.assertRaises(ValueError):account_backend(self.home,name)
        accfile=self.home/'accounts/alice/account.json';acc=json.loads(accfile.read_text());acc['home']=str(self.home/'accounts/bob');accfile.write_text(json.dumps(acc))
        self.assertEqual(self.login().status_code,503)

    def test_local_preview_cannot_be_exposed_as_public_or_trust_headers(self):
        self.assertEqual(self.client.get('/',headers={'Host':'evil.invalid'}).status_code,421)
        self.assertEqual(self.client.get('/',headers={'X-Forwarded-For':'127.0.0.1'}).status_code,403)
        with self.assertRaises(ValueError):create_gateway({**self.config,'origin':'http://public.invalid'})
        with self.assertRaises(ValueError):create_gateway({**self.config,'origin':'https://public.invalid','local_only':False})

    def cf_token(self,**changes):
        claims={'iss':'https://fixture.cloudflareaccess.com','aud':'a'*64,'iat':int(time.time()),'exp':int(time.time())+300,'sub':'synthetic-subject'}
        claims.update(changes)
        return jwt.encode(claims,self.private,algorithm='RS256',headers={'kid':'synthetic-key'})

    def test_cloudflare_signature_issuer_audience_expiry_and_dual_login(self):
        cfg={**self.config,'origin':'https://carme.example.test','local_only':False,'access_team':'fixture','access_audience':'a'*64}
        with TestClient(create_gateway(cfg,transport=self.transport),base_url=cfg['origin']) as c:
            self.assertEqual(c.get('/').status_code,401)
            for token in ('forged',self.cf_token(iss='https://evil.invalid'),self.cf_token(aud='wrong'),self.cf_token(exp=0),self.cf_token(sub='')):
                self.assertEqual(c.get('/',headers={'Cf-Access-Jwt-Assertion':token}).status_code,401)
            c.headers['Cf-Access-Jwt-Assertion']=self.cf_token()
            self.assertEqual(c.get('/').status_code,200)
            self.assertEqual(c.get('/',headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate'}).status_code,200)
            self.assertEqual(c.get('/api/stats',headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'cors'}).status_code,403)
            self.assertEqual(c.get('/api/stats').status_code,401)
            login=self.login(client=c);self.assertEqual(login.status_code,200)
            self.assertIn('Secure',login.headers['set-cookie'])
            self.assertEqual(c.get('/api/stats').status_code,200)
            c.headers['Cf-Access-Jwt-Assertion']=self.cf_token(sub='another-person')
            self.assertEqual(c.get('/api/stats').status_code,401)
            c.headers.pop('Cf-Access-Jwt-Assertion')
            self.assertEqual(c.get('/api/stats').status_code,401)

    def test_auth_store_private_no_plaintext_credentials(self):
        self.login()
        self.assertEqual((self.home/'gateway').stat().st_mode&0o777,0o700)
        self.assertEqual((self.home/'gateway/auth.db').stat().st_mode&0o777,0o600)
        with database(self.home) as db:
            dump='\n'.join(db.iterdump())
        for value in (PASSWORD,*self.tokens.values(),self.client.cookies[COOKIE]):self.assertNotIn(value,dump)

    def test_event_stream_survives_two_minutes_and_stops_on_revocation(self):
        self.login()
        clock = [time.time()]
        home = self.home
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                for _ in range(4):
                    clock[0] += 61
                    yield b': heartbeat\n\n'
            async def aclose(self): pass
        self.client.app.state.client._transport = httpx.MockTransport(lambda request:
            httpx.Response(200, headers={'Content-Type':'text/event-stream'}, stream=Stream()))
        with patch('carme.gateway.time', SimpleNamespace(time=lambda: clock[0])):
            response = self.client.get('/api/events')
        self.assertEqual(response.content.count(b'heartbeat'), 4)
        self.assertEqual(response.headers['x-accel-buffering'], 'no')

        class Revoked(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b': before\n\n'
                with database(home) as db: db.execute('DELETE FROM sessions')
                yield b': race\n\n'
                yield b': must-not-arrive\n\n'
            async def aclose(self): pass
        self.client.app.state.client._transport = httpx.MockTransport(lambda request:
            httpx.Response(200, headers={'Content-Type':'text/event-stream'}, stream=Revoked()))
        self.assertNotIn(b'must-not-arrive', self.client.get('/api/events').content)


if __name__=='__main__':unittest.main(verbosity=2)
