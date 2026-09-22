"""Optional paired native runner. Local grants plus Control approval; no shell or Pi."""
from __future__ import annotations
import argparse, asyncio, contextlib, fcntl, hashlib, hmac, json, math, os, re, secrets, signal, stat, sys, tempfile, threading, time
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from .projects import digest, save
from .execution import encoded, signature
from .tools.base import Tool

KEYS={'escape','tab','left','right','up','down','pageup','pagedown'}

def validate_action(action):
    if not isinstance(action,dict) or action.get('op') not in {'click','key'}:raise ValueError('native_operation_denied: no shell, AppleScript, typing or clipboard')
    expected={'op','bundle_id','window_id','x','y'} if action['op']=='click' else {'op','bundle_id','window_id','key'}
    if set(action)!=expected or not isinstance(action['bundle_id'],str) or not action['bundle_id'] or type(action['window_id']) is not int or action['window_id']<=0:raise ValueError('native_action_fields_denied')
    if action['op']=='key' and action['key'] not in KEYS:raise ValueError('native_key_denied')
    if action['op']=='click' and any(type(action[k]) not in {int,float} or not math.isfinite(action[k]) for k in ('x','y')):raise ValueError('native_coordinate_denied')
    return action

class MacActionTool(Tool):
    name='mac_action'
    description='在已配对且本地授权的指定 Mac 窗口执行一次点击或导航键；每次需要人工审批。不能运行 Shell、输入文本或使用剪贴板。'
    parameters={'type':'object','properties':{'action':{'type':'object'}},'required':['action'],'additionalProperties':False}
    def __init__(self,execution):self.execution=execution
    async def run(self,ctx,action):
        validate_action(action)
        if ctx.extras.get('policy',{}).get('target')!='macos':raise ValueError('native_target_required')
        outcome=await ctx.request_approval(kind='mac_action',summary='在真实 Mac 的指定窗口执行单次操作',detail={'action':action,'action_sha256':digest(action)})
        if not outcome.approved:return '操作未批准'
        return json.dumps(await self.execution.native(ctx.task_id,action),ensure_ascii=False)

