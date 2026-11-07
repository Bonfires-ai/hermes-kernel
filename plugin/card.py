"""A card to keep a phrase: Hermes's `clarify` tool, asked in a chat known to be private.

A confirmed phrase runs steps later, so the keep must come from the person, never from the model's words: a model
steered by hostile text could say "they picked Keep it". So nothing the model passes can confirm a phrase. The plugin
acts only on the real `clarify` result, seen in Hermes's `post_tool_call` hook (`CardBook.on_post_tool_call`), which a
model cannot write. It accepts that result only when the question and the choices are exactly what it issued, so the
card showed the true read-back, and only for a choice the surface reports as answered.

What Hermes hands the hook is the tool's JSON: `{"responses": [{"question", "choices_offered", "status",
"user_response"}], "outcome", "notice"?}` (`tools/clarify_tool.py`). `question` is the raw text, without a surface's
hint, stripped. `user_response` comes without the "(Recommended)" label Hermes puts on the first choice on screen.

A card leaves pending when it is answered, so a result cannot be replayed:

- **Keep it** calls the service's confirm with `card` on, as the speaker the card was opened for. A confirm that fails
  leaves the choice recorded, and `confirm_phrase` sends it again (`resend`), with what was recorded at the click.
- **Change it** and **Throw it away** record the draft as closed: mk has no route to delete a draft, so without this a
  later "yes" could revive one the person threw away. The record is in this process, bounded, so a restart forgets it.

Anything else changes nothing and the card stays pending. A card only ever opens in a private chat
(`LocalTurn.private`): `clarify` does not say who clicked, and on Telegram any allowed user may press a button.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from .client import ServiceError

if TYPE_CHECKING:
    from .turns import LocalTurn

logger = logging.getLogger(__name__)

#: Keep it first: it is what people want, and Hermes marks the first choice as recommended
CHOICES = ("Keep it", "Change it", "Throw it away")
KEEP = CHOICES[0]
#: Hermes abandons a `post_tool_call` callback at 30 s (`plugins.hook_callback_timeout`) and skips it for 60 s after
CONFIRM_TIMEOUT_S = 20.0
CHOSEN = "chosen"
KEPT = "kept"
CLOSED = "closed"

_CARDS_KEPT = 256
_DECISIONS_KEPT = 1024


def question(plain: str) -> str:
    """The card's question. Stripped, as Hermes strips it before it reports it."""
    return f"Keep this phrase? {plain}".strip()


def card_lines(draft_id: str, plain: str, speaker: str | None) -> list[str]:
    """`teach_phrase`'s answer in a private chat: the read-back, and how to ask with the card."""
    questions = json.dumps([{"question": question(plain), "choices": list(CHOICES)}], ensure_ascii=False)
    return [
        f"Draft {draft_id}: {plain}",
        f"Ask {speaker} with a card: call clarify now, copying exactly this questions value, as JSON: {questions}",
        "Keep it keeps the phrase by itself; then call confirm_phrase(draft_id) to hear the result. Change it: ask what "
        "to change and draft again. Throw it away: say so and stop. If clarify is not available, or they answer in the "
        'chat instead, ask for a plain "yes".',
    ]


class ConfirmApi(Protocol):
    """What a card calls on the turn service."""

    def confirm(self, draft_id: str, body: dict[str, Any], *, timeout: float = ...) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Card:
    """A card as it was opened: what it asked, and who confirming it speaks for."""

    draft_id: str
    turn_id: str
    platform: str
    group: str
    speaker: str
    question: str


@dataclass(frozen=True)
class Decision:
    """What the person chose on a card. Only the hook makes one."""

    card: Card
    kind: str
    #: the phrases that went live, when `kind` is `KEPT`
    phrases: tuple[str, ...] = ()

    @property
    def kept(self) -> bool:
        return self.kind == KEPT

    @property
    def chosen(self) -> bool:
        """Keep it was chosen, and the service has not confirmed it yet."""
        return self.kind == CHOSEN


