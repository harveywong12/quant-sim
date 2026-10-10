"""Offline audit only: pause job clocks; disallow external sockets."""
import socket
from apscheduler.schedulers.background import BackgroundScheduler
_original_start = BackgroundScheduler.start

def _paused_start(self, *args, **kwargs):
    kwargs['paused'] = True
    return _original_start(self, *args, **kwargs)

def _offline_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        raise RuntimeError('audit tests are offline: external socket blocked')
    return _original_connect(self, address)
_original_connect = socket.socket.connect
BackgroundScheduler.start = _paused_start
socket.socket.connect = _offline_connect


def pytest_collection_finish(session):
    import json, os
    from pathlib import Path
    output=os.environ.get('QSIM_AUDIT_COLLECT_FILE')
    if output:
        Path(output).write_text(json.dumps({'collected':len(session.items),'nodeids':[i.nodeid for i in session.items]},indent=2))
