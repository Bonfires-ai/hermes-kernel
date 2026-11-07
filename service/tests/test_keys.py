"""Canonical args, turn ids and effect keys, exactly as the Rulings define them."""

from __future__ import annotations

import hashlib
import re

from kernel_turns import keys


def test_keys_are_canonical() -> None:
    shuffled = {"b": 1, "a": "Kira Dace — café 🎉"}
    ordered = {"a": "Kira Dace — café 🎉", "b": 1}
    assert keys.canonical(shuffled) == keys.canonical(ordered)
    assert keys.canonical(shuffled) == '{"a":"Kira Dace — café 🎉","b":1}'  # the text survives unescaped
    nested = {"z": {"y": [1, {"b": None, "a": True}]}, "x": 1.5}
    assert keys.canonical(nested) == '{"x":1.5,"z":{"y":[1,{"a":true,"b":null}]}}'

    turn = keys.turn_id("b1", "telegram", "-100777", "-100777", "42")
    assert re.fullmatch(r"[0-9a-f]{32}", turn)
    assert turn == keys.turn_id("b1", "telegram", "-100777", "-100777", "42")
    assert turn == hashlib.sha256(b"b1\x1ftelegram\x1f-100777\x1f-100777\x1f42").hexdigest()[:32]
    assert turn != keys.turn_id("b1", "telegram", "-100777", "-100777", "43")
    assert turn != keys.turn_id("b1", "discord", "-100777", "-100777", "42")

    first = keys.effect_key(turn, "take", shuffled, 0)
    assert first == keys.effect_key(turn, "take", ordered, 0)
    assert first != keys.effect_key(turn, "take", ordered, 1)  # the same call made again is another effect
    assert first != keys.effect_key(turn, "give", ordered, 0)
    expected = f"{turn}\x1ftake\x1f" + '{"a":"Kira Dace — café 🎉","b":1}' + "\x1f0"
    assert first == hashlib.sha256(expected.encode()).hexdigest()


def test_turn_id_depends_on_the_bonfire() -> None:
    assert keys.turn_id("a", "cli", "group", "chat", "m1") != keys.turn_id("b", "cli", "group", "chat", "m1")


def test_message_key_is_the_bonfire_then_the_message() -> None:
    assert keys.message_key("b1", "m1") == "b1\x1fm1"
