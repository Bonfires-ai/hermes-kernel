"""Phrases: the runner and the tools the model calls."""

from types import SimpleNamespace
from typing import Any, cast

import pytest

from plugin import shared
from plugin.client import Refused, Unavailable
from plugin.guide import Editor, GuideData
from plugin.ledger import StepLedger
from plugin.runs import PhraseRun
from plugin.tools import KernelTools
from plugin.turns import LocalTurn

RESULT = "Sourdough starter notes: feed 1:1:1 daily; rye starters are forgiving."


def step(n, tool="web_search", args=None):
    args = {"query": "sourdough"} if args is None else args
    return {"step_id": f"s{n}", "tool": tool, "args": args, "effect_key": f"k{n}", "needs_confirmation": False}


def view(pending=None, status=None):
    return {"status": status or ("licensed" if pending else "done"), "pending": pending, "repair": None}


class Client:
    def __init__(self, *views):
        self.views = list(views)
        self.reports = []
        self.called = []
        self.taught = []
        self.call_result = {"kind": "turn", "turn_id": "c1", "step": step(1)}
        self.report_errors = []
        self.teach_error: Exception | None = None

    def calls(self, turn_id, tool, args, *, timeout=30.0):
        self.called.append((turn_id, tool, args))
        return self.call_result

    def get(self, turn_id, *, timeout=30.0):
        return self.views.pop(0)

    def report(self, turn_id, step_id, verdict, value=None, detail=None, *, timeout=30.0):
        if self.report_errors:
            raise self.report_errors.pop(0)
        self.reports.append((step_id, verdict, value, detail))
        return {}

    def teach(self, body, *, timeout=30.0):
        if self.teach_error:
            raise self.teach_error
        self.taught.append(body)
        return {"draft_id": "d1", "read_back": "ok", "problems": []}

    def offer(self, turn_id, body, *, timeout=30.0):
        return {}

    def confirm(self, draft_id, body, *, timeout=30.0):
        return {"phrases": ["research"]}


class Source:
    def __init__(self):
        self.enabled = True
        self.changing = set()
        self.check_error: Exception | None = None
        self.platform = ""

    def tool(self, name):
        return SimpleNamespace(name=name) if name in ("web_search", "read_file") else None

    def real_name(self, name):
        return name

    def enabled_for(self, platform, name):
        self.platform = platform
        return self.enabled

    def side_effecting(self, names):
        if self.check_error:
            raise self.check_error
        return {n for n in names if n in self.changing}

    def steps(self):
        return []


class Turns:
    def __init__(self, local):
        self.local = local
        self.named = []

    def current(self, session_id):
        return self.local

    def name_phrase(self, child, phrase):
        self.named.append((child, phrase))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    shared.reset_shared()
    sh = shared.get_shared()
    sh.sessions["s1"] = {"platform": "Telegram"}
    yield sh
    shared.reset_shared()


def build(client: Any, *, dispatch: Any = None, source: Any = None, local: Any = None, read: bool = True):
    source = source or Source()
    calls = []

    def run(name, args, **kw):
        calls.append((name, args))
        return dispatch(name, args) if dispatch else RESULT

    local = local or LocalTurn("h1", "s1", "telegram", "g", "g", "Ana", turn_id="m1")
    if local.affordances is None:
        local.affordances = ["research", "web_search", "teach_phrase"]
        local.native = {"web_search"}
    runs = PhraseRun(client, source, run, StepLedger(None))
    tools = KernelTools(
        client, cast(Any, Turns(local if read else None)), runs, None, source, Editor(source, GuideData())
    )
    return tools, calls, local, source


def ask(tools, name, args, **kw):
    return getattr(tools, name)(args, session_id="s1", task_id="t1", **kw)


def test_run_phrase_runs_its_steps_through_dispatch_and_reports(env):
    client = Client(view(step(2, "read_file", {"path": "a"})), view())
    tools, calls, _, _ = build(client)
    answer = ask(tools, "run_phrase", {"phrase": "research", "args": {"x": "bread"}})
    assert client.called == [("m1", "research", {"x": "bread"})]
    assert calls == [("web_search", {"query": "sourdough"}), ("read_file", {"path": "a"})]
    assert [r[:2] for r in client.reports] == [("s1", "committed"), ("s2", "committed")]
    assert client.reports[0][2] == RESULT  # the service gets the raw text
    assert answer.index("<untrusted_tool_result") < answer.index(RESULT)  # the model gets it fenced


