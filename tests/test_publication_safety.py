"""Synthetic publication guard regressions, unrelated to investment performance."""
import json
from pathlib import Path
import subprocess
import pytest
from tools.check_publication import findings, staged_files


@pytest.mark.parametrize("name", ["data/quant.db", ".env", "deploy/runtime.env", "logs/runtime.log", "docs/private.png", "config/account.local.json"])
def test_runtime_and_personal_files_are_not_publishable(name):
    assert "not_source_allowlisted" in findings(name, b"synthetic")


def test_personal_paths_addresses_keys_and_credentials_are_detected_without_values():
    home = "/" + "Users" + "/synthetic-person/source"
    ip = "192." + "168." + "1.9"
    key = "-----BEGIN " + "PRIVATE KEY-----"
    secret = "ghp_" + "x" * 36
    categories = findings("docs/test.md", (home + "\n" + ip + "\n" + key + "\n" + secret).encode())
    assert set(categories) == {"personal_home_path", "private_network_address", "private_key", "possible_credential"}
    assert all(secret not in item and ip not in item for item in categories)


def test_example_networks_and_copyright_attributions_are_preserved():
    assert findings("tests/test_example.py", b"192.0.2.10 198.51.100.10 127.0.0.1 0.0.0.0") == []
    attribution = b"Copyright Synthetic Vendor <author@" + b"vendor.test>"
    assert findings("qsim/services/web/static/licenses/vendor-LICENSE.txt", attribution) == []
    assert findings("docs/test.md", attribution) == ["email_review_required"]


def test_local_identity_denylist_covers_names_and_content():
    assert findings("docs/test.md", b"Synthetic Person", denied=("synthetic person",)) == ["local_identity_denylist"]
    assert findings("docs/synthetic-person.md", b"", denied=("synthetic-person",)) == ["local_identity_denylist"]


def test_binary_and_symlink_blobs_are_rejected():
    assert "binary_or_invalid_utf8" in findings("qsim/file.py", b"\xff")
    assert "non_regular_file" in findings("qsim/file.py", b"", mode="120000")


def test_staged_scan_checks_index_blob_instead_of_changed_worktree(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    folder = tmp_path / "docs"; folder.mkdir()
    path = folder / "note.md"
    path.write_text("synthetic " + "/" + "home" + "/test-person/project")
    subprocess.run(["git", "add", "docs/note.md"], cwd=tmp_path, check=True)
    path.write_text("sanitized worktree, but index is still unsafe")
    entries = list(staged_files(tmp_path))
    assert len(entries) == 1
    name, content, mode = entries[0]
    assert findings(name, content, mode=mode) == ["personal_home_path"]


def test_release_configuration_contains_no_private_performance_claims():
    root = Path(__file__).resolve().parents[1]
    for name in ("multi-factor-v3.json", "multi-factor-v4.json"):
        data = json.loads((root / "config" / name).read_text())
        assert data["evidence"]["status"] == "NOT_VERIFIED"
        assert not any(key.startswith(("full_", "oos_")) for key in data["evidence"])
    migration = json.loads((root / "config/strategy-migration.json").read_text())
    assert all(not set(item) & {"promotion", "retirement", "rebaseline"} for item in migration["strategies"])
