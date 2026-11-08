"""The name scan sees a real key, not only a short one."""

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _scan(tmp_path: Path, content: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "name-scan.sh", tmp_path / "scripts" / "name-scan.sh")
    (tmp_path / "notes.txt").write_text(content)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
    env = {"PATH": os.environ["PATH"], "NAME_SCAN_GENERIC_ONLY": "1", "HOME": str(tmp_path)}
    return subprocess.run(
        ["bash", str(tmp_path / "scripts" / "name-scan.sh")], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_name_scan_sees_a_real_key(tmp_path: Path) -> None:
    key = "b" + "f_" + "Ab3dE6gH" + "_" + "Zy9xW8vU7tS6rQ5pO4nM3lK2jI1hG0fE"  # built apart: this file is scanned too
    done = _scan(tmp_path, f"token {key}\n")
    assert done.returncode == 1, done.stdout + done.stderr


def test_name_scan_passes_a_clean_repo(tmp_path: Path) -> None:
    done = _scan(tmp_path, "nothing here\n")
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_key_shaped_hit_is_shown_by_place_never_by_its_text(tmp_path: Path) -> None:
    """The scan's output lands in CI logs and terminals: a real key it finds must not be printed there."""
    bf = "b" + "f_" + "Qw3eR6tY" + "_" + "Ui9oP8aS7dF6gH5jK4lZ3xC2vB1nM0qW"  # built apart: this file is scanned too
    sk = "s" + "k-" + "or-v1-" + "0123456789abcdef0123456789abcdef"
    done = _scan(tmp_path, f"first line\ntoken {bf}\nkey={sk}\n")
    assert done.returncode == 1, done.stdout + done.stderr
    out = done.stdout + done.stderr
    assert bf not in out and sk not in out
    assert bf[:12] not in out and sk[:12] not in out  # not a prefix either
    assert "notes.txt:2" in out and "notes.txt:3" in out
