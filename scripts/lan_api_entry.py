"""One existing API process, two exact listeners and a read-only LAN gate."""
from __future__ import annotations
import argparse
import ipaddress
import os
import socket
import sys

LOOPBACK = '127.0.0.1'
MAX_HTML = 8 * 1024 * 1024
LAN_NOTICE = b'''<script>(function(){try{sessionStorage.removeItem('qsim_tok');}catch(e){}document.addEventListener('DOMContentLoaded',function(){document.querySelectorAll('[onclick*="aiSend"],[onclick*="aiReSummary"],#ai-input,#ai-resummary-btn').forEach(function(e){e.disabled=true;e.title='\xe5\x86\x85\xe7\xbd\x91\xe5\x8f\xaa\xe8\xaf\xbb\xe6\x9f\xa5\xe7\x9c\x8b\xef\xbc\x8c\xe6\xad\xa4\xe5\x85\xa5\xe5\x8f\xa3\xe4\xb8\x8d\xe6\x89\xa7\xe8\xa1\x8c\xe4\xbb\xbb\xe5\x8a\xa1';});});})();</script>'''

class LANReadGate:
    def __init__(self, app, lan_ip, client_ip, port):
        self.app = app
        self.allowed = {str(ipaddress.IPv4Address(x)) for x in (LOOPBACK, lan_ip, client_ip)}
        self.hosts = {f'{ip}:{port}'.encode() for ip in self.allowed | {'localhost'}}
        self.hosts.add(f'{lan_ip}:{port}'.encode())

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        client = (scope.get('client') or ('',))[0]
        headers = dict(scope.get('headers') or [])
        if client not in self.allowed or headers.get(b'host', b'').lower() not in self.hosts:
            return await self.deny(scope, send)
        lan = client != LOOPBACK
        if lan and (scope['type'] == 'websocket' or scope.get('method') not in ('GET', 'HEAD')
                    or scope.get('path', '').startswith('/api/ai')):
            return await self.deny(scope, send)
        # Prevent another site from fetching private read endpoints. Ordinary
        # top-level navigation to the reviewed IP URL remains usable.
        if lan and headers.get(b'sec-fetch-site') == b'cross-site' and headers.get(b'sec-fetch-mode') != b'navigate':
            return await self.deny(scope, send)
        if not (lan and scope.get('path') == '/' and scope.get('method') == 'GET'):
            return await self.app(scope, receive, send)
        start, parts, size = None, [], 0
        async def capture(message):
            nonlocal start, size
            if message['type'] == 'http.response.start':
                start = dict(message)
            elif message['type'] == 'http.response.body':
                content = message.get('body', b''); size += len(content)
                if size > MAX_HTML:
                    raise ValueError('LAN page exceeds bounded view limit')
                parts.append(content)
        await self.app(scope, receive, capture)
        content = b''.join(parts)
        if start is None:
            raise ValueError('missing HTTP response')
        mime = dict(start['headers']).get(b'content-type', b'')
        if b'text/html' in mime and start['status'] == 200:
            # No business field is parsed, persisted or logged. Clear the old
            # browser key before legacy page JS; mutations/AI are denied above.
            if b'<head>' not in content:
                return await self.deny(scope, send)
            content = content.replace(b'<head>', b'<head>'+LAN_NOTICE, 1)
        start['headers'] = [(k,v) for k,v in start['headers'] if k.lower() not in
                            (b'content-length', b'cache-control', b'content-security-policy', b'x-frame-options')]
        start['headers'] += [(b'content-length', str(len(content)).encode()),
                             (b'cache-control', b'no-store'), (b'x-frame-options', b'DENY'),
                             (b'content-security-policy', b"frame-ancestors 'none'"),
                             (b'referrer-policy', b'no-referrer')]
        await send(start); await send({'type':'http.response.body', 'body':content})

    @staticmethod
    async def deny(scope, send):
        if scope['type'] == 'websocket':
            return await send({'type':'websocket.close','code':4403})
        body=b'{"detail":"LAN access is read-only and restricted to the reviewed computer"}'
        await send({'type':'http.response.start','status':403,'headers':[
            (b'content-type',b'application/json'),(b'content-length',str(len(body)).encode()),(b'cache-control',b'no-store')]})
        await send({'type':'http.response.body','body':body})

def bind_listeners(lan_ip, port):
    if not ipaddress.IPv4Address(lan_ip).is_private or lan_ip in (LOOPBACK, '0.0.0.0'):
        raise ValueError('reviewed private NAS address required')
    sockets=[]
    try:
        for address in (LOOPBACK, lan_ip):
            sock=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
            sockets.append(sock)
            sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind((address,port));sock.listen(128);sock.setblocking(False)
        return sockets
    except Exception:
        for sock in sockets:sock.close()
        raise

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--lan-ip',required=True);parser.add_argument('--client-ip',required=True)
    parser.add_argument('--port',type=int,default=8611)
    args=parser.parse_args()
    if os.environ.get('QSIM_SCHEDULER_ENABLED') != '0' or os.environ.get('QSIM_SCHEDULE_OWNER') != 'systemd':
        raise RuntimeError('existing single-scheduler configuration required')
    ipaddress.IPv4Address(args.client_ip)
    import uvicorn
    sockets=bind_listeners(args.lan_ip,args.port)
    try:
        # Match the old `python -m uvicorn` module search: this script lives
        # outside the reviewed runtime, while WorkingDirectory selects its
        # source. Do not accidentally load a different installed qsim copy.
        sys.path.insert(0,os.getcwd())
        from qsim.services.api.app import create_app
        app=LANReadGate(create_app(),args.lan_ip,args.client_ip,args.port)
        config=uvicorn.Config(app,workers=1,proxy_headers=False,access_log=False,
                             timeout_graceful_shutdown=5,log_level='warning')
        uvicorn.Server(config).run(sockets=sockets)
    finally:
        for sock in sockets:sock.close()

if __name__=='__main__':
    main()
