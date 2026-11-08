import json
import re
import stat
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "bf-SENTINEL-key-0123456789"
URL = "https://kernel.staging-bonfires.dev"
KEY = {"HERMES_KERNEL_TOKEN": SENTINEL}


def status_body(service="0.1.0"):
    return json.dumps({"bonfire_id": "b", "founded": True, "versions": {"service": service, "mk": "2.0.1"}})


def detail(text):
    return json.dumps({"detail": text})


def hosted(rig, *args, env=None, input=None):
    return rig.run("--mode", "hosted", *args, env={**KEY, **(env or {})}, input=input)


def untouched(rig):
    assert list(rig.home.iterdir()) == []
    assert not any(" plugins " in f" {line} " for line in rig.log if line.startswith("hermes"))
    assert not (rig.root / ".env").exists()


def test_first_question_offers_both(rig):
    result = rig.run(ask=True, input=f"hosted\n{SENTINEL}\n")
    assert result.returncode == 0, result.stderr
    assert "hosted" in result.stderr and "local" in result.stderr
    assert not any(line.startswith("docker") for line in rig.log)
    assert (rig.home / "hermes-kernel.json").exists()


def test_first_question_takes_short_and_any_case_and_asks_again(rig):
    result = rig.run(ask=True, env=KEY, input=f"maybe\nH\n{SENTINEL}\n")
    assert result.returncode == 0, result.stderr
    assert "please answer" in result.stdout


def test_no_mode_and_nothing_to_ask_names_the_flag(rig):
    result = rig.run(ask=True)
    assert result.returncode != 0
    assert "--mode" in result.stderr
    untouched(rig)


def test_bad_key_fails_before_any_write(rig):
    result = hosted(rig, env={"HK_CURL_CODE": "401", "HK_CURL_BODY": detail("invalid token")})
    assert result.returncode != 0
    assert "refused this key" in result.stderr
    assert SENTINEL not in result.stderr + result.stdout
    untouched(rig)


def test_read_only_key_says_so(rig):
    body = detail("this key is read-only: it needs write permission")
    result = hosted(rig, env={"HK_CURL_CODE": "403", "HK_CURL_BODY": body})
    assert result.returncode != 0
    assert "this key is read-only: it needs write permission" in result.stderr
    untouched(rig)


def test_wrong_kind_of_bonfire_stops(rig):
    body = detail("this key's bonfire is not set up for hermes-kernel")
    result = hosted(rig, env={"HK_CURL_CODE": "403", "HK_CURL_BODY": body})
    assert result.returncode != 0
    assert "this key's bonfire is not set up for hermes-kernel" in result.stderr
    untouched(rig)


def test_service_away_says_so_and_writes_nothing(rig):
    result = hosted(rig, env={"HK_CURL_CODE": "000", "HK_CURL_BODY": ""})
    assert result.returncode != 0
    assert "cannot be reached" in result.stderr and "try again" in result.stderr.lower()
    untouched(rig)


def test_a_503_says_try_again_not_bad_key(rig):
    body = detail("the key check did not answer")
    result = hosted(rig, env={"HK_CURL_CODE": "503", "HK_CURL_BODY": body})
    assert result.returncode != 0
    assert "try again" in result.stderr.lower()
    assert "was not refused" in result.stderr
    assert "refused this key" not in result.stderr
    untouched(rig)


def test_incompatible_service_stops(rig):
    result = hosted(rig, env={"HK_CURL_BODY": status_body("0.2.0")})
    assert result.returncode != 0
    assert "0.2.0" in result.stderr and "0.1" in result.stderr
    untouched(rig)


def test_unknown_service_version_is_incompatible(rig):
    result = hosted(rig, env={"HK_CURL_BODY": status_body("unknown")})
    assert result.returncode != 0
    untouched(rig)


def test_a_patch_release_is_compatible(rig):
    assert hosted(rig, env={"HK_CURL_BODY": status_body("0.1.7")}).returncode == 0


