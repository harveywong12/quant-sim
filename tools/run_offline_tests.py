"""Run synthetic tests in a temporary copy without runtime credentials/network."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=root / "artifacts/test-reports")
    parser.add_argument("targets", nargs="*", default=["tests"])
    args = parser.parse_args()
    out = args.output_dir.resolve(); out.mkdir(parents=True, exist_ok=True)
    began = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="qsim-offline-") as temporary:
        isolated = Path(temporary) / "source"
        shutil.copytree(root, isolated, ignore=shutil.ignore_patterns(".git", ".venv", "venv", ".secrets", ".env", ".env.*", "runtime.env", "data", "artifacts", "logs", "backups", "reports", "results", "audit_output", "__pycache__", ".pytest_cache", "build", "dist", "*.egg-info", "*.db", "*.db-*", "*.sqlite*"))
        env = {key: value for key, value in os.environ.items() if key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")}
        # Keep business gates under each synthetic fixture's control. Forcing
        # production stop flags would prevent tests from exercising fills and
        # settlement. Safety comes from the temporary copy, absent credentials,
        # blocked sockets and paused clocks, as in the existing audit harness.
        env.update(HOME=temporary, PYTHONPATH=str(isolated / "audit_tools") + os.pathsep + str(isolated), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONDONTWRITEBYTECODE="1", QSIM_AUDIT_COLLECT_FILE=str(out / "collection.json"), QSIM_LEGACY_ROOT=temporary + "/legacy", QSIM_SYNC_BACKUP_ROOT=temporary + "/backups", QSIM_AUTH_TOKEN_FILE=temporary + "/no-auth", QSIM_LEGACY_AUTH_TOKEN_FILE=temporary + "/no-auth", QSIM_MARKET_TOKEN_FILE=temporary + "/no-market", QSIM_NTFY_URL="", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1", TZ="Asia/Shanghai")
        command = [sys.executable, "-m", "pytest", "-p", "audit_pytest_guard", "-ra", "--junitxml=" + str(out / "tests.xml"), *(args.targets or ["tests"])]
        with (out / "tests.log").open("w") as log:
            result = subprocess.run(command, cwd=isolated, env=env, stdout=log, stderr=subprocess.STDOUT)
    counts = {key: 0 for key in ("passed", "failed", "errors", "skipped", "xfail", "xpass")}
    xml = out / "tests.xml"
    if xml.exists():
        for case in ET.parse(xml).iter("testcase"):
            if case.find("failure") is not None:
                counts["failed"] += 1
            elif case.find("error") is not None:
                counts["errors"] += 1
            elif case.find("skipped") is not None:
                counts["xfail" if case.find("skipped").get("type") == "pytest.xfail" else "skipped"] += 1
            else:
                counts["passed"] += 1
    collection = out / "collection.json"
    report = {"kind": "synthetic/offline", "test_modules": len(list((root / "tests").glob("test_*.py"))), "collected_cases": json.loads(collection.read_text())["collected"] if collection.exists() else None, "actually_executed": sum(counts[key] for key in ("passed", "failed", "xfail", "xpass")), "counts": counts, "exit_code": result.returncode, "duration_seconds": round(time.monotonic() - began, 3), "isolation": "temporary source copy; blank credentials; external sockets blocked; clocks paused"}
    (out / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
