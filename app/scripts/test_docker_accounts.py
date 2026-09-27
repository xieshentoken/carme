"""Real, temporary Docker accounts. No production paths or model credentials."""
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright

spec=importlib.util.spec_from_file_location('launcher',Path(__file__).with_name('carme_docker.py'))
cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
home=Path(sys.argv[1]);output=Path(sys.argv[2])
tmp=tempfile.gettempdir()
assert home.resolve() == home and str(home).startswith(tuple(tmp+'/'+name for name in ('carme-m3-', 'carme-browser-', 'carme-m4-')))
setup=cli.load(home/'installation.json')
accounts={a['id']:a for a in cli.account_list(home)}
a,b=accounts['alice'],accounts['bob'];checks=[]
def check(name, condition):
    assert condition,name
    checks.append(name);print('PASS:',name,flush=True)

check('two healthy independent brokers',all(cli.api(x,'/api/health')['execution']['broker']=='ready' for x in (a,b)))
check('separate origin and storage',a['origin']!=b['origin'] and a['home']!=b['home'])
keys=[(Path(x['home'])/'runtime/secrets/control-token').read_text().strip() for x in (a,b)]
check('distinct credentials',keys[0]!=keys[1])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,args=['--no-proxy-server'])
    ctx=browser.new_context();pa=ctx.new_page();pb=ctx.new_page()
    for page,acc in ((pa,a),(pb,b)):
        response=page.goto(acc['origin'],wait_until='domcontentloaded')
        check(acc['id']+' origin resolves in real Chromium',response.status==200)
    def fetch(page,path,method='GET',body=None,key='',csrf=''):
        return page.evaluate('''async x => {const h={'Content-Type':'application/json'};
          if(x.key)h.Authorization='Bearer '+x.key;if(x.csrf)h['X-Carme-CSRF']=x.csrf;
          const r=await fetch(x.path,{method:x.method,headers:h,body:x.body===null?undefined:JSON.stringify(x.body)});
          const t=await r.text();let data;try{data=JSON.parse(t)}catch{data={}}return {status:r.status,data}}''',
          dict(path=path,method=method,body=body,key=key,csrf=csrf))
    la=fetch(pa,'/api/session','POST',key=keys[0]);check('alice browser login',la['status']==200)
    check('alice login does not authenticate bob',fetch(pb,'/api/session')['status']==401)
    check('alice credential rejected by bob',fetch(pb,'/api/session','POST',key=keys[0])['status']==401)
    lb=fetch(pb,'/api/session','POST',key=keys[1]);check('bob browser login',lb['status']==200)
    cookies=ctx.cookies();check('host-only cookies',len(cookies)==2 and {c['domain'] for c in cookies}=={urlsplit(x['origin']).hostname for x in (a,b)} and all(c['httpOnly'] and c['sameSite']=='Strict' for c in cookies))
    ca=fetch(pa,'/api/conversations','POST',{'agent_ids':['assistant'],'title':'alice synthetic private'},csrf=la['data']['csrf'])
    check('alice creates own data',ca['status']==201);cid=ca['data']['conversation']['id']
    check('bob cannot read alice conversation id',fetch(pb,'/api/conversations/'+cid)['status']==404)
    check('bob list excludes alice data',cid not in [r['id'] for r in fetch(pb,'/api/conversations')['data']['conversations']])
    check('csrf remains required',fetch(pa,'/api/conversations','POST',{'agent_ids':['assistant']})['status']==403)
    import urllib.request,urllib.error
    def raw(host,cookie=''):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(urllib.request.Request(f'http://127.0.0.1:{b["port"]}/api/session',headers={'Host':host,'Cookie':cookie}),timeout=3) as r:return r.status
        except urllib.error.HTTPError as e:return e.code
    ac=next(c['value'] for c in cookies if c['domain']==urlsplit(a['origin']).hostname)
    check('stolen alice cookie rejected by bob',raw(urlsplit(b['origin']).netloc,'carme_session='+ac)==401)
    check('shared IP browser entry denied',raw(f'127.0.0.1:{b["port"]}')==421)
    browser.close()
for acc in (a,b):
    item=cli.inspect(setup,acc['control_name'])
    check(acc['id']+' mounts only own data',all(m['Source'].startswith(acc['home']+'/') for m in item['Mounts'] if m['Type']=='bind'))
    check(acc['id']+' has no Docker socket',not any('docker.sock' in m['Source'] for m in item['Mounts']))
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=8) as pool:
    results=list(pool.map(lambda item:(item[0]['id'],cli.api(item[0],'/api/conversations','POST',{'agent_ids':['assistant'],'title':'synthetic WAL '+str(item[1])})['conversation']['id']),[(acc,n) for n in range(30) for acc in (a,b)]))
for acc in (a,b):
    own={cid for owner,cid in results if owner==acc['id']};other={cid for owner,cid in results if owner!=acc['id']}
    visible={c['id'] for c in cli.api(acc,'/api/conversations')['conversations']}
    check(acc['id']+' concurrent WAL writes preserve scope',own<=visible and not visible&other)
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from carme.migrate import backup
    target=home/(acc['id']+'-wal-backup-'+str(time.time_ns())+'.db')
    manifest=backup(Path(acc['home'])/'runtime/control/carme.db',target)
    check(acc['id']+' Docker Desktop bind WAL backup integrity',manifest['conversations']['rows']>=len(own))
cli.stop(setup,a)
check('stopping alice leaves bob healthy',cli.api(b,'/api/health')['execution']['broker']=='ready')
release=cli.load(Path(a['home'])/'running-release.json')
cli.start(setup,a,release,cli.broker_python(setup))
check('restart preserves own data',cli.api(a,'/api/conversations/'+cid)['conversation']['id']==cid)
cli.stop(setup,a);cli.stop(setup,a);cli.stop(setup,b)
check('idempotent per-account stop',all(cli.status(setup,x)['control']=='stopped' for x in (a,b)))
output.write_text(json.dumps({'status':'pass','checks':checks,'count':len(checks),'synthetic_accounts':True},indent=2))