def test_hosted_needs_no_docker(rig):
    rig.remove_fake("docker")
    result = hosted(rig)
    assert result.returncode == 0, result.stderr
    assert not (rig.root / ".env").exists()
    assert not any(line.startswith("docker") for line in rig.log)
    assert "/kernel" in result.stdout


def test_hosted_writes_key_and_url(rig):
    result = hosted(rig)
    assert result.returncode == 0, result.stderr
    env_file = rig.home / ".env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text().splitlines() == [f"HERMES_KERNEL_TOKEN={SENTINEL}"]
    assert json.loads((rig.home / "hermes-kernel.json").read_text()) == {"url": URL}
    assert rig.token_at_enable == f"HERMES_KERNEL_TOKEN={SENTINEL}"
    assert "hermes config set memory.provider hermes-kernel" in rig.log
    assert rig.log[-1] == "hermes plugins enable hermes-kernel"


def test_url_flag_overrides_and_is_asked(rig):
    result = hosted(rig, "--url", "https://kernel.example.test/")
    assert result.returncode == 0, result.stderr
    assert json.loads((rig.home / "hermes-kernel.json").read_text()) == {"url": "https://kernel.example.test"}


def test_plain_http_is_refused_except_for_this_machine(rig):
    result = hosted(rig, "--url", "http://kernel.example.test")
    assert result.returncode != 0
    assert "https" in result.stderr
    untouched(rig)
    assert not any(line.startswith("curl") for line in rig.log)
    for ok in ("http://localhost:8787", "http://127.0.0.1:8787"):
        assert hosted(rig, "--url", ok).returncode == 0
    assert hosted(rig, "--url", "http://localhost.evil.test").returncode != 0


def test_hosted_keeps_every_other_line_of_hermes_env(rig):
    original = "OPENAI_API_KEY=x\n# a comment\nexport HERMES_KERNEL_TOKEN=old\nexport OTHER=1\nLAST=2"  # no newline
    (rig.home / ".env").write_text(original)
    result = hosted(rig)
    assert result.returncode == 0, result.stderr
    lines = (rig.home / ".env").read_text().splitlines()
    assert lines == ["OPENAI_API_KEY=x", "# a comment", f"HERMES_KERNEL_TOKEN={SENTINEL}", "export OTHER=1", "LAST=2"]


def test_the_key_is_never_on_a_command_line(rig):
    result = hosted(rig)
    assert result.returncode == 0, result.stderr
    assert SENTINEL not in "\n".join(rig.log)
    curl_lines = [line for line in rig.log if line.startswith("curl")]
    assert len(curl_lines) == 1
    config = (rig.base / "curl_config").read_text()
    assert f"Authorization: Bearer {SENTINEL}" in config
    assert (rig.base / "curl_config_mode").read_text().strip() == "-rw-------"
    assert SENTINEL not in result.stdout + result.stderr


def test_the_key_can_be_typed(rig):
    result = rig.run("--mode", "hosted", input=f"{SENTINEL}\n")
    assert result.returncode == 0, result.stderr
    assert (rig.home / ".env").read_text().strip() == f"HERMES_KERNEL_TOKEN={SENTINEL}"
    assert SENTINEL not in result.stdout + result.stderr


def test_no_key_stops(rig):
    result = rig.run("--mode", "hosted")
    assert result.returncode != 0
    assert "HERMES_KERNEL_TOKEN" in result.stderr
    untouched(rig)


def test_hosted_checks_hermes_first(rig):
    result = hosted(rig, env={"HK_HERMES_VERSION": "Hermes Agent v0.24.0 (test)"})
    assert result.returncode != 0
    assert "v0.23.0" in result.stderr
    assert not any(line.startswith("curl") for line in rig.log)


def test_runs_on_a_mac(rig):
    assert hosted(rig, env={"HK_UNAME": "Darwin arm64"}).returncode == 0
    local = rig.run(
        "--mode", "local", env={"HK_UNAME": "Darwin arm64", "OPENROUTER_API_KEY": "a", "VOYAGE_API_KEY": "b"}
    )
    assert local.returncode != 0
    assert "Linux x86_64" in local.stderr


