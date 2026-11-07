"""Clean a session before the kernel sees it: normalise every message, check it through the kernel parser.

A message the parser cannot read (a tab, a non-BMP glyph, a 40-sentence paste) fails the whole capture and holds up
every session behind it. So the writer repairs what it can and drops what it cannot, and a session is never stuck for
its text.

Not applied here: mk's own clean_text (links to [link: host], data URIs, long blobs) and its split_parts
(parts of at most 1900 characters and 20 sentences). The check is on the normalised text, so a message mk would have
reshaped may be split finer, or lose a sentence carrying a data URI or blob, that mk would have kept.

Kernel ids: a split message's pieces are <id>~<n>; a consumer mapping kernel message ids back to the original ids
strips a trailing ~<n>.

Memory: kernel_parse loads spaCy, benepar and torch into this long-lived process (about 1.8 GB RSS after the first
cleaned session, measured in the image), on top of the capture's own parser child.

Per message: normalise it; check it with parse (the kernel's syntactic parser: 1..31 trees, and the token basis
the kernel reads). A message that still fails is split on line breaks, then on sentence ends, and each piece is checked
alone; a sentence still unreadable is cut into windows of 150 words. The passing pieces stay, as messages
<id>~<n> (n = the piece's position, from 1: pieces, not sentences); the failing ones are dropped. A message
with no passing piece is dropped whole. A parser failure counts against the text only if a canary parse succeeds; if
the canary fails too, the parser is down and the error propagates (the session is tried again). Every drop is logged as
kernel.session.message_dropped.

normalise is a copy of the kernel's own statement normaliser, so this package needs no other import for it.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The parser wire: a message is one to this many trees (the kernel's limit).
MAX_TREES = 31
#: A known-good text: when ``parse`` raises on a message, parsing this tells a text fault from a parser outage (mk's
#: ``PARSER_WARMUP_TEXT``).
CANARY_TEXT = "The parser is alive."
#: A piece still unreadable after the sentence split is cut into windows of this many words (as mk's ``split_parts``).
WINDOW_WORDS = 150
#: Parser model of the check (the wire's name for benepar).
PARSER_MODEL = "en_benepar"


class Unreadable(ValueError):
    """The parser's verdict on one text: it cannot be read. It counts against the text only if the canary parses."""


Tree = dict[str, Any]
SessionDict = dict[str, Any]
ParseFn = Callable[[str], Awaitable[list[Tree]]]

_WHITESPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def normalise(text: str) -> str:
    """Control characters dropped; non-BMP characters folded with NFKC, one at a time; every run of whitespace
    (tabs, newlines) one space."""
    out: list[str] = []
    for ch in text:
        # an NFKC fold can be several characters (U+1F190 -> "DJ"): judge each one
        for char in unicodedata.normalize("NFKC", ch) if ord(ch) > 0xFFFF else ch:
            if char.isspace():
                out.append(" ")
            elif unicodedata.category(char) not in ("Cc", "Cf", "Cs"):
                out.append(char)
    return _WHITESPACE.sub(" ", "".join(out)).strip()


async def _readable(text: str, parse: ParseFn | None) -> bool:
    if not text:
        return False
    if parse is None:
        return True
    try:
        trees = await parse(text)
    except Exception:
        # ``Unreadable`` (what ``kernel_parse`` makes of ValueError / IndexError / KeyError) or any other exception.
        # A known-good canary tells a text fault from an outage for all of them: it parses -> the text is at fault;
        # it fails too -> a real outage (a spaCy/benepar skew fails every text alike), re-raised so the session is tried
        # again instead of dropping every message.
        try:
            await parse(CANARY_TEXT)
        except Exception:  # noqa: BLE001 - any canary failure means the parser is down
            pass
        else:
            return False
        raise
    return 1 <= len(trees) <= MAX_TREES


def _windows(sentence: str) -> list[str]:
    words = sentence.split()
    return [" ".join(words[i : i + WINDOW_WORDS]) for i in range(0, len(words), WINDOW_WORDS)]


