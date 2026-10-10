import json
import shutil
import subprocess
import sys

from qsim.services.offsite_backup import REMOTE_VERIFY, send_latest
from qsim.services.recovery import backup
from tests.test_recovery import fixture_root


def test_remote_integrity_check_and_atomic_publication(tmp_path, monkeypatch):
    import hashlib
    root=fixture_root(tmp_path)
    bundle=backup(root)
    remote=tmp_path/'remote'
    remote.mkdir()
    stage=remote/('.staging-'+bundle.name)
    shutil.copytree(bundle,stage)
    expected=hashlib.sha256((bundle/'manifest.json').read_bytes()).hexdigest()
    result=subprocess.run([sys.executable,'-c',REMOTE_VERIFY,str(remote),bundle.name,expected],capture_output=True,text=True)
    assert result.returncode==0
    assert not stage.exists()
    assert json.loads(result.stdout)['ok']
    final=remote/bundle.name
    (final/'config/setting.json').write_text('damaged')
    result=subprocess.run([sys.executable,'-c',REMOTE_VERIFY,str(remote),bundle.name,expected],capture_output=True,text=True)
    assert result.returncode!=0


def test_unconfigured_offsite_is_not_reported_as_success(tmp_path):
    assert send_latest(tmp_path)=={'ok':False,'status':'not_configured'}