def test_the_plugin_version_is_what_the_script_compares():
    text = (REPO / "plugin" / "plugin.yaml").read_text()
    assert re.search(r'^version: "0\.1"$', text, re.M)


def test_no_gnu_or_bash4_only_constructs():
    text = (REPO / "install.sh").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("mapfile", "readarray", "declare -A", ",,}", "^^}", "realpath -m", "sed -i", "${!", "&>>", "|&"):
        assert banned not in code, banned


LOCAL_KEYS = {"OPENROUTER_API_KEY": "a", "VOYAGE_API_KEY": "b"}


def test_answering_local_or_l_goes_local(rig):
    for answer in ("local", "l"):
        result = rig.run(ask=True, env=LOCAL_KEYS, input=f"{answer}\n")
        assert result.returncode == 0, result.stderr
        assert any(line.startswith("docker") for line in rig.log)
        assert (rig.home / "hermes-kernel.json").read_text().strip() == '{"url": "http://127.0.0.1:8787"}'


def test_mode_flag_ignores_case(rig):
    result = rig.run("--mode", "HOSTED", env=KEY)
    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("docker") for line in rig.log)


def test_userinfo_and_lookalike_hosts_cannot_pass_the_http_guard(rig):
    for bad in (
        "http://localhost:80@evil.example/",
        "http://127.0.0.1:1@evil.example",
        "https://a@kernel.example",
        "http://localhost.evil.example",
        "http://127.0.0.1.evil.example",
        "http://localhost:80x/",
        "http://localhost@evil.example",
    ):
        result = hosted(rig, "--url", bad)
        assert result.returncode != 0, bad
    assert not any(line.startswith("curl") for line in rig.log)
    untouched(rig)
    assert hosted(rig, "--url", "http://localhost:8020/").returncode == 0


def test_every_sed_without_dash_E_is_basic_regex_safe():
    text = (REPO / "install.sh").read_text()
    for line in text.splitlines():
        if line.lstrip().startswith("#") or "sed " not in line:
            continue
        if re.search(r"sed (-[a-zA-Z]+ )*-[a-zA-Z]*E", line):
            continue
        for banned in ("\\|", "\\+", "\\?", "\\s"):
            assert banned not in line.replace("\\\\", ""), (banned, line)


def test_403_detail_reaches_the_person_with_json_escapes_in_it(rig):
    body = detail("this key is read-only: it needs write permission")
    result = hosted(rig, env={"HK_CURL_CODE": "403", "HK_CURL_BODY": body})
    assert "needs write permission" in result.stderr


def test_the_curl_config_is_gone_before_anything_is_installed(rig):
    result = hosted(rig)
    assert result.returncode == 0, result.stderr
    assert not (rig.base / "config_at_install").exists()
    assert list((rig.base / "tmp").iterdir()) == []


def test_unreachable_says_why(rig):
    err = "curl: (6) Could not resolve host: kernel.example.test"
    result = hosted(rig, env={"HK_CURL_CODE": "000", "HK_CURL_BODY": "", "HK_CURL_ERR": err})
    assert result.returncode != 0
    assert "Could not resolve host" in result.stderr
    assert SENTINEL not in result.stderr


def test_control_characters_in_a_detail_are_stripped(rig):
    body = '{"detail": "bad\x1b[31m red\x07 text"}'
    result = hosted(rig, env={"HK_CURL_CODE": "403", "HK_CURL_BODY": body})
    assert result.returncode != 0
    assert "bad[31m red text" in result.stderr
    assert "\x1b" not in result.stderr and "\x07" not in result.stderr


def test_a_pasted_key_is_trimmed(rig):
    result = rig.run("--mode", "hosted", input=f"   {SENTINEL}\t \n")
    assert result.returncode == 0, result.stderr
    assert (rig.home / ".env").read_text().strip() == f"HERMES_KERNEL_TOKEN={SENTINEL}"
    result = hosted(rig, env={"HERMES_KERNEL_TOKEN": f"  {SENTINEL} "})
    assert result.returncode == 0, result.stderr
    assert f'Bearer {SENTINEL}"\n' in (rig.base / "curl_config").read_text()
