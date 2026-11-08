import logging
import stat
import threading
from datetime import UTC, datetime
from typing import Any

from plugin.capture import Capture, message_id
from plugin.client import Refused, Unauthorized, Unavailable

T0 = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.sent: list[dict[str, Any]] = []
        self.handles: list[list[str]] = []
        self.closed: list[str] = []
        self.calls: list[str] = []
        self.arrived = threading.Event()

    def post_messages(self, batch, agent_handles, *, timeout=30.0):
        if self.error:
            raise self.error
        self.sent.extend(batch)
        self.handles.append(list(agent_handles))
        self.calls.append("post")
        self.arrived.set()
        return {"accepted": len(batch), "duplicates": 0}

    def close_session(self, conversation_id, *, timeout=30.0):
        if self.error:
            raise self.error
        self.closed.append(conversation_id)
        self.calls.append("close")
        return {"closed": True}


def make(tmp_path, client=None, **kw):
    client = client or FakeClient()
    spool = tmp_path / "spool.jsonl"
    cap = Capture(client, spool, clock=lambda: T0, autostart=False, **kw)
    return cap, client, spool


def turn(cap, n=1, user="hello", reply="hi there"):
    cap.turn("chat-1", "Kira", user, reply, session_id="s1", turn_number=n)


def test_a_turn_is_two_messages_addressed_to_each_other(tmp_path):
    cap, client, _ = make(tmp_path)
    turn(cap)
    assert cap.drain_once()
    user, reply = client.sent
    assert set(user) == {"conversation_id", "message_id", "speaker", "text", "timestamp", "addressees"}
    assert (user["speaker"], user["text"], user["addressees"]) == ("Kira", "hello", ["hermes"])
    assert (reply["speaker"], reply["text"], reply["addressees"]) == ("hermes", "hi there", ["Kira"])
    assert user["conversation_id"] == reply["conversation_id"] == "chat-1"
    assert datetime.fromisoformat(user["timestamp"]) < datetime.fromisoformat(reply["timestamp"])
    assert datetime.fromisoformat(user["timestamp"]).tzinfo is not None
    assert client.handles == [["hermes"]]


def test_the_same_turn_twice_has_the_same_ids(tmp_path):
    cap, client, _ = make(tmp_path)
    turn(cap)
    turn(cap)
    cap.drain_once()
    ids = [m["message_id"] for m in client.sent]
    assert ids[:2] == ids[2:]
    assert len(set(ids)) == 2
    assert message_id("c", "s", 1, "user", "a") != message_id("c", "s", 1, "user", "b")
    assert len(message_id("c", "s", 1, "user", "a")) == 64


def test_an_empty_reply_sends_only_the_user_message(tmp_path):
    cap, client, _ = make(tmp_path)
    turn(cap, reply="")
    turn(cap, n=2, user="")
    turn(cap, n=3, user="", reply="")
    cap.drain_once()
    assert [(m["speaker"], m["text"]) for m in client.sent] == [("Kira", "hello"), ("hermes", "hi there")]


def test_the_service_away_keeps_turns_on_disk_and_replays(tmp_path):
    cap, client, spool = make(tmp_path)
    client.error = Unavailable("down")
    turn(cap)
    cap.close("chat-1")
    assert not cap.drain_once()
    assert spool.exists() and stat.S_IMODE(spool.stat().st_mode) == 0o600
    assert cap.backlog == 3 and client.sent == []
    assert cap.last_error and "down" in cap.last_error

    again, client2, _ = make(tmp_path)  # a new process, same path, service back
    turn(again, n=2)
    while again.backlog and again.drain_once():
        pass
    assert [m["text"] for m in client2.sent] == ["hello", "hi there", "hello", "hi there"]
    assert client2.calls == ["post", "close", "post"]  # the close stays in order after its turn
    assert client2.closed == ["chat-1"]
    assert not spool.exists()
    assert again.last_error is None and again.backlog == 0


def test_a_wrong_token_keeps_the_turns_and_says_why(tmp_path, caplog):
    cap, client, spool = make(tmp_path)
    client.error = Unauthorized(401, "bad token")
    turn(cap)
    with caplog.at_level(logging.WARNING):
        assert not cap.drain_once()
        assert not cap.drain_once()
    assert cap.backlog == 2 and spool.exists()
    assert cap.last_error and "HERMES_KERNEL_TOKEN" in cap.last_error
    assert len([r for r in caplog.records if "HERMES_KERNEL_TOKEN" in r.getMessage()]) == 1
    client.error = None
    assert cap.drain_once() and cap.backlog == 0 and cap.last_error is None


def test_a_refused_batch_is_dropped_so_it_cannot_block_the_rest(tmp_path):
    cap, client, _ = make(tmp_path)
    client.error = Refused(422, "text: too short")
    turn(cap)
    cap.drain_once()
    assert cap.backlog == 0 and cap.last_error and "422" in cap.last_error
    client.error = None
    turn(cap, n=2)
    cap.drain_once()
    assert len(client.sent) == 2 and cap.last_error is None


def test_a_full_spool_drops_oldest_and_logs_once(tmp_path, caplog):
    cap, client, spool = make(tmp_path, queue_max=4)
    client.error = Unavailable("down")
    turn(cap, n=1)
    cap.drain_once()
    with caplog.at_level(logging.WARNING):
        for n in range(2, 6):
            turn(cap, n=n)
    assert cap.backlog == 4
    assert len([r for r in caplog.records if "dropping the oldest" in r.getMessage()]) == 1
    client.error = None
    while cap.backlog:
        cap.drain_once()
    assert [m["message_id"] for m in client.sent[-2:]] == [
        message_id("chat-1", "s1", 5, "user", "hello"),
        message_id("chat-1", "s1", 5, "assistant", "hi there"),
    ]
    assert not spool.exists()


