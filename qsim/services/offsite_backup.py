"""Ship a complete bundle over SSH and verify every remote file before publication."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

from qsim.services.recovery import ROOT, _state

REMOTE_VERIFY = '''import hashlib,json,sys
from pathlib import Path
base=Path(sys.argv[1]); name=sys.argv[2]; expected=sys.argv[3]
source=base/('.staging-'+name); final=base/name
if not source.exists(): source=final
assert not source.is_symlink()
m=source/'manifest.json'
assert hashlib.sha256(m.read_bytes()).hexdigest()==expected
manifest=json.loads(m.read_text())
assert manifest['kind']=='qsim-recovery'
for relative,item in manifest['files'].items():
 p=Path(relative)
 assert not p.is_absolute() and '..' not in p.parts
 f=source/p
 assert not f.is_symlink()
 h=hashlib.sha256()
 with f.open('rb') as r:
  for b in iter(lambda:r.read(1024*1024),b''):h.update(b)
 assert h.hexdigest()==item['sha256'],relative
if source!=final:
 assert not final.exists()
 source.rename(final)
print(json.dumps({'ok':True,'bundle':name,'files':len(manifest['files'])}))
'''


def send_latest(root: Path = ROOT) -> dict:
    config = root / '.secrets' / 'offsite-backup.json'
    if not config.is_file():
        return {'ok': False, 'status': 'not_configured'}
    target = json.loads(config.read_text())
    host, directory = target['host'], target['directory']
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', host) or not re.fullmatch(r'/[A-Za-z0-9_./-]+', directory) or '..' in Path(directory).parts:
        raise ValueError('备份目标必须是 SSH 主机和绝对目录')
    bundles = sorted((root/'backups/recovery').glob('*/manifest.json'))
    if not bundles:
        raise RuntimeError('没有完整备份可发送')
    bundle = bundles[-1].parent
    ssh = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'StrictHostKeyChecking=yes']
    if target.get('identity_file'):
        ssh += ['-i', str(Path(target['identity_file']).expanduser())]
    stage = directory.rstrip('/') + '/.staging-' + bundle.name
    exists = subprocess.run(ssh + [host, 'test -f ' + shlex.quote(directory.rstrip('/') + '/' + bundle.name + '/manifest.json')], capture_output=True, text=True, timeout=30)
    if exists.returncode not in (0, 1):
        raise RuntimeError('异地 SSH 目标不可用')
    if exists.returncode == 1:
        subprocess.run(ssh + [host, 'umask 077; mkdir -p ' + shlex.quote(stage)], check=True, capture_output=True, text=True, timeout=30)
        subprocess.run(['rsync','-a','--partial','--chmod=D700,F600','-e',shlex.join(ssh),str(bundle)+'/',host+':'+stage+'/'],
                   check=True,capture_output=True,text=True,timeout=3600)
    expected = hashlib.sha256((bundle/'manifest.json').read_bytes()).hexdigest()
    command = 'python3 - ' + shlex.join([directory,bundle.name,expected])
    result = subprocess.run(ssh+[host,command],input=REMOTE_VERIFY,check=True,capture_output=True,text=True,timeout=600)
    payload=json.loads(result.stdout.strip().splitlines()[-1])
    import datetime as dt
    payload['finished_at']=dt.datetime.now(dt.timezone.utc).isoformat()
    _state(root,'offsite_backup_state.json',payload)
    return payload


if __name__ == '__main__':
    try:
        result=send_latest()
        print(json.dumps(result,ensure_ascii=False))
        raise SystemExit(0 if result['ok'] else 2)
    except Exception:
        from qsim.services.notify import notify
        error={'ok':False,'status':'failed','error':'异地备份传输或远端校验失败，请检查目标与 SSH 连接'}
        _state(ROOT,'offsite_backup_state.json',error)
        notify('[qsim] 异地备份失败',error['error'])
        print(json.dumps(error,ensure_ascii=False))
        raise SystemExit(1)
