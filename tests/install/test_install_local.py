import json
import os
import re
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
KEYS = {"OPENROUTER_API_KEY": "key-SENTINEL-one", "VOYAGE_API_KEY": "key-SENTINEL-two"}


def parse_env(path):
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            out[key.removeprefix("export ").strip()] = value
    return out


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_wrong_hermes_stops_before_any_write(rig):
    result = rig.run(env={**KEYS, "HK_HERMES_VERSION": "Hermes Agent v0.24.0 (test)"})
    assert result.returncode != 0
    assert "v0.23.0" in result.stderr
    assert not (rig.root / ".env").exists()
    assert list(rig.home.iterdir()) == []
    assert not any(line.startswith("docker") for line in rig.log)


def test_no_hermes_says_so(rig):
    rig.remove_fake("hermes")
    result = rig.run(env=KEYS)
    assert result.returncode != 0
    assert "never installs hermes" in result.stderr.lower()
    assert not (rig.root / ".env").exists()
    assert list(rig.home.iterdir()) == []


def test_fresh_install(rig):
    (rig.home / ".env").write_text("FIRST=1\nexport HERMES_KERNEL_TOKEN=old\nLAST=2\n")
    result = rig.run(env=KEYS)
    assert result.returncode == 0, result.stderr
    env_file = rig.root / ".env"
    assert mode(env_file) == 0o600
    values = parse_env(env_file)
    assert re.fullmatch(r"[0-9a-f]{48}", values["NEO4J_PASSWORD"])
    assert "/" not in values["NEO4J_PASSWORD"]
    assert re.fullmatch(r"[0-9a-f]{48}", values["KT_TOKEN"])
    assert values["OPENROUTER_API_KEY"] == KEYS["OPENROUTER_API_KEY"]
    assert values["VOYAGE_API_KEY"] == KEYS["VOYAGE_API_KEY"]
    assert values["KT_BONFIRE_ID"] == "hermes"
    home_env = rig.home / ".env"
    assert mode(home_env) == 0o600
    lines = home_env.read_text().splitlines()
    assert lines == ["FIRST=1", f"HERMES_KERNEL_TOKEN={values['KT_TOKEN']}", "LAST=2"]
    assert json.loads((rig.home / "hermes-kernel.json").read_text()) == {"url": "http://127.0.0.1:8787"}
    assert any(line.startswith("curl") and "127.0.0.1:8787/healthz" in line for line in rig.log)
    assert result.stdout.rstrip().splitlines()[-1].endswith("/kernel")


def test_enable_comes_last_and_finds_the_token(rig):
    result = rig.run(env=KEYS)
    assert result.returncode == 0, result.stderr
    hermes = [line for line in rig.log if line.startswith("hermes")]
    steps = [line for line in hermes if "--version" not in line]
    assert re.fullmatch(r"hermes plugins install \S+ --enable", steps[0])
    assert steps[1:] == ["hermes config set memory.provider hermes-kernel", "hermes plugins enable hermes-kernel"]
    token = parse_env(rig.root / ".env")["KT_TOKEN"]
    assert rig.token_at_enable == f"HERMES_KERNEL_TOKEN={token}"
    # the stack is up before the plugin is touched
    log = rig.log
    assert max(i for i, line in enumerate(log) if line.startswith("docker") and " up " in line) < log.index(steps[0])


def test_second_run_keeps_everything(rig):
    assert rig.run(env=KEYS).returncode == 0
    before = (rig.root / ".env").read_bytes()
    home_before = (rig.home / ".env").read_bytes()
    start = len(rig.log)
    result = rig.run()  # no keys in the environment: the kept .env holds them
    assert result.returncode == 0, result.stderr
    assert (rig.root / ".env").read_bytes() == before
    assert (rig.home / ".env").read_bytes() == home_before
    assert "kept" in result.stdout
    second = rig.log[start:]
    assert not any(re.search(r"\bdown\b", line) for line in second)
    assert any(line.startswith("hermes plugins update hermes-kernel") for line in second)
    assert not any("plugins install" in line or "--force" in line for line in second)
    assert "hermes gateway restart --detach" in result.stdout