class DesktopLease:
    """One physical desktop mutex for all account installations of this OS user."""
    def __init__(self,state_dir,lock_path=None):
        self.home=Path(state_dir)
        if self.home.resolve()!=self.home:raise ValueError('runner_state_symlink_denied')
        self.home.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock_path=Path(lock_path or (Path(tempfile.gettempdir())/f'carme-desktop-{os.getuid()}.lock'))
        self.fd=None;self.run='';self.generation='';self.guard=threading.RLock();self.revoked=True
    def release(self):
        with self.guard:
            if self.fd is not None:fcntl.flock(self.fd,fcntl.LOCK_UN);os.close(self.fd)
            self.fd=None;self.run='';self.revoked=True
    def takeover(self,reason='human_takeover'):
        # The listener must revoke before waiting for an in-flight window check's
        # mutex; otherwise that check could post input after human input arrived.
        self.revoked=True
        (self.home/'grant.json').unlink(missing_ok=True)
        with self.guard:
            self.release()
            save(self.home/'status.json',{'state':'stopped','reason':reason,'at':time.time(),'requires_new_grant':True})
    def grant(self):
        try:
            p=self.home/'grant.json'
            if p.is_symlink():return {}
            grant=json.loads(p.read_text())
            return grant if time.time()<grant['expires']<=time.time()+601 else {}
        except (OSError,ValueError,KeyError):return {}
    def acquire(self,job,window):
        with self.guard:
            grant=self.grant();action=validate_action(job['action'])
            if (not grant or (grant['bot_id']!='*' and grant['bot_id']!=job['bot_id']) or action['bundle_id']!=grant['bundle_id'] or action['window_id']!=grant['window_id']
                    or time.time()>=job['deadline'] or digest(action)!=job['action_sha256']):raise ValueError('local_native_grant_required')
            if self.run and (self.run!=job['run_id'] or self.generation!=grant['generation']):raise ValueError('desktop_lease_busy_requires_new_grant')
            if window.get('bundle_id')!=action['bundle_id'] or window.get('window_id')!=action['window_id'] or not window.get('frontmost'):raise ValueError('native_target_window_changed')
            if action['op']=='click':
                x,y,w,h=window['bounds']
                if not (x<=action['x']<x+w and y<=action['y']<y+h):raise ValueError('native_click_outside_window')
                for x,y,w,h in window.get('blocked_regions',[]):
                    if x<=action['x']<x+w and y<=action['y']<y+h:raise ValueError('native_click_occluded')
            if self.fd is None:
                fd=os.open(self.lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
                info=os.fstat(fd)
                if info.st_uid!=os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:os.close(fd);raise ValueError('desktop_lock_owner_denied')
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:os.close(fd);raise ValueError('desktop_busy_other_bot_or_account') from None
                self.fd=fd;self.run=job['run_id'];self.generation=grant['generation'];self.revoked=False
            save(self.home/'status.json',{'state':'authorized','run_id':self.run,'bot_id':job['bot_id'],'expires':grant['expires'],'at':time.time()})
    def perform(self,job,window_getter,execute):
        with self.guard:
            self.acquire(job,window_getter())
            if self.revoked or not self.grant():raise ValueError('desktop_lease_revoked')
            # Re-check immediately before the single fixed action; no queued macro or batch.
            self.acquire(job,window_getter())
            if self.revoked or not self.grant():raise ValueError('desktop_lease_revoked')
            save(self.home/'status.json',{'state':'executing','run_id':self.run,'action_sha256':job['action_sha256'],'at':time.time()})
            execute(job['action'])
            return {'ok':True,'action_sha256':job['action_sha256'],'verification':'input_posted_not_outcome_verified'}

def permissions():
    if sys.platform!='darwin':return {'ready':False,'reason':'macos_required'}
    try:
        import Quartz as q
        import HIServices as ax
        result={'accessibility':bool(q.CGPreflightPostEventAccess() and ax.AXIsProcessTrusted()),'screen_recording':bool(q.CGPreflightScreenCaptureAccess()),
                'input_monitoring':bool(q.CGPreflightListenEventAccess())}
        return {**result,'ready':all(result.values())}
    except (ImportError,AttributeError):return {'ready':False,'reason':'dedicated_pyobjc_dependency_required'}
def request_permissions():
    """触发 macOS 的三个 TCC 授权弹窗（辅助功能/屏幕录制/输入监控）；是否授予仍由人在系统弹窗决定。

    权限状态可能在授予后需重启进程才会刷新；本函数只负责发起请求，不绕过任何检查。
    """
    if sys.platform!='darwin':return {'ready':False,'reason':'macos_required'}
    out={}
    try:
        import Quartz as q
        import HIServices as ax
        trusted=bool(q.CGPreflightPostEventAccess() and ax.AXIsProcessTrusted())
        if not trusted:
            ax.AXIsProcessTrustedWithOptions({ax.kAXTrustedCheckOptionPrompt:True})
        out['accessibility']=trusted
        if not q.CGPreflightScreenCaptureAccess():q.CGRequestScreenCaptureAccess()
        out['screen_recording']=bool(q.CGPreflightScreenCaptureAccess())
        if not q.CGPreflightListenEventAccess():q.CGRequestListenEventAccess()
        out['input_monitoring']=bool(q.CGPreflightListenEventAccess())
        out['ready']=all((out['accessibility'],out['screen_recording'],out['input_monitoring']))
    except (ImportError,AttributeError):return {'ready':False,'reason':'dedicated_pyobjc_dependency_required'}
    return out


def native_window():
    import Quartz as q
    import HIServices as ax
    from CoreFoundation import CFEqual
    from AppKit import NSWorkspace
    if threading.current_thread() is not threading.main_thread():raise RuntimeError('native_window_main_thread_required')
    workspace=NSWorkspace.sharedWorkspace()
    # NSWorkspace/NSRunningApplication state refreshes on the Cocoa main loop,
    # which asyncio alone does not run. Never keep a cached foreground identity.
    q.CFRunLoopRunInMode(q.kCFRunLoopDefaultMode,.01,False)
    front=workspace.frontmostApplication()
    if front is None:return {}
    app=ax.AXUIElementCreateApplication(front.processIdentifier())
    def value(element,key):
        error,result=ax.AXUIElementCopyAttributeValue(element,key,None)
        return result if error==0 else None
    focused=value(app,ax.kAXFocusedWindowAttribute)
    element=value(app,ax.kAXFocusedUIElementAttribute)
    if focused is None or element is None:return {}
    # A modal sheet/menu is not the approved page just because its owner is Chrome.
    top=value(element,ax.kAXTopLevelUIElementAttribute)
    if top is None or not CFEqual(top,focused):return {}
    position=value(focused,ax.kAXPositionAttribute);size=value(focused,ax.kAXSizeAttribute)
    if position is None or size is None:return {}
    ok,position=ax.AXValueGetValue(position,ax.kAXValueCGPointType,None)
    sized,size=ax.AXValueGetValue(size,ax.kAXValueCGSizeType,None)
    if not ok or not sized:return {}
    expected=[position.x,position.y,size.width,size.height]
    windows=q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionOnScreenOnly|q.kCGWindowListExcludeDesktopElements,q.kCGNullWindowID)
    matches=[];above=[]
    for w in windows:
        if w.get(q.kCGWindowAlpha,1)<=0:continue
        bounds=[w[q.kCGWindowBounds][k] for k in ('X','Y','Width','Height')]
        if (w.get(q.kCGWindowLayer)==0 and w.get(q.kCGWindowOwnerPID)==front.processIdentifier()
                and all(abs(a-b)<.5 for a,b in zip(bounds,expected))):
            matches.append({'bundle_id':str(front.bundleIdentifier()),'window_id':int(w[q.kCGWindowNumber]),
                            'frontmost':True,'bounds':bounds,'blocked_regions':list(above)})
        above.append(bounds)
    # CoreGraphics may put a separate traffic-light/control window first. Require
    # a unique match to AX focus, and still deny clicks covered by any upper window.
    return matches[0] if len(matches)==1 else {}

