"""Run INSIDE the dedicated Pi image, network=none, /runtime tmpfs. Synthetic resources only."""
from pathlib import Path
import http.server,json,os,signal,subprocess,sys,tempfile,threading
sys.path.insert(0,'/app')
from carme.engines import PI_SECURITY_FLAGS,build_argv
from carme.security import child_env

root=Path(tempfile.mkdtemp(prefix='loader-',dir='/runtime'))
home=root/'home';cwd=home/'project/sub';cwd.mkdir(parents=True)
profile=home/'pi';profile.mkdir()
marker='PI_LOADER_FORBIDDEN_CANARY'
side_effect=root/'untrusted-extension-ran'
for folder in (home,home/'project',cwd,profile):
 for name in ('AGENTS.md','CLAUDE.md','SYSTEM.md','APPEND_SYSTEM.md'):(folder/name).write_text(marker)
 for sub in (folder/'.pi',folder):
  (sub/'skills/untrusted').mkdir(parents=True,exist_ok=True)
  (sub/'skills/untrusted/SKILL.md').write_text('---\nname: forbidden\ndescription: '+marker+'\n---\n'+marker)
  (sub/'prompts').mkdir(exist_ok=True);(sub/'prompts/untrusted.md').write_text(marker)
  (sub/'extensions').mkdir(exist_ok=True)
  (sub/'extensions/untrusted.js').write_text("import fs from 'node:fs';fs.writeFileSync("+json.dumps(str(side_effect))+",'executed');export default function(){}")
(profile/'settings.json').write_text(json.dumps({'defaultProvider':'personal','defaultModel':'personal-model',
    'extensions':[str(profile/'extensions/untrusted.js')],'skills':[str(profile/'skills/untrusted')]}))
(home/'bin').mkdir();fake=home/'bin/pi';fake.write_text('#!/bin/sh\ntouch '+str(root/'wrong-pi-ran')+'\nexit 99\n');fake.chmod(0o755)
os.environ['PATH']=str(home/'bin')+':'+os.environ.get('PATH','')
os.environ['NODE_OPTIONS']='--require /unapproved/personal.js'
requests=[]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(body)
  chunks=[{'id':'loader','object':'chat.completion.chunk','created':1,'model':'loader','choices':[{'index':0,'delta':{'role':'assistant','content':'LOADER_OK'},'finish_reason':None}]},
          {'id':'loader','object':'chat.completion.chunk','created':1,'model':'loader','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
  data=(''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode()
  self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
(profile/'models.json').write_text(json.dumps({'providers':{'carme':{'api':'openai-completions','apiKey':'synthetic-local-only',
 'baseUrl':f'http://127.0.0.1:{server.server_port}/v1','models':[{'id':'loader','name':'Loader fixture','reasoning':False,'input':['text'],
 'contextWindow':32000,'maxTokens':512,'cost':{'input':0,'output':0,'cacheRead':0,'cacheWrite':0}}]}}}))
env=child_env(home);env['PI_SKIP_VERSION_CHECK']='1';Path(env['TMPDIR']).mkdir()
argv=build_argv('pi','/opt/pi/node_modules/.bin/pi','carme/loader')
process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
try:
 out,err=process.communicate(b'Reply LOADER_OK',timeout=40)
 assert process.returncode==0,(process.returncode,err.decode()[:800])
 assert b'LOADER_OK' in out and requests
 print(json.dumps({'canary_locations':[(m['role'],str(m.get('content',''))[:300]) for r in requests for m in r['messages'] if marker in str(m.get('content',''))]}),flush=True)
 assert marker not in json.dumps(requests)
 assert all(not request.get('tools') for request in requests)
 assert not side_effect.exists() and not (root/'wrong-pi-ran').exists()
 assert set(PI_SECURITY_FLAGS)<=set(argv) and '--no-tools' in argv
 version=json.loads(Path('/opt/pi/node_modules/@earendil-works/pi-coding-agent/package.json').read_text())['version']
 assert version=='0.85.1'
 print(json.dumps({'status':'pass','version':version,'requests':len(requests),'resource_canaries_absent':True,
  'untrusted_extension_not_executed':True,'fixed_binary_despite_parent_path':True,'parent_node_options_ignored':True,
  'native_tools_empty':True,'flags':list(PI_SECURITY_FLAGS)}))
finally:
 if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait()
 server.shutdown();server.server_close()
