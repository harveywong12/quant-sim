import importlib.util
from pathlib import Path
import socket
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as OriginalTestClient

def TestClient(app, *, base_url, client):
    # The installed Starlette lacks a client= constructor argument. Inject
    # synthetic ASGI identity in tests; production uses actual TCP scope.
    async def synthetic(scope, receive, send):
        scope = {**scope, 'client': client}
        await app(scope, receive, send)
    return OriginalTestClient(synthetic, base_url=base_url)
spec=importlib.util.spec_from_file_location('lan_access',Path(__file__).resolve().parents[1]/'scripts/lan_api_entry.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
NAS='192.0.2.10';MAC='192.0.2.20'

def application():
    from fastapi.responses import HTMLResponse
    app=FastAPI()
    @app.get('/healthz')
    def health():return {'ok':True}
    @app.get('/')
    def root():return HTMLResponse('<html><head></head><body>synthetic</body></html>')
    @app.post('/write')
    def write():raise AssertionError('LAN must never execute mutation')
    return m.LANReadGate(app,NAS,MAC,8611)

@pytest.mark.parametrize('ip,status',[(MAC,200),(NAS,200),('127.0.0.1',200),('192.0.2.99',403),('8.8.8.8',403)])
def test_only_reviewed_socket_clients(ip,status):
    c=TestClient(application(),base_url=f'http://{NAS}:8611',client=(ip,100))
    assert c.get('/healthz').status_code==status

@pytest.mark.parametrize('path,method',[('/write','POST'),('/write','DELETE'),('/api/ai-chat','GET')])
def test_lan_cannot_write_or_call_models(path,method):
    c=TestClient(application(),base_url=f'http://{NAS}:8611',client=(MAC,100))
    assert c.request(method,path).status_code==403

def test_forwarded_headers_cannot_grant_access_and_bad_host_rejected():
    c=TestClient(application(),base_url=f'http://{NAS}:8611',client=('192.0.2.99',100))
    assert c.get('/healthz',headers={'X-Forwarded-For':MAC}).status_code==403
    c=TestClient(application(),base_url='http://evil.example',client=(MAC,100))
    assert c.get('/healthz').status_code==403

def test_page_clears_browser_token_before_legacy_js_and_is_not_cached():
    c=TestClient(application(),base_url=f'http://{NAS}:8611',client=(MAC,100))
    r=c.get('/')
    assert r.status_code==200 and "sessionStorage.removeItem('qsim_tok')" in r.text
    assert r.headers['content-length']==str(len(r.content))
    assert r.headers['cache-control']=='no-store' and r.headers['x-frame-options']=='DENY'
    assert c.get('/healthz').json()=={'ok':True}

def test_cross_site_reads_denied_but_top_level_url_allowed():
    c=TestClient(application(),base_url=f'http://{NAS}:8611',client=(MAC,100))
    assert c.get('/healthz',headers={'sec-fetch-site':'cross-site','sec-fetch-mode':'cors'}).status_code==403
    assert c.get('/',headers={'sec-fetch-site':'cross-site','sec-fetch-mode':'navigate'}).status_code==200

def test_two_listener_sockets_one_process_and_no_wildcard(monkeypatch):
    # Synthetic bind calls verify exact addresses without opening network listeners.
    addresses=[]
    class Socket:
        def setsockopt(self,*a): pass
        def bind(self,a): addresses.append(a)
        def listen(self,n): assert n==128
        def setblocking(self,b): assert b is False
        def close(self): pass
    monkeypatch.setattr(m.socket,'socket',lambda *a:Socket())
    listeners=m.bind_listeners(NAS,18861)
    assert len(listeners)==2 and addresses==[('127.0.0.1',18861),(NAS,18861)]

@pytest.mark.parametrize('address',['0.0.0.0','8.8.8.8','127.0.0.1'])
def test_listener_rejects_public_wildcard_and_duplicate(address):
    with pytest.raises(ValueError):m.bind_listeners(address,18861)

def test_entry_uses_reviewed_working_directory_source_not_installed_copy(tmp_path,monkeypatch):
    import sys,types
    runtime=tmp_path/'reviewed-runtime';installed=tmp_path/'installed-other-version'
    for folder,marker in [(runtime,'reviewed'),(installed,'wrong-version')]:
        appdir=folder/'qsim/services/api';appdir.mkdir(parents=True)
        for part in [folder/'qsim',folder/'qsim/services',appdir]:
            (part/'__init__.py').write_text('')
        (appdir/'app.py').write_text(f'def create_app(): return {marker!r}\n')
    monkeypatch.chdir(runtime)
    monkeypatch.setattr(sys,'path',[str(installed)]+list(sys.path))
    for key in ['qsim','qsim.services','qsim.services.api','qsim.services.api.app']:
        monkeypatch.delitem(sys.modules,key,raising=False)
    monkeypatch.setenv('QSIM_SCHEDULER_ENABLED','0');monkeypatch.setenv('QSIM_SCHEDULE_OWNER','systemd')
    monkeypatch.setattr(sys,'argv',['entry','--lan-ip',NAS,'--client-ip',MAC])
    monkeypatch.setattr(m,'bind_listeners',lambda *args:[])
    seen=[]
    class Server:
        def __init__(self,config):self.config=config
        def run(self,*,sockets):seen.append(self.config.app)
    fake=types.SimpleNamespace(Config=lambda app,**kw:types.SimpleNamespace(app=app),Server=Server)
    monkeypatch.setitem(sys.modules,'uvicorn',fake)
    try:
        m.main()
        assert seen[0].app=='reviewed'
    finally:
        for key in ['qsim','qsim.services','qsim.services.api','qsim.services.api.app']:
            sys.modules.pop(key,None)
