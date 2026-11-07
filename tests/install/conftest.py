"""A rig for `install.sh`: a copy of the repo files, a throwaway home, and fakes for every outside program.

`install.sh` writes to a person's real Hermes home by design, so nothing here may let it near one. The rig builds the
child's environment from nothing (no inherited variables), its PATH holds only the fakes and symlinks to a short list of
plain tools (never a real hermes, docker or curl), and `run` refuses to start unless both facts check out.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The plain tools install.sh may use, linked into the rig's PATH from the host.
TOOLS = [
    "bash", "sh", "awk", "grep", "sed", "od", "tr", "mv", "chmod", "mktemp", "cut", "cat", "mkdir", "cp", "rm",
    "dirname", "basename", "head", "tail", "date", "sleep", "env", "printf", "ls", "wc", "id", "sort", "stty", "tee",
    "ln", "touch", "expr", "test", "true", "false",
]  # fmt: skip

HERMES = """#!/bin/sh
{log_line}
# Hermes re-homes onto the sticky profile unless the exported home is already a profile directory.
if [ "$(basename "$(dirname "$HERMES_HOME")")" != profiles ] && [ -f "$HERMES_HOME/active_profile" ]; then
  _p=$(cat "$HERMES_HOME/active_profile")
  if [ -n "$_p" ] && [ "$_p" != default ]; then HERMES_HOME="$HERMES_HOME/profiles/$_p"; fi
fi
if [ "$1" = --version ]; then echo "${{HK_HERMES_VERSION:-Hermes Agent v0.23.0 (test)}}"; fi
if [ "$1" = plugins ] && [ "$2" = install ]; then
  mkdir -p "$HERMES_HOME/plugins/hermes-kernel"
fi
if [ "$1" = plugins ] && [ "$2" = enable ]; then
  grep '^HERMES_KERNEL_TOKEN=' "$HERMES_HOME/.env" > "$HK_RIG/token_at_enable" 2>/dev/null || : > "$HK_RIG/token_at_enable"
fi
exit 0
"""

DOCKER = """#!/bin/sh
{log_line}
case " $* " in
  *" compose version "*) [ -z "$HK_NO_COMPOSE" ] || exit 1; echo "Docker Compose version v2.29.0" ;;
  *" ps "*) [ -z "$HK_PS_OUT" ] || echo "$HK_PS_OUT" ;;
  *" info "*) echo "$HK_RIG/docker-root" ;;
  *" image inspect "*) [ -n "$HK_IMAGE_PRESENT" ] || exit 1 ;;
esac
exit 0
"""

CURL = """#!/bin/sh
{log_line}
echo '{{"ok": true}}'
"""

UNAME = """#!/bin/sh
{log_line}
echo "${{HK_UNAME:-Linux x86_64}}"
"""

DF = """#!/bin/sh
{log_line}
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "fake 500000000 1000 ${{HK_DF_AVAIL_KB:-200000000}} 1% /"
"""

LOG_LINE = 'printf \'%s\' "$(basename "$0")" >> "$HK_RIG/log"; for a in "$@"; do printf \' %s\' "$a" >> "$HK_RIG/log"; done; echo >> "$HK_RIG/log"'  # noqa: E501


class Rig:
    def __init__(self, tmp_path: Path):
        self.base = tmp_path / "rig"
        self.root = self.base / "repo"
        self.user_home = self.base / "home"
        self.home = self.user_home / ".hermes"  # the Hermes home
        self.bin = self.base / "bin"
        self.tools = self.base / "tools"
        for d in (self.root, self.home, self.bin, self.tools, self.base / "docker-root"):
            d.mkdir(parents=True)
        (self.root / "plugin").mkdir()
        for name in (".env.example", "compose.yaml"):
            shutil.copy2(REPO / name, self.root / name)
        shutil.copy2(REPO / "plugin" / "plugin.yaml", self.root / "plugin" / "plugin.yaml")
        shutil.copy2(REPO / "install.sh", self.root / "install.sh")
        (self.base / "log").write_text("")
        (self.base / "meminfo").write_text("MemTotal:       32000000 kB\nMemAvailable:   16000000 kB\n")
        for name, body in (
            ("hermes", HERMES), ("docker", DOCKER), ("curl", CURL), ("uname", UNAME), ("df", DF),
        ):  # fmt: skip
            path = self.bin / name
            path.write_text(body.format(log_line=LOG_LINE))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        for tool in TOOLS:
            found = shutil.which(tool)
            if found:
                (self.tools / tool).symlink_to(found)
        self.path = f"{self.bin}{os.pathsep}{self.tools}"

    @property
    def log(self) -> list[str]:
        return (self.base / "log").read_text().splitlines()

    @property
    def token_at_enable(self) -> str:
        return (self.base / "token_at_enable").read_text().strip()

    def remove_fake(self, name: str) -> None:
        (self.bin / name).unlink()

    def set_meminfo(self, available_kb: int) -> None:
        (self.base / "meminfo").write_text(f"MemTotal: 32000000 kB\nMemAvailable: {available_kb} kB\n")

    def assert_contained(self) -> None:
        """Before anything runs: no hermes, docker or curl on the rig's PATH may live outside the throwaway tree."""
        for name in ("hermes", "docker", "curl"):
            found = shutil.which(name, path=self.path)
            assert found is None or Path(found).resolve().is_relative_to(self.base.resolve()), (
                f"{name} on the rig PATH resolves outside the throwaway tree: {found}"
            )
        assert self.home.resolve().is_relative_to(self.base.resolve()), "HERMES_HOME is outside the throwaway tree"
        assert self.user_home.resolve() != Path.home().resolve()

    def child_env(self, env: dict | None = None) -> dict:
        self.assert_contained()
        child = {
            "PATH": self.path,
            "HOME": str(self.user_home),
            "HERMES_HOME": str(self.home),
            "HK_RIG": str(self.base),
            "HK_PROC_MEMINFO": str(self.base / "meminfo"),
            "LANG": "C",
        }
        child.update(env or {})
        assert Path(child["HERMES_HOME"]).resolve().is_relative_to(self.base.resolve()), "HERMES_HOME escaped"
        for key, value in child.items():
            if key == "HERMES_BIN" and value:
                assert Path(value).resolve().is_relative_to(self.base.resolve()), "HERMES_BIN escaped"
        return child

    def run(self, *args: str, env: dict | None = None, input: str | None = None) -> subprocess.CompletedProcess:
        child = self.child_env(env)
        stdin = {"input": input} if input is not None else {"stdin": subprocess.DEVNULL}
        return subprocess.run(
            [str(self.root / "install.sh"), *args],
            cwd=str(self.root),
            env=child,
            capture_output=True,
            text=True,
            timeout=60,
            **stdin,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    if sys.platform == "win32":
        pytest.skip("install.sh is for Linux and macOS")
    return Rig(tmp_path)
