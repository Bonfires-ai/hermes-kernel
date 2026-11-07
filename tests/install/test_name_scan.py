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
