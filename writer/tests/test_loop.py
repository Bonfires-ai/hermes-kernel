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
KEY = "or-very-secret-value"


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
                "_id": f"bf\x1f{sid}-{seq}",
                "bonfire_id": "bf",
                "message_id": f"{sid}-{seq}",
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
    assert run_once(Queue(db, "bf"), Script(BuildResult("built")), Release(), settings)
    session = state(db, "A")
    assert session["state"] == "built" and session["built_at"]
    docs = list(db.messages.find({"session_id": "A"}))
    assert len(docs) == 2 and all("text" not in d for d in docs)


def test_a_stop_retries_then_fails_and_releases(db, settings):
    add_session(db, "A", 1)
    add_session(db, "B", 2)
    stop = BuildResult("stopped", "stopped before seal")
    build, release = Script(stop, stop, stop, BuildResult("built")), Release()
    now = [T0]
    queue = Queue(db, "bf", clock=lambda: now[0])
    run_once(queue, build, release, settings)
    assert state(db, "A")["attempts"] == 1 and state(db, "A")["state"] == "closed"
    now[0] += timedelta(seconds=60)
    run_once(queue, build, release, settings)
    assert state(db, "A")["attempts"] == 2
    now[0] += timedelta(seconds=120)
    run_once(queue, build, release, settings)
    assert state(db, "A")["state"] == "failed" and release.calls == 1
    assert db.messages.find_one({"session_id": "A"})["text"]  # the text stays
    run_once(queue, build, release, settings)
    assert build.seen == ["A", "A", "A", "B"] and state(db, "B")["state"] == "built"


def test_a_stop_waits_out_its_delay_before_the_next_try(db, settings):
    add_session(db, "A", 1)
    stop = BuildResult("stopped", "x")
    build = Script(stop, stop, BuildResult("built"))
    now = [T0]
    queue = Queue(db, "bf", clock=lambda: now[0])
    assert run_once(queue, build, Release(), settings)  # the stop itself is work done
    assert state(db, "A")["retry_after"].replace(tzinfo=UTC) == T0 + timedelta(seconds=60)
    now[0] = T0 + timedelta(seconds=59)
    assert not run_once(queue, build, Release(), settings) and build.seen == ["A"]
    now[0] = T0 + timedelta(seconds=60)
    assert run_once(queue, build, Release(), settings) and build.seen == ["A", "A"]
    assert state(db, "A")["retry_after"].replace(tzinfo=UTC) == T0 + timedelta(seconds=60 + 120)  # doubled
    now[0] = T0 + timedelta(seconds=60 + 119)
    assert not run_once(queue, build, Release(), settings)
    now[0] = T0 + timedelta(seconds=60 + 120)
    assert run_once(queue, build, Release(), settings) and state(db, "A")["state"] == "built"


def test_while_a_stop_waits_no_other_session_is_claimed(db, settings):
    add_session(db, "A", 1)
    add_session(db, "B", 2)
    build = Script(BuildResult("stopped", "x"), BuildResult("built"), BuildResult("built"))
    now = [T0]
    queue = Queue(db, "bf", clock=lambda: now[0])
    run_once(queue, build, Release(), settings)
    now[0] = T0 + timedelta(seconds=30)
    assert queue.claim() is None and state(db, "B")["state"] == "closed"
    now[0] = T0 + timedelta(seconds=61)
    run_once(queue, build, Release(), settings)
    run_once(queue, build, Release(), settings)
    assert build.seen == ["A", "A", "B"]


def test_a_busy_or_held_result_sets_no_delay(db, settings):
    add_session(db, "A", 1)
    run_once(Queue(db, "bf"), Script(BuildResult("busy", "leased")), Release(), settings)
    assert "retry_after" not in state(db, "A")