def native_execute(action):
    import Quartz as q
    from .desktop import DesktopController
    if action['op']=='click':
        # No drag/held buttons and no arbitrary text; one down/up pair.
        DesktopController._post_clicks(q,action['x'],action['y'],'left',1)
    else:
        vkey=DesktopController._VK[action['key']]
        for down in (True,False):q.CGEventPost(q.kCGHIDEventTap,q.CGEventCreateKeyboardEvent(None,vkey,down))

def listen_human(lease,ready,stopped):
    import Quartz as q
    kinds=[q.kCGEventKeyDown,q.kCGEventFlagsChanged,q.kCGEventLeftMouseDown,q.kCGEventRightMouseDown,
           q.kCGEventOtherMouseDown,q.kCGEventMouseMoved,q.kCGEventScrollWheel]
    def event(proxy,kind,value,info):
        if kind in (q.kCGEventTapDisabledByTimeout,q.kCGEventTapDisabledByUserInput):
            lease.takeover('input_monitor_unavailable');stopped.set()
        elif (lease.run or lease.grant()) and q.CGEventGetIntegerValueField(value,q.kCGEventSourceUnixProcessID)!=os.getpid():
            # Do not record key values, coordinates, clipboard or window content.
            lease.takeover()
        return value
    tap=q.CGEventTapCreate(q.kCGSessionEventTap,q.kCGHeadInsertEventTap,q.kCGEventTapOptionListenOnly,sum(1<<v for v in kinds),event,None)
    if tap is None:stopped.set();ready.set();return
    source=q.CFMachPortCreateRunLoopSource(None,tap,0);loop=q.CFRunLoopGetCurrent()
    q.CFRunLoopAddSource(loop,source,q.kCFRunLoopCommonModes);q.CGEventTapEnable(tap,True);ready.set()
    try:
        while not stopped.is_set():q.CFRunLoopRunInMode(q.kCFRunLoopDefaultMode,.05,False)
    finally:q.CGEventTapEnable(tap,False);q.CFRunLoopRemoveSource(loop,source,q.kCFRunLoopCommonModes);lease.takeover('runner_stopped')

