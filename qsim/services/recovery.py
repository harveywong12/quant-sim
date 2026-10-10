"""Complete, verified backup bundles and isolated restore drills."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from qsim.services.mutation_lock import mutation_guard

ROOT = Path(__file__).resolve().parents[2]
DATABASES = ("data/quant.db", "data/v2_market.db", "data/v2_events.db", "data/v2_shadow.db",
             "artifacts/scheduler_state.db", "artifacts/experiments.db")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _db_check(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"database integrity failed: {path.name}")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                for name in ("qsim_accounts", "qsim_snapshots", "qsim_trades", "qsim_order_intents",
                             "bars", "stock_bars", "cb_daily_raw", "cb_indicator", "job_runs") if name in tables}
    finally:
        conn.close()


def _state(root: Path, name: str, payload: dict) -> None:
    path = root / "artifacts" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(path)


def backup(root: Path = ROOT) -> Path:
    base = root / "backups" / "recovery"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    base.chmod(0o700)
    now = dt.datetime.now(dt.timezone.utc)
    name = now.strftime("%Y%m%dT%H%M%SZ")
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=base))
    final = base / name
    manifest = {"kind": "qsim-recovery", "version": 1, "created_at": now.isoformat(), "files": {},
                "runtime": {"python": sys.version.split()[0],
                            "packages": {item.metadata["Name"]: item.version
                                         for item in importlib.metadata.distributions() if item.metadata.get("Name")}}}
    try:
        with mutation_guard("recovery.backup"):
            replay_dbs = tuple(path.relative_to(root).as_posix() for path in (root / "artifacts" / "historical_replays").glob("*.db"))
            for relative in DATABASES + replay_dbs:
                source = root / relative
                if not source.is_file():
                    if relative in ("data/quant.db", "data/v2_market.db", "artifacts/scheduler_state.db"):
                        raise RuntimeError(f"required database missing: {relative}")
                    continue
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                reader = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
                writer = sqlite3.connect(target)
                try:
                    reader.backup(writer, pages=1000)
                finally:
                    writer.close()
                    reader.close()
                target.chmod(0o600)
                manifest["files"][relative] = {"kind": "sqlite", "sha256": _hash(target),
                                               "counts": _db_check(target), "mode": 0o600}
            sources = []
            for directory in ("qsim", "scripts", "config", "deploy", "artifacts", ".secrets", "data/strategies"):
                sources.extend(path for path in (root / directory).rglob("*") if path.is_file())
            sources.extend(root / name for name in ("pyproject.toml", "README.md", "requirements.txt", "requirements-lock.txt", "requirements.runtime.lock.txt") if (root / name).is_file())
            for source in sources:
                relative = source.relative_to(root).as_posix()
                if (relative in manifest["files"] or source.is_symlink() or "__pycache__" in source.parts
                        or source.suffix in (".db", ".pyc") or source.name.endswith(("-wal", "-shm", ".tmp"))):
                    continue
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                target.chmod(0o600)
                mode = 0o600 if ".secrets" in source.parts else (0o755 if source.stat().st_mode & 0o111 else 0o644)
                manifest["files"][relative] = {"kind": "file", "sha256": _hash(target), "mode": mode}
            unit_root = Path.home() / ".config" / "systemd" / "user"
            for source in unit_root.rglob("*"):
                if source.is_file() and not source.is_symlink() and source.relative_to(unit_root).parts[0].startswith("qsim-"):
                    relative = "runtime-systemd/" + source.relative_to(unit_root).as_posix()
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copyfile(source, target)
                    target.chmod(0o600)
                    manifest["files"][relative] = {"kind": "file", "sha256": _hash(target), "mode": 0o644}
            proc = subprocess.run(["git", "-c", f"safe.directory={root.resolve()}", "rev-parse", "HEAD"],
                                  cwd=root, capture_output=True, text=True)
            manifest["git_commit"] = proc.stdout.strip() if proc.returncode == 0 else None
            (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            (stage / "manifest.json").chmod(0o600)
            stage.rename(final)
        _state(root, "recovery_backup_state.json", {"ok": True, "bundle": name,
               "files": len(manifest["files"]), "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        # Keep seven recent bundles plus four weekly and six monthly checkpoints.
        bundles = []
        for path in sorted(base.iterdir(), reverse=True):
            try:
                item = json.loads((path / "manifest.json").read_text())
                if item.get("kind") == "qsim-recovery":
                    bundles.append((path, dt.datetime.fromisoformat(item["created_at"]).astimezone(dt.timezone(dt.timedelta(hours=8)))))
            except (OSError, ValueError, KeyError):
                continue
        keep = {path for path, _ in bundles[:7]}
        keep.update([path for path, date in bundles if date.weekday() == 0][:4])
        keep.update([path for path, date in bundles if date.day == 1][:6])
        for path, _ in bundles:
            if path not in keep:
                shutil.rmtree(path)
        return final
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def restore(bundle: Path, destination: Path) -> dict:
    if destination.resolve() == ROOT.resolve() or ROOT.resolve() in destination.resolve().parents:
        raise RuntimeError("restore must use an isolated directory outside the live project")
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("restore destination must be empty")
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("kind") != "qsim-recovery":
        raise RuntimeError("not a qsim recovery bundle")
    # Verify everything before writing a restore target.
    for relative, item in manifest["files"].items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError("unsafe bundle path")
        source = bundle / path
        if (source.is_symlink() or not source.resolve().is_relative_to(bundle.resolve())
                or any((bundle / Path(*path.parts[:i])).is_symlink() for i in range(1, len(path.parts)))):
            raise RuntimeError("unsafe bundle path or symlink")
        if _hash(source) != item["sha256"]:
            raise RuntimeError(f"backup checksum mismatch: {relative}")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination.chmod(0o700)
    for relative, item in manifest["files"].items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(bundle / relative, target)
        target.chmod(item.get("mode", 0o600))
        if item["kind"] == "sqlite" and _db_check(target) != item["counts"]:
            raise RuntimeError(f"restored row counts mismatch: {relative}")
    return {"ok": True, "files": len(manifest["files"])}


def drill(bundle: Path, root: Path = ROOT) -> dict:
    base = Path(tempfile.mkdtemp(prefix="qsim-restore-drill-", dir=root.parent))
    destination = base / "restored"
    try:
        result = restore(bundle, destination)
        env = {**os.environ, "PYTHONPATH": str(destination), "QSIM_REFACTOR_ROOT": str(destination),
               "QSIM_CALENDAR_FILE": str(destination / "config" / "trading-calendar.json"),
               "QSIM_MUTATION_LOCK": str(destination / "data" / ".qsim-mutation.lock"),
               "QSIM_SCHEDULER_ENABLED": "0"}
        code = '''import json
from fastapi.testclient import TestClient
from qsim.services.api.app import create_app
with TestClient(create_app()) as client:
    health=client.get('/healthz'); ready=client.get('/readyz'); page=client.get('/')
    assert health.status_code==200 and page.status_code==200
    print(json.dumps({'health':health.status_code,'readiness':ready.status_code,'page':page.status_code}))
'''
        proc = subprocess.run([sys.executable, "-c", code], cwd=destination, env=env,
                              capture_output=True, text=True, timeout=90)
        if proc.returncode != 0:
            raise RuntimeError("restored application smoke check failed: " + proc.stderr[-400:])
        result.update({"application": json.loads(proc.stdout.strip().splitlines()[-1]),
                       "bundle": bundle.name, "finished_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        _state(root, "recovery_drill_state.json", result)
        return result
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("backup", "drill", "drill-latest", "restore"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    try:
        if args.action == "backup":
            result = {"ok": True, "bundle": backup().name}
        elif args.action == "drill-latest":
            bundles = sorted((ROOT / "backups" / "recovery").glob("*/manifest.json"))
            if not bundles:
                raise RuntimeError("no complete recovery bundle exists")
            result = drill(bundles[-1].parent)
        elif args.action == "drill" and args.bundle:
            result = drill(args.bundle)
        elif args.action == "restore" and args.bundle and args.destination:
            result = restore(args.bundle, args.destination)
        else:
            parser.error("drill requires --bundle; restore also requires --destination")
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        from qsim.services.notify import notify
        notify("[qsim] 备份或恢复验证失败", str(exc)[:300])
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