def test_a_failed_release_still_fails_the_session_and_hides_keys(db, settings):
    add_session(db, "A", 1)
    stop = BuildResult("stopped", f"died with {KEY}")
    build = Script(stop, stop, stop)
    now = [T0]
    queue = Queue(db, "bf", clock=lambda: now[0])
    for _ in range(3):
        run_once(queue, build, Release(fail=True), settings)
        now[0] += timedelta(seconds=300)
    session = state(db, "A")
    assert session["state"] == "failed" and "lease is live" in session["reason"]
    assert KEY not in session["reason"]


def test_a_stopped_session_goes_first(db, settings):
    add_session(db, "A", 1)
    add_session(db, "B", 2)
    build = Script(BuildResult("stopped", "x"), BuildResult("built"), BuildResult("built"))
    now = [T0]
    queue = Queue(db, "bf", clock=lambda: now[0])
    run_once(queue, build, Release(), settings)
    now[0] += timedelta(seconds=60)
    run_once(queue, build, Release(), settings)
    assert build.seen == ["A", "A"]


def test_past_the_ceiling_waits(db, settings):
    add_session(db, "A", 1)
    run_once(Queue(db, "bf"), Script(BuildResult("held", "bonfire spend ceiling reached")), Release(), settings)
    assert state(db, "A")["state"] == "held" and state(db, "A")["attempts"] == 0


def test_held_sessions_are_tried_again(db):
    add_session(db, "A", 1)
    now = [T0]
    queue = Queue(db, "bf", clock=lambda: now[0])
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
    queue = Queue(db, "bf")
    run_once(queue, build, release, settings)
    run_once(queue, build, release, settings)
    assert state(db, "A")["state"] == "failed" and release.calls == 0 and state(db, "B")["state"] == "built"


def test_restart_puts_running_back(db):
    add_session(db, "A", 1, state="running")
    db.sessions.update_one({"_id": "A"}, {"$set": {"attempts": 2}})
    Queue(db, "bf").reset_running()
    assert state(db, "A")["state"] == "closed" and state(db, "A")["attempts"] == 2


def test_messages_are_read_by_timestamp_then_seq_and_blank_ones_skipped(db):
    add_session(db, "A", 1, messages=0)
    for _id, seq, sec, text in [("m1", 5, 2, "last"), ("m2", 5, 1, "first"), ("m3", 1, 3, None)]:
        doc = {
            "_id": f"bf\x1f{_id}",
            "message_id": _id,
            "session_id": "A",
            "seq": seq,
            "speaker": "me",
            "timestamp": T0 + timedelta(seconds=sec),
        }
        db.messages.insert_one({**doc, "text": text} if text else doc)
    claimed = Queue(db, "bf").claim()
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


# --- fix round 1 ---------------------------------------------------------------------------------------------------


def _claimed() -> Claimed:
    return {
        "id": "A",
        "conversation_id": "c",
        "agent_handles": [],
        "messages": [
            {"message_id": "m1", "speaker": "me", "text": "hi", "timestamp": "2026-11-01T00:00:00+00:00"},
            {"message_id": "m2", "speaker": "me", "text": "BAD", "timestamp": "2026-11-01T00:00:01+00:00"},
            {"message_id": "m3", "speaker": "me", "text": "ok", "timestamp": "2026-11-01T00:00:02+00:00"},
        ],
    }


async def _parse(text: str) -> list[dict[str, str]]:
    if "BAD" in text:
        raise ValueError("unreadable")
    return [{"tree": text}]


def _api(append_session: Any, exists: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        bonfire_exists=lambda bonfire_id, overrides: exists,
        SessionInput=lambda **kw: SimpleNamespace(**kw),
        append_session=append_session,
    )


def _raising(name: str, text: str, *bases: type[Exception]) -> Exception:
    return type(name, bases or (Exception,), {})(text)


def _boom(exc: Exception) -> Any:
    def append_session(*args: Any, **kw: Any) -> Any:
        raise exc

    return append_session


def test_a_stopped_result_carries_mks_stage_and_reason(settings):
    out = {"status": "stopped", "result": {"stage": "capture", "reason": f"syntax failed {KEY}"}}
    api = _api(lambda *a, **kw: out)
    result = build_session(settings, _claimed(), api=api, ops=object(), parse=None)
    assert result.kind == "stopped" and result.reason
    assert "capture" in result.reason and "syntax failed" in result.reason and KEY not in result.reason


