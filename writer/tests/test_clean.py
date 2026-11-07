from __future__ import annotations

import asyncio

from hermes_writer.clean import clean_session


async def _parse(text: str) -> list[dict[str, str]]:
    # a fake kernel parser: it cannot read a sentence marked BAD, nor a message of more than 31 sentences
    if "BAD" in text or text.count(".") > 31:
        raise ValueError("unreadable")
    return [{"tree": text}]


def test_clean_keeps_what_it_can():
    long_reply = " ".join(f"Sentence {n}." if n != 7 else "BAD sentence." for n in range(1, 41))
    session = {
        "messages": [
            {"message_id": "m1", "speaker": "me", "text": "Hello\tthere \U0001f600"},
            {"message_id": "m2", "speaker": "agent", "text": long_reply},
            {"message_id": "m3", "speaker": "me", "text": "BAD"},
        ]
    }
    cleaned, changed = asyncio.run(clean_session(session, parse=_parse, bonfire_id="b"))
    ids = [m["message_id"] for m in cleaned["messages"]]
    assert ids[0] == "m1" and cleaned["messages"][0]["text"] == "Hello there \U0001f600"
    pieces = ids[1:]
    assert pieces == [f"m2~{n}" for n in range(1, 41) if n != 7]  # only the unreadable piece is gone
    assert "m3" not in ids  # a message with no readable piece is dropped whole
    assert set(changed) == {"m1", "m2", "m3"}
