"""Canonical arguments, turn ids and effect keys:

* ``canonical(args)`` is the kernel's own canonical JSON (``runtime.api._canonical``): sorted keys, no spaces, text
  unescaped. Two calls with the same canonical args are the same call.
* ``turn_id`` is the first 32 hex of sha256 over ``platform \\x1f group \\x1f chat \\x1f message_id``: the same message
  is always the same turn.
* ``effect_key`` is sha256 hex over ``turn_id \\x1f tool \\x1f canonical(args) \\x1f occurrence``. ``occurrence``
  counts this turn's COMMITTED ledger rows with the same tool and canonical args, so a step issued again before its
  report keeps its key, and the same call made a second time in one turn is a second effect.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from kernel_turns.models import JsonValue

SEP = "\x1f"


def canonical(args: Mapping[str, JsonValue]) -> str:
    return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def turn_id(platform: str, group: str, chat: str, message_id: str) -> str:
    return hashlib.sha256(SEP.join((platform, group, chat, message_id)).encode("utf-8")).hexdigest()[:32]


def effect_key(turn_id: str, tool: str, args: Mapping[str, JsonValue], occurrence: int) -> str:
    raw = SEP.join((turn_id, tool, canonical(args), str(occurrence)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