def test_a_claim_survives_a_bad_message_and_a_failed_read(db):
    add_session(db, "A", 1, messages=0)
    db.messages.insert_one(
        {
            "_id": "bf\x1fok",
            "message_id": "ok",
            "session_id": "A",
            "seq": 0,
            "speaker": "me",
            "text": "t",
            "timestamp": T0,
        }
    )
    db.messages.insert_one(
        {
            "_id": "bf\x1fno-speaker",
            "message_id": "no-speaker",
            "session_id": "A",
            "seq": 1,
            "text": "t",
            "timestamp": T0,
        }
    )
    claimed = Queue(db, "bf").claim()
    assert claimed is not None and [m["message_id"] for m in claimed["messages"]] == ["ok"]

    add_session(db, "B", 2)
    queue = Queue(db, "bf")

    def broken(*a: Any, **kw: Any) -> Any:
        raise OSError("mongo went away")

    queue._messages = SimpleNamespace(find=broken)  # pyright: ignore[reportAttributeAccessIssue]
    db.sessions.update_one({"_id": "A"}, {"$set": {"state": "built"}})
    with pytest.raises(OSError):
        queue.claim()
    assert state(db, "B")["state"] == "closed"  # not left running


def test_nothing_readable_fails_and_a_partial_drop_is_noted(db, settings):
    api = _api(lambda *a, **kw: {"status": "appended"})
    partial = build_session(settings, _claimed(), api=api, ops=object(), parse=_parse)
    assert partial.kind == "built" and partial.reason == "dropped 1 of 3 messages"

    allbad = _claimed()
    allbad["messages"] = [{**m, "text": "BAD"} for m in allbad["messages"]]
    nothing = build_session(settings, allbad, api=api, ops=object(), parse=_parse)
    assert nothing.kind == "refused" and nothing.reason == "nothing readable after cleaning: 3 messages dropped"
    mk_empty = build_session(
        settings, _claimed(), api=_api(lambda *a, **kw: {"status": "empty"}), ops=object(), parse=None
    )
    assert mk_empty.kind == "refused"

    add_session(db, "A", 1)
    run_once(Queue(db, "bf"), Script(partial), Release(), settings)
    assert state(db, "A")["state"] == "built" and state(db, "A")["reason"] == "dropped 1 of 3 messages"


def test_a_pending_session_tries_release_once_per_interval(db, settings):
    add_session(db, "F", 1, state="failed")
    db.sessions.update_one({"_id": "F"}, {"$set": {"failed_at": T0}})
    add_session(db, "B", 2)
    now = [T0 + timedelta(hours=2)]
    queue = Queue(db, "bf", clock=lambda: now[0])
    pending = BuildResult("busy", "PendingSession: run 1 stopped", pending=True)
    build, release = Script(pending, pending, pending), Release()
    assert not run_once(queue, build, release, settings)
    assert not run_once(queue, build, release, settings)
    assert release.calls == 1
    assert state(db, "B")["state"] == "closed" and "F" in state(db, "B")["reason"]
    now[0] += timedelta(seconds=601)
    run_once(queue, build, release, settings)
    assert release.calls == 2 and state(db, "B")["attempts"] == 0


def test_a_missing_key_holds_and_a_stale_commit_is_busy(settings):
    bare = Settings(bonfire_id="bf", openrouter_api_key=None, voyage_api_key=None, neo4j_password=None)
    api = _api(lambda *a, **kw: {"status": "appended"}, exists=False)
    held = build_session(bare, _claimed(), api=api, ops=object(), parse=None)
    assert held.kind == "held" and held.reason and "OPENROUTER_API_KEY" in held.reason

    stale = _raising("StaleCommit", "the commit went stale", _raising("BonfireError", "").__class__)
    for exc in (stale, _raising("BonfireError", "could not take the lease on bonfire 'bf': it kept changing")):
        result = build_session(settings, _claimed(), api=_api(_boom(exc)), ops=object(), parse=None)
        assert result.kind == "busy"


