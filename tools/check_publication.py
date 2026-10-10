"""Check publishable source or staged blobs without printing sensitive values."""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

SOURCE_DIRS = {"qsim", "tests", "config", "scripts", "migrations", "schemas", "docs", "deploy", "tools", "audit_tools", ".github"}
ROOT_FILES = {"README.md", "THIRD_PARTY_NOTICES.md", "pyproject.toml", ".gitignore", "requirements.txt", "requirements-lock.txt", "requirements.runtime.lock.txt"}
SOURCE_SUFFIXES = {".py", ".sh", ".json", ".sql", ".md", ".txt", ".toml", ".html", ".css", ".js", ".yaml", ".yml", ".service", ".timer", ".example"}
LOCAL_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
USER_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+(?:/|\b)")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
SECRET = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9]{32,})")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LITERAL_CREDENTIAL = re.compile(r'''(?im)^\s*(?:[A-Z_]*(?:TOKEN|PASSWORD|API_KEY)|(?:tushare_)?token|password|api_key)\s*=\s*["']([A-Za-z0-9_+/=-]{24,})["']''')


def path_allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if len(path.parts) == 1:
        return name in ROOT_FILES
    if path.parts[0] not in SOURCE_DIRS:
        return False
    if any(part in LOCAL_DIRS or part in {".secrets", "data", "logs", "backups", "artifacts", "reports", "results"} for part in path.parts):
        return False
    if path.name == ".env" or path.name.startswith(".env.") or path.name == "runtime.env":
        return False
    if path.parts[0] == "config" and (path.name.startswith("local") or ".local." in path.name):
        return False
    return path.suffix in SOURCE_SUFFIXES


def findings(name: str, content: bytes, *, mode: str = "100644", denied: tuple[str, ...] = ()) -> list[str]:
    result = []
    if not path_allowed(name):
        result.append("not_source_allowlisted")
    if mode not in {"100644", "100755"}:
        result.append("non_regular_file")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return result + ["binary_or_invalid_utf8"]
    if "\x00" in text:
        result.append("binary_content")
    if USER_PATH.search(text):
        result.append("personal_home_path")
    for value in IPV4.findall(text):
        try:
            ip = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            continue
        # RFC 5737 examples, loopback and wildcard validation fixtures are safe.
        examples = (ipaddress.IPv4Network("192.0.2.0/24"), ipaddress.IPv4Network("198.51.100.0/24"), ipaddress.IPv4Network("203.0.113.0/24"))
        if ip.is_private and not (ip.is_loopback or ip.is_unspecified or any(ip in net for net in examples)):
            result.append("private_network_address")
            break
    if SECRET.search(text) or LITERAL_CREDENTIAL.search(text):
        result.append("possible_credential")
    if PRIVATE_KEY.search(text):
        result.append("private_key")
    if any(value and value.casefold() in (name + "\n" + text).casefold() for value in denied):
        result.append("local_identity_denylist")
    # Third-party legal attribution must be retained. Never erase its authors.
    if not name.startswith("qsim/services/web/static/licenses/"):
        emails = EMAIL.findall(text)
        if any(not (email.lower().endswith(("@example.com", "@example.org", "@example.invalid", ".example"))) for email in emails):
            result.append("email_review_required")
    return sorted(set(result))


def staged_files(root: Path):
    raw = subprocess.check_output(["git", "ls-files", "--stage", "-z"], cwd=root)
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        metadata, encoded_name = entry.split(b"\t", 1)
        mode, sha, stage = metadata.decode().split()
        name = encoded_name.decode("utf-8")
        if stage != "0":
            raise ValueError("unmerged index entries")
        data = subprocess.check_output(["git", "cat-file", "blob", sha], cwd=root)
        yield name, data, mode


def source_files(root: Path):
    # Ignore developer tools and ignored runtime roots; they are checked if
    # actually staged. Never read local credential files or business databases.
    candidates = [root / name for name in ROOT_FILES]
    for name in SOURCE_DIRS:
        folder = root / name
        if not folder.is_dir():
            continue
        import os
        for parent, directories, names in os.walk(folder, followlinks=False):
            directories[:] = [d for d in directories if d not in LOCAL_DIRS and d not in {".secrets", "data", "logs", "backups", "artifacts", "reports", "results"}]
            candidates.extend(Path(parent) / filename for filename in names)
    for path in sorted(candidates):
        if not path.is_file() and not path.is_symlink():
            continue
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            yield name, b"", "120000"
        elif path_allowed(name):
            yield name, path.read_bytes(), "100644"
        elif not any(part in LOCAL_DIRS for part in path.parts):
            # Report the forbidden path without opening it.
            yield name, b"", "100644"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--deny-file", type=Path)
    args = parser.parse_args()
    denied = ()
    if args.deny_file:
        values = json.loads(args.deny_file.read_text())
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError("deny file must be a JSON array of strings")
        denied = tuple(values)
    issues = []; total = 0
    entries = staged_files(args.root) if args.staged else source_files(args.root)
    for name, data, mode in entries:
        total += 1
        categories = findings(name, data, mode=mode, denied=denied)
        if categories:
            issues.append({"path": name, "categories": categories})
    print(json.dumps({"status": "PASS" if not issues else "BLOCKED", "scope": "index" if args.staged else "source", "checked_files": total, "issues": issues}, ensure_ascii=False, indent=2))
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