async def serve(config):
    state=permissions()
    if not state['ready']:raise RuntimeError('需在系统设置明确授予辅助功能、屏幕录制及输入监控权限；不会请求或绕过权限：'+json.dumps(state))
    url=urlsplit(config['control_url'])
    if url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password or url.path not in ('','/') or url.query or url.fragment:raise ValueError('local_control_required')
    key=Path(config['key_file']).read_bytes().strip()
    if len(key)<32:raise ValueError('runner_pairing_required')
    lease=DesktopLease(config['state_dir']);lease.takeover('runner_started_new_grant_required')
    ready=threading.Event();stopped=threading.Event()
    listener=threading.Thread(target=listen_human,args=(lease,ready,stopped),daemon=True);listener.start()
    if not await asyncio.to_thread(ready.wait,3) or stopped.is_set():raise RuntimeError('input_monitor_unavailable')
    loop=asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stopped.set)
    print('Mac Runner 已连接。默认未授权。保持此终端可见；Ctrl+C 或 stop 立即撤销；人工输入会接管。',flush=True)
    async with httpx.AsyncClient(base_url=config['control_url'],trust_env=False,timeout=3) as client:
        async def call(data):
            body=encoded(data);nonce=f'{time.time():.3f}:'+secrets.token_hex(16)
            r=await client.post('/internal/mac',content=body,headers={'X-Carme-Nonce':nonce,'X-Carme-Signature':signature(key,nonce,body)})
            r.raise_for_status()
            if len(r.content)>16_000_000 or not hmac.compare_digest(signature(key,nonce,r.content),r.headers.get('X-Carme-Signature','')):raise ValueError('control_signature_invalid')
            return r.json()
        screen_size=(0,0)
        try:
            desktop=DesktopController({'enabled':True,'control_enabled':True})
            probe=await asyncio.to_thread(desktop.status)
            screen_size=(int(probe.get('screen_width') or 0),int(probe.get('screen_height') or 0))
            if not screen_size[0]:raise RuntimeError('screen_size_unavailable')
            print('人工桌面通道就绪：屏幕 %dx%d。'%(screen_size[0],screen_size[1]),flush=True)
        except Exception as exc:
            desktop=None
            print('人工桌面通道不可用（查看/控制不可用，任务内动作不受影响）：'+str(exc)[:200],flush=True)
        import base64
        try:
            while not stopped.is_set():
                if (lease.home/'stop').exists():stopped.set();break
                grant=lease.grant()
                if not grant or (lease.generation and lease.generation!=grant['generation']):lease.release()
                try:
                    answer=await call({'op':'claim','runner_id':config['runner_id'],'authorized':bool(lease.grant())})
                    if answer.get('screen') and desktop is not None:
                        try:
                            frame=await asyncio.to_thread(desktop.screenshot)
                            await call({'op':'screen','runner_id':config['runner_id'],
                                'frame':base64.b64encode(frame).decode('ascii'),'mtime':time.time(),
                                'width':screen_size[0],'height':screen_size[1]})
                        except Exception:pass
                    batch=answer.get('control') or []
                    if batch and desktop is not None and lease.grant():
                        results=[]
                        for item in batch[:6]:
                            body=item.get('body') or {}
                            try:
                                if item.get('kind')=='mouse':r=await asyncio.to_thread(desktop.mouse,**body)
                                elif item.get('kind')=='keyboard':r=await asyncio.to_thread(desktop.keyboard,keys=str(body.get('keys','')))
                                else:r={'error':'unknown_control_kind'}
                                results.append({'id':item.get('id'),'ok':True,'result':r})
                            except Exception as exc:
                                results.append({'id':item.get('id'),'ok':False,'error':str(exc)[:200]})
                        if results:await call({'op':'control_results','runner_id':config['runner_id'],'results':results})
                    if answer.get('job'):
                        job=answer['job'];print('执行已审批动作：'+job['job_id'],flush=True)
                        try:
                            permitted=await call({'op':'authorize','runner_id':config['runner_id'],'job_id':job['job_id'],'lease':answer['lease']})
                            if not permitted.get('accepted'):raise ValueError('task_revoked')
                            result=lease.perform(job,native_window,native_execute)
                        except Exception:lease.takeover('native_action_denied');result={'error':'native_action_denied_requires_new_grant'}
                        await call({'op':'finish','runner_id':config['runner_id'],'job_id':job['job_id'],'lease':answer['lease'],'result':result})
                except Exception:
                    lease.takeover('control_disconnected_no_replay')
                await asyncio.sleep(.2)
        finally:stopped.set();lease.takeover('runner_stopped');listener.join(timeout=2)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('op',choices=['doctor','request','serve','grant','stop','status']);p.add_argument('--config',type=Path,required=True)
    p.add_argument('--allow-gui',action='store_true');p.add_argument('--bot');p.add_argument('--bundle');p.add_argument('--window',type=int);p.add_argument('--seconds',type=int,default=60)
    a=p.parse_args();c=json.loads(a.config.read_text());home=Path(c['state_dir'])
    if a.op=='doctor':print(json.dumps(permissions()));return
    if a.op=='request':print(json.dumps(request_permissions()));return
    if a.op=='serve':
        if not a.allow_gui:p.error('serve 需要显式 --allow-gui；请先阅读本机权限与接管说明')
        (home/'stop').unlink(missing_ok=True);asyncio.run(serve(c))
    elif a.op=='grant':
        bot=(a.bot or '*').strip()
        if not a.allow_gui or not bot or not a.bundle or not a.window or not 1<=a.seconds<=600:p.error('grant 需要 --allow-gui --bundle --window 和 1–600 秒期限；--bot 省略或 * 表示全部 Bot')
        if bot!='*' and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',bot):p.error('bot 格式不正确')
        save(home/'grant.json',{'bot_id':bot,'bundle_id':a.bundle,'window_id':a.window,'expires':time.time()+a.seconds,'generation':secrets.token_hex(16)})
        print('已授权'+('全部 Bot' if bot=='*' else ' Bot '+bot)+'/指定窗口；每个动作仍需 Control 审批；人工操作或停止将撤销。')
    elif a.op=='stop':
        save(home/'stop',{'stop':True});(home/'grant.json').unlink(missing_ok=True);print('已请求停止并撤销本地授权。')
    else:
        state=json.loads((home/'status.json').read_text()) if (home/'status.json').exists() else {'state':'not_connected'}
        if time.time()-state.get('at',0)>5:state['state']='stale_or_stopped'
        print(json.dumps(state,ensure_ascii=False))

if __name__=='__main__':main()