def test_an_outage_counts_no_attempt(db, settings):
    for name in (
        "ServiceUnavailable",
        "SessionExpired",
        "AutoReconnect",
        "ServerSelectionTimeoutError",
        "ConnectionError",
    ):
        exc = _raising(name, "connection refused")
        result = build_session(settings, _claimed(), api=_api(_boom(exc)), ops=object(), parse=None)
        assert result.kind == "busy" and not result.pending and name in (result.reason or "")
    add_session(db, "A", 1)
    run_once(Queue(db, "bf"), Script(BuildResult("busy", "ServiceUnavailable: down")), Release(), settings)
    assert state(db, "A")["state"] == "closed" and state(db, "A")["attempts"] == 0


def test_tied_and_decreasing_timestamps_come_out_rising(db):
    # A crash between a message's insert and its count leaves an orphan, and the next message reuses its seq and can
    # carry an earlier time. mk refuses a session whose times do not rise, so it could never be built.
    add_session(db, "s1", 1, messages=0)
    db["messages"].insert_many(
        [
            {
                "_id": f"bf\x1fm{i}",
                "message_id": f"m{i}",
                "session_id": "s1",
                "seq": i,
                "speaker": "a",
                "text": str(i),
                "timestamp": T0 + timedelta(seconds=at),
            }
            for i, at in enumerate((0, 0, 0, 5, 3))
        ]
    )
    claimed = Queue(db, "bf").claim()
    assert claimed is not None
    stamps = [datetime.fromisoformat(m["timestamp"]) for m in claimed["messages"]]
    assert [m["message_id"] for m in claimed["messages"]] == ["m0", "m1", "m2", "m4", "m3"]  # by time, then seq
    milli = timedelta(milliseconds=1)
    assert stamps == [T0, T0 + milli, T0 + 2 * milli, T0 + timedelta(seconds=3), T0 + timedelta(seconds=5)]
    assert all(later > earlier for earlier, later in zip(stamps, stamps[1:], strict=False))
    assert (
        db["messages"].find_one({"message_id": "m1"})["timestamp"].replace(tzinfo=UTC) == T0
    )  # the store keeps what was sent


def test_claim_hands_the_plain_message_id(db) -> None:
    add_session(db, "A", 0, messages=0)
    db.messages.insert_one(
        {
            "_id": "bf\x1fm1",
            "bonfire_id": "bf",
            "message_id": "m1",
            "session_id": "A",
            "seq": 1,
            "speaker": "me",
            "text": "t",
            "timestamp": T0,
        }
    )
    claimed = Queue(db, "bf").claim()
    assert claimed is not None and [m["message_id"] for m in claimed["messages"]] == ["m1"]


def test_the_writer_never_touches_another_bonfires_sessions(db) -> None:
    add_session(db, "theirs", 0)
    add_session(db, "theirs-running", 1, state="running")
    add_session(db, "theirs-failed", 2, state="failed")
    add_session(db, "theirs-held", 3, state="held")
    db.sessions.update_many({}, {"$set": {"bonfire_id": "other"}})
    db.sessions.update_one({"_id": "theirs-held"}, {"$set": {"held_at": T0}})
    db.sessions.update_one({"_id": "theirs"}, {"$set": {"retry_after": T0 + timedelta(hours=1)}})
    before = list(db.sessions.find({}).sort("_id"))
    add_session(db, "mine", 4)
    queue = Queue(db, "bf", clock=lambda: T0)
    assert queue.reset_running() == 0
    assert queue.release_held(0) == 0
    assert queue.last_failed() is None
    claimed = queue.claim()  # the oldest closed session is theirs, and it is waiting out a delay: neither stops this
    assert claimed is not None and claimed["id"] == "mine"
    assert list(db.sessions.find({"bonfire_id": "other"}).sort("_id")) == before