def read_choice(card: Card, result: Any) -> tuple[str | None, str]:
    """The person's choice on `card` in a `clarify` result (`None` when there is none), and why, in words that hold no
    text of theirs or of the card's."""
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return None, "the result is not JSON"
    responses = parsed.get("responses") if isinstance(parsed, dict) else None
    if not isinstance(responses, list):
        return None, "the result has no responses"
    mine = [r for r in responses if isinstance(r, dict) and r.get("question") == card.question]
    if len(mine) != 1:
        return None, "no response has the card's question" if not mine else "two responses have the card's question"
    response = mine[0]
    if response.get("choices_offered") != list(CHOICES):
        return None, "the choices are not the card's"
    if response.get("status") != "answered":
        return None, "it was not answered"
    choice = response.get("user_response")
    if not isinstance(choice, str) or choice not in CHOICES:
        return None, "the answer is not one of the choices"
    return choice, "answered"


class CardBook:
    """The open cards, one per session, and what became of the drafts they asked about. Thread-safe and bounded."""

    def __init__(self, client: ConfirmApi) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._pending: OrderedDict[str, Card] = OrderedDict()
        self._decisions: OrderedDict[str, Decision] = OrderedDict()

    def open(self, session_id: str, draft_id: str, turn: LocalTurn, question: str) -> None:
        """Record a pending card for `session_id`, in place of any earlier one."""
        card = Card(draft_id, turn.turn_id or "", turn.platform, turn.group, turn.speaker or "", question)
        with self._lock:
            self._pending.pop(session_id, None)
            self._pending[session_id] = card
            while len(self._pending) > _CARDS_KEPT:
                self._pending.popitem(last=False)

    def decision(self, draft_id: str) -> Decision | None:
        with self._lock:
            return self._decisions.get(draft_id)

    def on_post_tool_call(
        self,
        tool_name: object = None,
        args: object = None,
        result: object = None,
        session_id: object = None,
        status: object = None,
        **_: object,
    ) -> None:
        """`post_tool_call` observer. Never raises, always `None`."""
        try:
            if tool_name == "clarify" and status == "ok" and isinstance(session_id, str) and session_id:
                self._answered(session_id, result)
        except ServiceError as exc:  # the choice stands: `confirm_phrase` sends it again
            logger.warning("hermes-kernel: a card's keep did not reach the service: %s", exc.detail)
        except Exception:  # noqa: BLE001 — a card costs a plain yes at worst, never the call
            logger.warning("hermes-kernel: could not read a card's result", exc_info=True)

    def _answered(self, session_id: str, result: object) -> None:
        with self._lock:
            card = self._pending.get(session_id)
        if card is None:
            logger.info("hermes-kernel: a clarify result: no card is open in this session")
            return
        choice, why = read_choice(card, result)
        if choice is None:
            logger.info("hermes-kernel: a clarify result changes nothing: %s", why)
            return
        decision = Decision(card, CHOSEN if choice == KEEP else CLOSED)
        with self._lock:
            if self._pending.get(session_id) is not card:  # another result took it
                return
            del self._pending[session_id]
            self._remember(decision)
        logger.info("hermes-kernel: a card was answered: %s", decision.kind)
        if decision.chosen:
            self.resend(decision, timeout=CONFIRM_TIMEOUT_S)
            logger.info("hermes-kernel: a card kept its phrase")

    def resend(self, decision: Decision, *, timeout: float) -> dict[str, Any]:
        """Confirm a choice of Keep it with what was recorded at the click; the service's answer, or what the client
        raises (the choice stays recorded)."""
        card = decision.card
        kept = self._client.confirm(
            card.draft_id,
            {
                "platform": card.platform,
                "group": card.group,
                "turn_id": card.turn_id,
                "by": card.speaker,
                "card": True,
            },
            timeout=timeout,
        )
        raw = kept.get("phrases")
        phrases = tuple(p for p in raw if isinstance(p, str)) if isinstance(raw, list) else ()
        with self._lock:
            self._remember(Decision(card, KEPT, phrases))
        return kept

    def _remember(self, decision: Decision) -> None:
        """Under `_lock`."""
        self._decisions.pop(decision.card.draft_id, None)
        self._decisions[decision.card.draft_id] = decision
        while len(self._decisions) > _DECISIONS_KEPT:
            self._decisions.popitem(last=False)