async def _repair(message: dict[str, Any], parse: ParseFn | None, bonfire_id: str | None) -> list[dict[str, Any]]:
    """The messages that replace ``message`` when its normalised text does not parse: split on line breaks and sentence
    ends; a sentence still unreadable (an unpunctuated paste) is cut into word windows. What still fails is dropped."""
    sentences = [s for line in str(message["text"]).splitlines() for s in _SENTENCE_END.split(normalise(line)) if s]
    kept: list[dict[str, Any]] = []
    slot = 0  # the position of a sentence (or window) in the message, from 1; dropped ones keep their slot

    async def keep_or_drop(text: str) -> bool:
        nonlocal slot
        slot += 1
        if await _readable(text, parse):
            kept.append({**message, "message_id": f"{message['message_id']}~{slot}", "text": text})
            return True
        logger.warning(
            "kernel.session.sentence_dropped",
            extra={"bonfire_id": bonfire_id, "message_id": message["message_id"], "position": slot},
        )
        return False

    for sentence in sentences:
        windows = _windows(sentence)
        if len(windows) < 2:
            if len(sentences) > 1:
                await keep_or_drop(sentence)
        elif len(sentences) > 1 and await _readable(sentence, parse):
            slot += 1
            kept.append({**message, "message_id": f"{message['message_id']}~{slot}", "text": sentence})
        else:
            for window in windows:
                await keep_or_drop(window)
    return kept


async def clean_session(
    session: SessionDict, *, parse: ParseFn | None, bonfire_id: str | None = None
) -> tuple[SessionDict, list[str]]:
    """Return ``(cleaned session, ids of the messages changed or dropped)``.

    ``parse(text)`` returns the parser's trees and raises when the text is not readable. A failure (``Unreadable`` or any
    other exception) is the text's fault only if parsing ``CANARY_TEXT`` succeeds; otherwise it is an outage and
    propagates. ``None`` skips the parser check (normalisation only). A session left with no messages comes back with
    ``messages == []``: the caller answers ``empty``.
    """
    messages: list[dict[str, Any]] = []
    changed: list[str] = []
    for original in session["messages"]:
        text = normalise(str(original["text"]))
        if await _readable(text, parse):
            messages.append({**original, "text": text})
            if text != original["text"]:
                changed.append(str(original["message_id"]))
            continue
        repaired = await _repair(original, parse, bonfire_id)
        changed.append(str(original["message_id"]))
        if repaired:
            messages.extend(repaired)
        else:
            logger.warning(
                "kernel.session.message_dropped", extra={"bonfire_id": bonfire_id, "message_id": original["message_id"]}
            )
    return {**session, "messages": messages}, changed


async def kernel_parse(text: str) -> list[Tree]:
    """The real ``parse``: the kernel's own syntactic parser, run in-process (nothing else in the image serves it: the
    parser service is a loopback child of ``mk build``, with no client). It is the handler the kernel mounts at
    ``/syntactic-parser`` (``en_benepar``), then the token-basis check ``source_tokens_from_nodes``. Raises when
    the text is not readable. memory_kernel is imported here, not at module level (the tests do not install it)."""

    def run() -> list[Tree]:
        # Lazy, by name: the unit tests run without memory_kernel.
        transport = importlib.import_module("memory_kernel.construction.fcg.native_frame_transport")
        facade = importlib.import_module("memory_kernel.parser_service.facade")
        try:
            trees = facade.syntactic_parser(facade.SentenceRequest(sentence=text, model=PARSER_MODEL))["trees"]
        except (ValueError, IndexError, KeyError) as exc:  # benepar's refusals (over-length); constituency._collapse
            # IndexError / KeyError when spaCy and benepar leaves misalign (tab, newline, nbsp: probed in the image)
            raise Unreadable(str(exc)) from exc
        if not 1 <= len(trees) <= MAX_TREES:
            raise Unreadable(f"{len(trees)} trees")
        nodes = [{**n, "node--type": n["node_type"]} for t in trees for n in t["tree"]]
        try:
            transport.source_tokens_from_nodes(nodes, text)
        except transport.NativeFrameTransportError as exc:
            raise Unreadable(str(exc)) from exc
        return trees

    return await asyncio.to_thread(run)