def test_blank_value_in_a_kept_env_is_filled(rig):
    example = (rig.root / ".env.example").read_text()
    (rig.root / ".env").write_text(example.replace("OPENROUTER_API_KEY=", "OPENROUTER_API_KEY=kept-key"))
    (rig.root / ".env").chmod(0o644)
    result = rig.run(env={"VOYAGE_API_KEY": KEYS["VOYAGE_API_KEY"]})
    assert result.returncode == 0, result.stderr
    after = (rig.root / ".env").read_text().splitlines()
    values = parse_env(rig.root / ".env")
    assert values["OPENROUTER_API_KEY"] == "kept-key"
    assert values["VOYAGE_API_KEY"] == KEYS["VOYAGE_API_KEY"]
    assert values["NEO4J_PASSWORD"] and values["KT_TOKEN"]
    assert mode(rig.root / ".env") == 0o600
    untouched = [
        line
        for line in example.splitlines()
        if not re.match(r"(NEO4J_PASSWORD|OPENROUTER_API_KEY|VOYAGE_API_KEY|KT_TOKEN)=", line)
    ]
    assert [line for line in after if line in untouched] == untouched
    assert len(after) == len(example.splitlines())


def test_shell_token_beats_env(rig):
    example = (rig.root / ".env.example").read_text()
    (rig.root / ".env").write_text(example.replace("KT_TOKEN=", "KT_TOKEN=yyyy"))
    result = rig.run(env={**KEYS, "KT_TOKEN": "xxxx"})
    assert result.returncode == 0, result.stderr
    assert parse_env(rig.root / ".env")["KT_TOKEN"] == "yyyy"
    assert (rig.home / ".env").read_text().splitlines().count("HERMES_KERNEL_TOKEN=xxxx") == 1
    assert rig.token_at_enable == "HERMES_KERNEL_TOKEN=xxxx"


def test_shell_token_is_not_written_to_a_blank_env(rig):
    result = rig.run(env={**KEYS, "KT_TOKEN": "xxxx"})
    assert result.returncode == 0, result.stderr
    assert parse_env(rig.root / ".env")["KT_TOKEN"] == ""
    assert "xxxx" not in (rig.root / ".env").read_text()


def test_no_secret_on_any_command_line(rig):
    result = rig.run(env={**KEYS, "KT_TOKEN": "sentinel-kt-token-3", "NEO4J_PASSWORD": "sentinel-neo-4"})
    assert result.returncode == 0, result.stderr
    text = "\n".join(rig.log)
    for secret in (*KEYS.values(), "sentinel-kt-token-3", "sentinel-neo-4"):
        assert secret not in text
    generated = parse_env(rig.root / ".env")
    assert generated["NEO4J_PASSWORD"] == "sentinel-neo-4"


def test_unattended_without_keys_stops(rig):
    result = rig.run(env={"VOYAGE_API_KEY": KEYS["VOYAGE_API_KEY"]})
    assert result.returncode != 0
    assert "OPENROUTER_API_KEY" in result.stderr
    assert not (rig.root / ".env").exists()
    assert list(rig.home.iterdir()) == []
    assert not any(" up " in line or " build " in line for line in rig.log)


def test_not_linux_x86_64_stops(rig):
    for value in ("Darwin arm64", "Linux aarch64"):
        result = rig.run(env={**KEYS, "HK_UNAME": value})
        assert result.returncode != 0
        assert "Linux x86_64" in result.stderr
        assert not (rig.root / ".env").exists()


def test_low_ram_stops_unless_the_stack_is_running(rig):
    rig.set_meminfo(8 * 1024 * 1024)
    result = rig.run(env=KEYS)
    assert result.returncode != 0
    assert "9 GiB" in result.stderr
    assert not (rig.root / ".env").exists()
    result = rig.run(env={**KEYS, "HK_PS_OUT": "hermes-kernel-turn-service-1"})
    assert result.returncode == 0, result.stderr


def test_low_disk_stops_unless_the_image_is_there(rig):
    low = {**KEYS, "HK_DF_AVAIL_KB": str(4 * 1024 * 1024)}
    result = rig.run(env=low)
    assert result.returncode != 0
    assert "8 GiB" in result.stderr
    assert not (rig.root / ".env").exists()
    result = rig.run(env={**low, "HK_IMAGE_PRESENT": "1"})
    assert result.returncode == 0, result.stderr


def test_no_build_skips_the_build(rig):
    result = rig.run("--no-build", env=KEYS)
    assert result.returncode == 0, result.stderr
    assert not any(" build" in line for line in rig.log if line.startswith("docker"))


