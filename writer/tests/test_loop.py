from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import mongomock
import pytest
from pydantic import SecretStr

from hermes_writer.build import OWNER, BuildResult, build_session
from hermes_writer.config import Settings
from hermes_writer.loop import run_once
from hermes_writer.queue import Claimed, Queue

T0 = datetime(2026, 11, 1, tzinfo=UTC)
KEY = "sk-or-very-secret-value"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bonfire_id="bf",
        openrouter_api_key=SecretStr(KEY),
        voyage_api_key=SecretStr("voyage-very-secret-value"),
        neo4j_password=SecretStr("neo4j-very-secret-value"),
        max_tries=3,
    )


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def add_session(db, sid: str, n: int, *, state: str = "closed", messages: int = 2) -> None:
    db.sessions.insert_one(
        {
            "_id": sid,
            "bonfire_id": "bf",
            "conversation_id": f"chat-{sid}",
            "state": state,
            "agent_handles": ["hermes"],
            "closed_at": T0 + timedelta(minutes=n),
            "attempts": 0,
        }
    )
    for seq in range(messages):
        db.messages.insert_one(
            {
                "_id": f"{sid}-{seq}",
                "session_id": sid,
                "seq": seq,
                "speaker": "me",
                "text": f"text {sid} {seq}",
                "timestamp": T0 + timedelta(minutes=n, seconds=seq),
            }
        )


class Script:
    """A fake build: answers from a list and records which session it was asked about."""

    def __init__(self, *results: BuildResult) -> None:
        self.results = list(results)
        self.seen: list[str] = []

    def __call__(self, claimed) -> BuildResult:
        self.seen.append(claimed["id"])
        return self.results.pop(0)


class Release:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def __call__(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("the lease is live")


def state(db, sid: str) -> dict[str, Any]:
    return db.sessions.find_one({"_id": sid})


def test_a_built_session_deletes_its_text(db, settings):
    add_session(db, "A", 1)
    assert run_once(Queue(db), Script(BuildResult("built")), Release(), settings)
    session = state(db, "A")
    assert session["state"] == "built" and session["built_at"]
    docs = list(db.messages.find({"session_id": "A"}))
    assert len(docs) == 2 and all("text" not in d for d in docs)


def test_a_stop_retries_then_fails_and_releases(db, settings):
    add_session(db, "A", 1)
    add_session(db, "B", 2)
    stop = BuildResult("stopped", "stopped before seal")
    build, release = Script(stop, stop, stop, BuildResult("built")), Release()
    queue = Queue(db)
    run_once(queue, build, release, settings)
    assert state(db, "A")["attempts"] == 1 and state(db, "A")["state"] == "closed"
    run_once(queue, build, release, settings)
    assert state(db, "A")["attempts"] == 2
    run_once(queue, build, release, settings)
    assert state(db, "A")["state"] == "failed" and release.calls == 1
    assert db.messages.find_one({"session_id": "A"})["text"]  # the text stays
    run_once(queue, build, release, settings)
    assert build.seen == ["A", "A", "A", "B"] and state(db, "B")["state"] == "built"


def test_a_failed_release_still_fails_the_session_and_hides_keys(db, settings):
    add_session(db, "A", 1)
    stop = BuildResult("stopped", f"died with {KEY}")
    build = Script(stop, stop, stop)
    queue = Queue(db)
    for _ in range(3):
        run_once(queue, build, Release(fail=True), settings)
    session = state(db, "A")
    assert session["state"] == "failed" and "lease is live" in session["reason"]
    assert KEY not in session["reason"]


def test_a_stopped_session_goes_first(db, settings):
    add_session(db, "A", 1)
    add_session(db, "B", 2)
    build = Script(BuildResult("stopped", "x"), BuildResult("built"), BuildResult("built"))
    queue = Queue(db)
    run_once(queue, build, Release(), settings)
    run_once(queue, build, Release(), settings)
    assert build.seen == ["A", "A"]


def test_past_the_ceiling_waits(db, settings):
    add_session(db, "A", 1)
    run_once(Queue(db), Script(BuildResult("held", "bonfire spend ceiling reached")), Release(), settings)
    assert state(db, "A")["state"] == "held" and state(db, "A")["attempts"] == 0


def test_held_sessions_are_tried_again(db):
    add_session(db, "A", 1)
    now = [T0]
    queue = Queue(db, clock=lambda: now[0])
    claimed = queue.claim()
    assert claimed is not None
    queue.held("A", "ceiling")
    now[0] = T0 + timedelta(seconds=601)
    assert queue.release_held(600) == 1
    assert state(db, "A")["state"] == "closed"


def test_a_refused_session_fails_at_once_and_does_not_release(db, settings):
    add_session(db, "A", 1)
    add_session(db, "B", 2)
    build, release = Script(BuildResult("refused", "malformed"), BuildResult("built")), Release()
    queue = Queue(db)
    run_once(queue, build, release, settings)
    run_once(queue, build, release, settings)
    assert state(db, "A")["state"] == "failed" and release.calls == 0 and state(db, "B")["state"] == "built"


def test_restart_puts_running_back(db):
    add_session(db, "A", 1, state="running")
    db.sessions.update_one({"_id": "A"}, {"$set": {"attempts": 2}})
    Queue(db).reset_running()
    assert state(db, "A")["state"] == "closed" and state(db, "A")["attempts"] == 2


def test_messages_are_read_by_timestamp_then_seq_and_blank_ones_skipped(db):
    add_session(db, "A", 1, messages=0)
    for _id, seq, sec, text in [("m1", 5, 2, "last"), ("m2", 5, 1, "first"), ("m3", 1, 3, None)]:
        doc = {"_id": _id, "session_id": "A", "seq": seq, "speaker": "me", "timestamp": T0 + timedelta(seconds=sec)}
        db.messages.insert_one({**doc, "text": text} if text else doc)
    claimed = Queue(db).claim()
    assert claimed is not None
    assert [m["message_id"] for m in claimed["messages"]] == ["m2", "m1"]


def test_every_append_uses_the_one_owner(settings):
    owners: list[str] = []

    def append_session(bonfire_id, session, **kw):
        owners.append(kw["owner"])
        return {"status": "appended"}

    api = SimpleNamespace(
        bonfire_exists=lambda bonfire_id, overrides: True,
        SessionInput=lambda **kw: SimpleNamespace(**kw),
        append_session=append_session,
    )
    claimed: Claimed = {
        "id": "A",
        "conversation_id": "c",
        "agent_handles": [],
        "messages": [{"message_id": "m", "speaker": "me", "text": "hi", "timestamp": "2026-11-01T00:00:00+00:00"}],
    }
    for _ in range(2):  # the second is a restarted writer
        fresh = settings.model_copy()
        result = build_session(fresh, claimed, api=api, ops=object(), parse=None)
        assert result.kind == "built"
    assert owners == [OWNER, OWNER] == ["hermes-writer"] * 2