def test_run_phrase_refuses_a_plain_tool(env):
    client = Client()
    tools, calls, _, _ = build(client)
    answer = ask(tools, "run_phrase", {"phrase": "web_search", "args": {"query": "x"}})
    assert "call it directly" in answer
    assert client.called == [] and calls == []


def test_a_think_step_pauses_and_continue_resumes(env):
    client = Client(view(step(2, "think", {"instruction": "pick one", "input": RESULT})), view())
    tools, calls, local, _ = build(client)
    answer = ask(tools, "run_phrase", {"phrase": "research", "args": {"x": "bread"}})
    assert "paused at a think step" in answer and "pick one" in answer
    assert "<untrusted_tool_result" in answer.split("Its input")[1]  # the think input is fenced
    assert "c1" in local.waiting
    done = ask(tools, "phrase_continue", {"text": "the rye one"})
    assert ("s2", "committed", "the rye one", None) in client.reports
    assert "went on" in done and calls == [("web_search", {"query": "sourdough"})]
    assert "Nothing is waiting" in ask(tools, "phrase_continue", {"text": "again"})


def test_a_gone_tool_stops_with_a_sentence_and_reports_nothing_committed(env):
    client = Client()
    client.call_result = {"kind": "turn", "turn_id": "c1", "step": step(1, "gone_tool")}
    tools, calls, _, _ = build(client)
    answer = ask(tools, "run_phrase", {"phrase": "research"})
    assert "gone_tool is not one of my tools" in answer and "nothing after it ran" in answer
    assert [r[1] for r in client.reports] == ["tool_error"] and calls == []


def test_a_step_on_a_tool_not_enabled_here_does_not_run(env):
    client = Client()
    tools, calls, _, source = build(client)
    source.enabled = False
    answer = ask(tools, "run_phrase", {"phrase": "research"})
    assert calls == [] and "not enabled on telegram" in answer
    assert [r[1] for r in client.reports] == ["tool_error"]


def test_a_lost_report_does_not_run_the_step_twice(env):
    client = Client(view(step(1)), view())
    client.report_errors = [Unavailable("gone")]
    tools, calls, _, _ = build(client)
    first = ask(tools, "run_phrase", {"phrase": "research"})
    assert "did not take its result" in first and len(calls) == 1
    second = ask(tools, "phrase_continue", {"text": ""})
    assert len(calls) == 1  # held in the ledger: reported, not dispatched again
    assert [r[:3] for r in client.reports] == [("s1", "committed", RESULT)]
    assert "went on" in second


def test_a_write_tool_in_a_taught_body_is_refused_before_sending(env):
    client = Client()
    tools, _, _, source = build(client)
    source.changing = {"write_file"}
    body = [{"call": "web_search", "args": {"query": "{X}"}}, {"call": "write_file", "args": {"path": "it"}}]
    answer = ask(tools, "teach_phrase", {"forms": ["research {X}"], "body": body})
    assert "write_file" in answer and "read-only in this version" in answer
    assert client.taught == []


def test_a_failed_check_of_a_steps_tool_refuses_too(env):
    client = Client()
    tools, _, _, source = build(client)
    source.check_error = RuntimeError("registry")
    answer = ask(tools, "teach_phrase", {"forms": ["go {X}"], "body": [{"call": "web_search", "args": {"query": "x"}}]})
    assert "couldn't check that step's tool, so nothing was sent" in answer
    assert client.taught == []


def test_teaching_before_the_first_conversation_says_so(env):
    client = Client()
    client.teach_error = Refused(409, "teaching starts after the first conversation")
    tools, _, _, _ = build(client)
    answer = ask(tools, "teach_phrase", {"forms": ["go {X}"], "body": ["look up {X}"]})
    assert answer == "teaching starts after the first conversation"


def test_every_tool_answers_when_the_service_is_away(env):
    args = {
        "run_phrase": {"phrase": "research"},
        "offer_phrase": {"forms": ["a"], "body": ["b"]},
        "teach_phrase": {"forms": ["a"], "body": ["b"]},
        "confirm_phrase": {"draft_id": "d1"},
        "phrase_continue": {"text": "x"},
    }
    away = Client()
    for client in (None, away):
        tools, _, _, _ = build(client)
        if client is away:
            away.calls = away.teach = away.offer = away.confirm = lambda *a, **k: (_ for _ in ()).throw(
                Unavailable("x")
            )
            away.get = away.report = away.calls
        for name, given in args.items():
            assert isinstance(ask(tools, name, given), str)
    tools, _, _, _ = build(Client(), read=False)  # no reading of the message at all
    for name, given in args.items():
        assert isinstance(ask(tools, name, given), str)