def test_a_bare_source_gives_no_local_warning(rig):
    result = rig.run("--plugin-source", "Bonfires-ai/hermes-kernel/plugin", env=KEYS)
    assert result.returncode == 0, result.stderr
    assert "uncommitted" not in result.stdout
    assert "hermes plugins install Bonfires-ai/hermes-kernel/plugin --enable" in rig.log


def test_a_local_source_warns_once_that_it_installs_head(rig):
    result = rig.run("--plugin-source", "file:///somewhere#plugin", env=KEYS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("installs HEAD") == 1
    assert "hermes plugins install file:///somewhere#plugin --enable" in rig.log


# ---- fix round 1


def _profile_files(home):
    return home / ".env", home / "hermes-kernel.json", home / "plugins" / "hermes-kernel"


def test_sticky_profile_gets_the_whole_install(rig):
    (rig.home / "active_profile").write_text("work\n")
    result = rig.run(env=KEYS)
    assert result.returncode == 0, result.stderr
    assert "work" in result.stdout
    profile = rig.home / "profiles" / "work"
    token_file, url_file, plugin_dir = _profile_files(profile)
    assert plugin_dir.is_dir()
    token = parse_env(rig.root / ".env")["KT_TOKEN"]
    assert token_file.read_text().splitlines() == [f"HERMES_KERNEL_TOKEN={token}"]
    assert json.loads(url_file.read_text()) == {"url": "http://127.0.0.1:8787"}
    assert rig.token_at_enable == f"HERMES_KERNEL_TOKEN={token}"
    for stray in _profile_files(rig.home):
        assert not stray.exists()


def test_sticky_profile_second_run_updates(rig):
    (rig.home / "active_profile").write_text("work")
    assert rig.run(env=KEYS).returncode == 0
    start = len(rig.log)
    result = rig.run()
    assert result.returncode == 0, result.stderr
    second = rig.log[start:]
    assert any(line.startswith("hermes plugins update hermes-kernel") for line in second)
    assert not any("plugins install" in line for line in second)


def test_profile_named_default_is_the_root_home(rig):
    (rig.home / "active_profile").write_text("default\n")
    result = rig.run(env=KEYS)
    assert result.returncode == 0, result.stderr
    assert (rig.home / ".env").exists()
    assert not (rig.home / "profiles").exists()


def test_home_already_under_profiles_is_trusted(rig):
    inner = rig.home / "profiles" / "x"
    inner.mkdir(parents=True)
    (inner / "active_profile").write_text("zzz")
    result = rig.run(env={**KEYS, "HERMES_HOME": str(inner)})
    assert result.returncode == 0, result.stderr
    assert (inner / ".env").exists()
    assert not (inner / "profiles").exists()


def test_profile_name_with_a_slash_or_dots_is_refused(rig):
    for name in ("../evil", "a/b", ".."):
        (rig.home / "active_profile").write_text(name)
        result = rig.run(env=KEYS)
        assert result.returncode != 0
        assert "profile" in result.stderr
        assert not (rig.root / ".env").exists()
        assert sorted(p.name for p in rig.home.iterdir()) == ["active_profile"]


def env_with(rig, text, shell=None):
    (rig.root / ".env").write_text(text)
    (rig.root / ".env").chmod(0o600)
    return rig.run(env={**KEYS, **(shell or {})})


EXAMPLE_BLANKS = "OPENROUTER_API_KEY=\nVOYAGE_API_KEY=\nKT_BONFIRE_ID=hermes\nMK_VERSION=2.0.1\n"


def test_export_prefixed_value_is_kept(rig):
    result = env_with(rig, "export NEO4J_PASSWORD=mine\nKT_TOKEN=tok\n" + EXAMPLE_BLANKS)
    assert result.returncode == 0, result.stderr
    text = (rig.root / ".env").read_text()
    assert text.count("NEO4J_PASSWORD") == 1
    assert "export NEO4J_PASSWORD=mine" in text


def test_indented_value_is_kept(rig):
    result = env_with(rig, "NEO4J_PASSWORD=pw\n  KT_TOKEN=abc\n" + EXAMPLE_BLANKS)
    assert result.returncode == 0, result.stderr
    text = (rig.root / ".env").read_text()
    assert text.count("KT_TOKEN") == 1
    assert rig.token_at_enable == "HERMES_KERNEL_TOKEN=abc"


def test_trailing_comment_is_not_part_of_the_token(rig):
    result = env_with(rig, "NEO4J_PASSWORD=pw\nKT_TOKEN=abc # note\n" + EXAMPLE_BLANKS)
    assert result.returncode == 0, result.stderr
    assert rig.token_at_enable == "HERMES_KERNEL_TOKEN=abc"


def test_quoted_token_with_comment_after_the_quote(rig):
    result = env_with(rig, 'NEO4J_PASSWORD=pw\nKT_TOKEN="abc" # q\n' + EXAMPLE_BLANKS)
    assert result.returncode == 0, result.stderr
    assert rig.token_at_enable == "HERMES_KERNEL_TOKEN=abc"


def test_last_duplicate_wins_and_a_blank_one_is_filled_in_place(rig):
    result = env_with(rig, "NEO4J_PASSWORD=pw\nKT_TOKEN=first\nKT_TOKEN=\n" + EXAMPLE_BLANKS)
    assert result.returncode == 0, result.stderr
    lines = (rig.root / ".env").read_text().splitlines()
    assert lines[1] == "KT_TOKEN=first"
    assert lines.count("KT_TOKEN=") == 0
    assert sum(line.startswith("KT_TOKEN=") for line in lines) == 2
    assert lines[2] == f"KT_TOKEN={rig.token_at_enable.split('=', 1)[1]}"


def test_blank_export_line_is_replaced_not_appended(rig):
    result = env_with(rig, "export NEO4J_PASSWORD=\nKT_TOKEN=t\n" + EXAMPLE_BLANKS)
    assert result.returncode == 0, result.stderr
    text = (rig.root / ".env").read_text()
    assert text.count("NEO4J_PASSWORD") == 1
    assert parse_env(rig.root / ".env")["NEO4J_PASSWORD"]


def test_version_gate_is_exact(rig):
    for version in ("v0.23.0+45.gabc", "v0.23.0-rc1", "v0.23.0.1", "v0.23.01"):
        result = rig.run(env={**KEYS, "HK_HERMES_VERSION": f"Hermes Agent {version}"})
        assert result.returncode != 0, version
        assert not (rig.root / ".env").exists()
    ok = rig.run(env={**KEYS, "HK_HERMES_VERSION": "Hermes Agent v0.23.0"})
    assert ok.returncode == 0, ok.stderr


def test_git_file_counts_as_a_checkout(rig):
    (rig.root / ".git").write_text("gitdir: /elsewhere\n")
    result = rig.run(env=KEYS)
    assert result.returncode == 0, result.stderr
    assert f"hermes plugins install file://{rig.root}#plugin --enable" in rig.log


def test_git_directory_counts_as_a_checkout(rig):
    (rig.root / ".git").mkdir()
    result = rig.run(env=KEYS)
    assert result.returncode == 0, result.stderr
    assert f"hermes plugins install file://{rig.root}#plugin --enable" in rig.log


def test_failed_mv_is_an_error_and_leaves_no_temp_file(rig):
    (rig.tools / "mv").unlink()
    (rig.tools / "mv").write_text("#!/bin/sh\nexit 1\n")
    (rig.tools / "mv").chmod(0o755)
    result = rig.run(env=KEYS)
    assert result.returncode != 0
    assert "could not" in result.stderr
    assert "kept" not in result.stdout.replace("kept /", "")
    assert list(rig.root.glob(".hk.*")) == []


def test_interrupt_between_awk_and_mv_leaves_no_copy_of_env(rig):
    real_mv = shutil.which("mv")
    (rig.tools / "mv").unlink()
    (rig.tools / "mv").write_text(f'#!/bin/sh\nsleep 30\nexec {real_mv} "$@"\n')
    (rig.tools / "mv").chmod(0o755)
    proc = subprocess.Popen(
        [str(rig.root / "install.sh")],
        cwd=str(rig.root),
        env=rig.child_env({**KEYS, "HERMES_KERNEL_MODE": "local"}),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.time() + 20
        while not list(rig.root.glob(".hk.*")) and time.time() < deadline:
            time.sleep(0.05)
        assert list(rig.root.glob(".hk.*")), "the temp copy never appeared"
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert list(rig.root.glob(".hk.*")) == []


def test_gitignore_covers_the_temp_copies():
    ignore = (REPO / ".gitignore").read_text().splitlines()
    assert ".hk.*" in ignore