def test_nothing_runs_until_the_first_turn_and_the_worker_sends(tmp_path):
    client = FakeClient()
    before = threading.active_count()
    cap = Capture(client, tmp_path / "s.jsonl", clock=lambda: T0)
    assert threading.active_count() == before
    turn(cap)
    assert client.arrived.wait(5)
    cap.shutdown()
    assert len(client.sent) == 2


def test_a_lone_surrogate_reaches_the_service_as_valid_text(tmp_path):
    cap, client, _ = make(tmp_path)
    turn(cap, user="bad \ud800 text")
    turn(cap, n=2)
    while cap.backlog:
        assert cap.drain_once()
    assert len(client.sent) == 4
    client.sent[0]["text"].encode("utf-8")  # does not raise


def test_a_poison_batch_is_dropped_and_later_turns_still_send(tmp_path):
    cap, client, _ = make(tmp_path)
    real = client.post_messages

    def post(batch, agent_handles, *, timeout=30.0):
        if any("\ud800" in m["text"] for m in batch):
            raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")
        return real(batch, agent_handles)

    client.post_messages = post
    cap._enqueue([{"kind": "message", "handle": "hermes", "message": {"text": "\ud800"}}])  # pyright: ignore
    turn(cap, n=2)
    assert cap.drain_once()  # the poison item, alone at the head
    assert cap.last_error and "UnicodeEncodeError" in cap.last_error
    while cap.backlog:
        assert cap.drain_once()
    assert [m["text"] for m in client.sent] == ["hello", "hi there"]


def test_a_batch_with_one_refused_message_delivers_the_others(tmp_path):
    cap, client, _ = make(tmp_path)
    real = client.post_messages

    def post(batch, agent_handles, *, timeout=30.0):
        if any(m["text"] == "bad" for m in batch):
            raise Refused(422, "nope")
        return real(batch, agent_handles)

    client.post_messages = post
    turn(cap, n=1)
    turn(cap, n=2, user="bad")
    turn(cap, n=3)
    while cap.backlog:
        assert cap.drain_once()
    assert [m["text"] for m in client.sent] == ["hello", "hi there", "hi there", "hello", "hi there"]
    assert cap.last_error and "422" in cap.last_error


def test_a_409_keeps_the_batch_and_retries(tmp_path):
    cap, client, spool = make(tmp_path)
    client.error = Refused(409, "busy")
    turn(cap)
    assert not cap.drain_once()
    assert cap.backlog == 2 and spool.exists()
    client.error = None
    assert cap.drain_once() and len(client.sent) == 2


def test_a_dead_worker_is_replaced_on_the_next_turn(tmp_path):
    client = FakeClient()
    cap = Capture(client, tmp_path / "s.jsonl", clock=lambda: T0)
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    cap._thread = dead
    turn(cap)
    assert cap._thread is not dead
    assert client.arrived.wait(5)
    cap.shutdown()


def test_an_unreadable_spool_is_left_byte_identical_through_an_outage(tmp_path, monkeypatch):
    spool = tmp_path / "spool.jsonl"
    spool.write_bytes(b'{"kind": "close", "conversation_id": "old"}\n')
    before = spool.read_bytes()
    monkeypatch.setattr("plugin.spool.Spool.load", lambda self: None)
    cap, client, _ = make(tmp_path)
    client.error = Unavailable("down")
    turn(cap)
    cap.drain_once()
    assert spool.read_bytes() == before
    assert cap.last_error and "spool.jsonl" in cap.last_error
    client.error = None
    cap.drain_once()
    assert spool.read_bytes() == before
    assert cap.last_error and "spool.jsonl" in cap.last_error


def test_a_turn_without_a_conversation_is_skipped_and_empty_names_are_filled(tmp_path):
    cap, client, _ = make(tmp_path)
    cap.turn("", "Kira", "hello", "hi", session_id="s", turn_number=1)
    assert cap.backlog == 0
    cap.turn("c", "", "hello", "hi", session_id="s", turn_number=2)
    cap.drain_once()
    assert [m["speaker"] for m in client.sent] == ["user", "hermes"]


def test_a_404_holds_every_queued_message_and_drops_nothing(tmp_path):
    cap, client, spool = make(tmp_path)
    client.error = Refused(404, "no such path")
    turn(cap, n=1)
    turn(cap, n=2)
    assert not cap.drain_once()
    assert cap.backlog == 4 and spool.exists()
    assert cap.last_error and "404" in cap.last_error
    client.error = None
    while cap.backlog:
        assert cap.drain_once()
    assert len(client.sent) == 4


def test_a_plain_value_error_from_the_client_holds_rather_than_drops(tmp_path):
    cap, client, _ = make(tmp_path)
    client.error = ValueError("a bug in the client")
    turn(cap)
    assert not cap.drain_once()
    assert cap.backlog == 2 and cap.last_error and "ValueError" in cap.last_error


def test_one_add_never_holds_more_than_64_messages(tmp_path):
    """The service refuses an add of more than 64 (a 422 drops the batch): a backlog goes in batches of at most 64."""
    cap, client, _ = make(tmp_path)
    sizes: list[int] = []
    real = client.post_messages

    def post(batch, agent_handles, *, timeout=30.0):
        sizes.append(len(batch))
        return real(batch, agent_handles, timeout=timeout)

    client.post_messages = post
    for n in range(1, 71):  # 140 messages queued while the worker was not running
        turn(cap, n, user=f"hello {n}", reply=f"hi {n}")
    while cap.drain_once() and len(client.sent) < 140:
        pass
    assert len(client.sent) == 140
    assert max(sizes) == 64
