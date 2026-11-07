"""Build one closed session into the bonfire: the first founds it, every later one appends.

Founding is the genesis build of the session (``graph_seed.seed``, the equivalent of ``mk build <conversation.json>
--genesis --boundary walk --through seal``): the session's messages are the genesis conversation, tagged
``session_1_conversation`` so a second chat can append later. Without ``genesis`` the build stops at ``graph_a`` with
"no picks". Every later session is appended with ``api.append_session`` under the one lease owner, ``OWNER``.

Each founding works in ``<scratch_dir>/found/<id>/<uuid>`` (the conversation, the purpose and the work dir), deleted when
the call ends. ``memory_kernel`` is imported lazily, inside functions, so the unit tests need no kernel install.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Final, Literal, cast
from uuid import uuid4

from hermes_writer.clean import ParseFn, clean_session, kernel_parse
from hermes_writer.config import Settings
from hermes_writer.queue import Claimed

if TYPE_CHECKING:
    from memory_kernel.pipeline.config import BuildConfig  # pyright: ignore[reportMissingImports]

log = logging.getLogger(__name__)

#: The one lease owner. A writer killed hard keeps a lease that still looks live for its whole 3600 s: the same owner
#: resending carries on, any other is refused as busy and ``mk bonfire release`` is refused too.
OWNER: Final = "hermes-writer"
LEASE_TTL_S: Final = 3600
#: The Purpose of the bonfire.
DEFAULT_PURPOSE: Final = "What this person and their agent talk about, decide and do."
#: The fixed `mk build` settings of a founding build.
FOUNDING_BOUNDARY = "walk"
FOUNDING_THROUGH = "seal"
#: `mk build`'s defaults where BuildConfig's own differ or a setting is unset: --referents, --neo4j-user.
CLI_REFERENTS = "structure"
CLI_NEO4J_USER = "neo4j"
#: The words of mk's refusals of a build into a group that is not empty.
FIRST_WRITE_REFUSED = ("first write refused", "group holds data no 2.0 seed started")
_CAP_STOP = "build cap:"
_CEILING_REFUSED = "bonfire spend ceiling reached"
_STOPPED_BEFORE_SEAL = "stopped before seal"
#: (Settings field, the variable it comes from): the keys every founding build needs.
SEED_KEYS: tuple[tuple[str, str], ...] = (
    ("openrouter_api_key", "OPENROUTER_API_KEY"),
    ("voyage_api_key", "VOYAGE_API_KEY"),
    ("neo4j_password", "NEO4J_PASSWORD"),
)
#: What `run_build` logs as each key's source (a name, never a value).
_SECRET_SOURCES: dict[str, str | None] = dict(SEED_KEYS)
#: A shorter value is not redacted: it would garble ordinary words (a password "neo4j" in "bolt://neo4j:7687").
_MIN_SECRET_LEN = 8
_DETAIL_MAX = 500
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True)
class BuildResult:
    """``built``: done (a duplicate or empty session counts). ``stopped``: a stop before seal, tried again. ``held``: at
    the spend ceiling, waits. ``busy``: the lease or a pending session blocked it, no attempt counted. ``refused``:
    mk will never take it."""

    kind: Literal["built", "stopped", "held", "busy", "refused"]
    reason: str | None = None


class ReleaseRefused(RuntimeError):
    """``mk bonfire release`` was refused (the lease still looks live)."""


class FoundingFailed(RuntimeError):
    """A founding that did not seal; tried again."""


class FoundingRefused(FoundingFailed):
    """A founding that cannot work until an operator sets a key."""


@dataclass(frozen=True, kw_only=True)
class FoundingBuild:
    """One founding build's ``mk build`` input. The keys are left out of the repr, so no traceback shows them."""

    conversation: Path
    purpose: Path
    workdir: Path
    group: str
    openrouter_api_key: str = field(repr=False)
    voyage_api_key: str = field(repr=False)
    neo4j_password: str = field(repr=False)
    neo4j_uri: str
    cap_usd: float


#: ``seed(build)``: ``graph_seed.seed``'s result (``{"status": "seeded" | "done"}``), or an exception.
SeedFn = Callable[[FoundingBuild], Mapping[str, object]]


def session_date(when: datetime) -> str:
    """``H:MM am|pm on D Month, YYYY`` in UTC."""
    utc = when.astimezone(UTC)
    hour = utc.hour % 12 or 12
    half = "am" if utc.hour < 12 else "pm"
    return f"{hour}:{utc.minute:02d} {half} on {utc.day} {_MONTHS[utc.month - 1]}, {utc.year}"


def genesis_conversation(
    bonfire_id: str, conversation_id: str, agent_handles: Iterable[str], messages: list[dict[str, Any]]
) -> dict[str, object]:
    """``mk build``'s input: ``{sample_id, conversation: {agent_handles, session_1, session_1_date_time,
    session_1_conversation}}``, one turn per message with the message's own id as ``dia_id``."""
    turns: list[dict[str, object]] = [
        {
            "speaker": m["speaker"],
            "dia_id": m["message_id"],
            "text": m["text"],
            "timestamp": m["timestamp"],
            "addressees": list(m.get("addressees") or []),
        }
        for m in messages
    ]
    return {
        "sample_id": bonfire_id,
        "conversation": {
            "agent_handles": list(agent_handles),
            "session_1": turns,
            "session_1_date_time": session_date(datetime.fromisoformat(str(messages[0]["timestamp"]))),
            "session_1_conversation": conversation_id,
        },
    }


def redact(text: str, secrets: Iterable[str]) -> str:
    for value in sorted({s for s in secrets if len(s) >= _MIN_SECRET_LEN}, key=len, reverse=True):
        text = text.replace(value, "[secret]")
    return text


def _detail(exc: BaseException, secrets: Iterable[str]) -> str:
    return redact(f"{type(exc).__name__}: {exc}", secrets)[:_DETAIL_MAX]


def founding_error(exc: BaseException, secrets: Iterable[str]) -> BuildResult:
    """``exc`` as the result it is, with a redacted reason. mk's classes are matched by name: the kernel is not
    imported here. An unknown exception is a stop (tried again, and failed after the last try)."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    text = str(exc)
    detail = _detail(exc, secrets)
    if _CEILING_REFUSED in text:
        return BuildResult("held", detail)
    if "AppendBusy" in names or "PendingSession" in names:
        return BuildResult("busy", detail)
    if any(words in text for words in FIRST_WRITE_REFUSED):
        return BuildResult("refused", f"the bonfire's graph group holds data no 2.0 seed started: {detail}")
    if _STOPPED_BEFORE_SEAL in text and _CAP_STOP in text:
        return BuildResult("refused", f"the founding build hit its cap before it sealed: {detail}")
    if _STOPPED_BEFORE_SEAL in text:
        return BuildResult("stopped", detail)
    if "BonfireError" in names:
        return BuildResult("refused", detail)
    return BuildResult("stopped", detail)


def _keys(settings: Settings) -> dict[str, str]:
    """The non-blank keys among ``SEED_KEYS``, by field name."""
    out: dict[str, str] = {}
    for name, _ in SEED_KEYS:
        secret = getattr(settings, name)
        if secret is not None and secret.get_secret_value().strip():
            out[name] = secret.get_secret_value()
    return out


def build_overrides(settings: Settings) -> dict[str, object]:
    """The ``BuildConfig`` fields set over the bonfire's own, as ``mk append`` does: the secrets, this host's data dir and
    the Neo4j address. A field left out keeps the bonfire's."""
    out: dict[str, object] = {"data_dir": settings.data_dir, "neo4j_uri": settings.neo4j_uri}
    for name in ("openrouter_api_key", "voyage_api_key", "neo4j_password"):
        secret = getattr(settings, name)
        if secret is not None:
            out[name] = secret.get_secret_value()
    return out


def open_ops(settings: Settings) -> object:
    """A ``MongoKernelOps`` on ``MK_OPS_URI`` (spend holds, the append lease)."""
    mongo = importlib.import_module("memory_kernel.ops.mongo")
    return mongo.MongoKernelOps.from_uri(
        settings.ops_uri.get_secret_value(), db=settings.ops_db, collection="kernel_ops"
    )


def to_build_config(build: FoundingBuild, settings: Settings) -> BuildConfig:
    """The ``BuildConfig`` ``mk build`` makes of the founding flags: genesis, the walk boundary, through seal,
    ``--cap-usd``, ``--neo4j-uri``, every other flag at the CLI's default. The keys go in as fields."""
    from memory_kernel import setup_runtime  # pyright: ignore[reportMissingImports]
    from memory_kernel.cli import read_purpose  # pyright: ignore[reportMissingImports]

    # The kernel annotates load_conversation's sample as a bare `dict`: it is read as a Mapping below.
    from memory_kernel.pipeline.config import (  # pyright: ignore[reportMissingImports]
        BuildConfig,
        lexical_abspath,
        load_conversation,  # pyright: ignore[reportUnknownVariableType]
    )

    conversation = lexical_abspath(build.conversation)
    root = lexical_abspath(".")  # --root's default: the cwd
    item = cast("Mapping[str, object]", load_conversation(conversation, None)[0])
    return BuildConfig(
        sample_id=str(item["sample_id"]),
        conversation=conversation,
        group=build.group,
        purpose=read_purpose(lexical_abspath(build.purpose, base=root)),
        workdir=lexical_abspath(build.workdir),
        root=root,
        data_dir=setup_runtime.resolve_data_dir(settings.data_dir, {}),
        boundary_policy=FOUNDING_BOUNDARY,
        referent_policy=CLI_REFERENTS,
        neo4j_uri=build.neo4j_uri,
        neo4j_user=CLI_NEO4J_USER,
        neo4j_password=build.neo4j_password,
        cap_usd=build.cap_usd,
        openrouter_api_key=build.openrouter_api_key,
        voyage_api_key=build.voyage_api_key,
        through=FOUNDING_THROUGH,
        genesis=True,
    )


def _graph_seed(build: FoundingBuild, settings: Settings) -> Mapping[str, object]:
    """The default ``SeedFn``: ``graph_seed.seed`` of ``to_build_config``, on the ``kernel_ops`` record."""
    from memory_kernel.pipeline import graph_seed  # pyright: ignore[reportMissingImports]

    ops = open_ops(settings)
    try:
        return cast(
            "Mapping[str, object]",
            graph_seed.seed(
                to_build_config(build, settings),
                ops=ops,
                ceiling_usd=settings.spend_ceiling_usd,
                secret_sources=_SECRET_SOURCES,
            ),
        )
    finally:
        close = getattr(ops, "close", None)
        if callable(close):
            close()


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def found_bonfire(
    settings: Settings, claimed: Claimed, messages: list[dict[str, Any]], *, seed: SeedFn | None = None
) -> bool:
    """Found the bonfire from this session. True when it was founded, False when its graph says it already was (the
    session is then an ordinary append). Raises ``FoundingFailed`` for a key that is unset or a build that did not seal."""
    bonfire_id = settings.bonfire_id
    keys = _keys(settings)
    workdir = settings.scratch_dir / "found" / bonfire_id / uuid4().hex
    try:
        missing = [var for name, var in SEED_KEYS if name not in keys]
        if missing:
            raise FoundingRefused(f"cannot found bonfire '{bonfire_id}': {', '.join(missing)} is unset or blank")
        inputs = workdir / "input"
        inputs.mkdir(parents=True)
        (workdir / "work").mkdir()
        conversation, purpose = inputs / "conversation.json", inputs / "purpose.txt"
        doc = genesis_conversation(bonfire_id, claimed["conversation_id"], claimed["agent_handles"], messages)
        _write_atomic(conversation, json.dumps(doc, indent=2, ensure_ascii=False))
        _write_atomic(purpose, DEFAULT_PURPOSE + "\n")
        build = FoundingBuild(
            conversation=conversation,
            purpose=purpose,
            workdir=workdir / "work",
            group=bonfire_id,
            openrouter_api_key=keys["openrouter_api_key"],
            voyage_api_key=keys["voyage_api_key"],
            neo4j_password=keys["neo4j_password"],
            neo4j_uri=settings.neo4j_uri,
            cap_usd=float(settings.founding_cap_usd),
        )
        out = seed(build) if seed is not None else _graph_seed(build, settings)
        status = out.get("status")
        if status == "seeded":
            return True
        if status == "done":
            return False
        raise FoundingFailed(f"the founding build of '{bonfire_id}' returned status {status!r}, not seeded or done")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        with contextlib.suppress(OSError):  # refuses while a concurrent founding's directory is still inside
            workdir.parent.rmdir()


def build_session(
    settings: Settings,
    claimed: Claimed,
    *,
    api: ModuleType | Any | None = None,
    seed: SeedFn | None = None,
    ops: object | None = None,
    parse: ParseFn | None = kernel_parse,
) -> BuildResult:
    """Clean the session, then found the bonfire from it or append it. Never raises: a failure is a ``BuildResult``
    whose reason carries no key. ``api`` (default ``memory_kernel.api``), ``ops``, ``seed`` and ``parse`` are the
    seams the tests replace."""
    secrets = settings.secrets()
    own_ops = False
    try:
        cleaned, _ = asyncio.run(
            clean_session(
                {"messages": claimed["messages"]},
                parse=parse,
                bonfire_id=settings.bonfire_id,
            )
        )
        messages: list[dict[str, Any]] = cleaned["messages"]
        if not messages:  # nothing to build: as good as built
            return BuildResult("built", "empty")
        if api is None:
            api = importlib.import_module("memory_kernel.api")
        overrides = build_overrides(settings)
        if not api.bonfire_exists(settings.bonfire_id, overrides=overrides):
            if found_bonfire(settings, claimed, messages, seed=seed):
                return BuildResult("built", "founded")
            # Founded already by an earlier, interrupted attempt: this session is an ordinary append.
        if ops is None:
            ops = open_ops(settings)
            own_ops = True
        session = api.SessionInput(
            conversation_id=claimed["conversation_id"],
            messages=messages,
            agent_handles=list(claimed["agent_handles"]) or None,
        )
        out = api.append_session(
            settings.bonfire_id,
            session,
            ops=ops,
            scratch_root=settings.scratch_dir,
            owner=OWNER,
            lease_ttl_s=LEASE_TTL_S,
            on_built="skip",
            overrides=overrides,
        )
        status = out.get("status")
        if status == "stopped":
            return BuildResult(
                "stopped", redact(str(out.get("reason") or "stopped before seal"), secrets)[:_DETAIL_MAX]
            )
        # appended; duplicate and empty ran nothing and leave nothing to do
        return BuildResult("built", str(status))
    except FoundingFailed as exc:
        kind = "refused" if isinstance(exc, FoundingRefused) else "stopped"
        return BuildResult(kind, redact(str(exc), secrets)[:_DETAIL_MAX])
    except Exception as exc:  # noqa: BLE001 - classified by name, never raised
        return founding_error(exc, secrets)
    finally:
        if own_ops:
            close = getattr(ops, "close", None)
            if callable(close):
                close()


def release(settings: Settings) -> None:
    """``mk bonfire release <id>``. Raises ``ReleaseRefused`` when mk refuses (a lease that still looks live)."""
    uri = settings.ops_uri.get_secret_value()
    done = subprocess.run(
        ["mk", "bonfire", "release", settings.bonfire_id, "--ops-uri", uri],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "MK_OPS_URI": uri},
        check=False,
    )
    if done.returncode != 0:
        text = (done.stderr or done.stdout or f"exit {done.returncode}").strip()
        raise ReleaseRefused(redact(text, settings.secrets())[:_DETAIL_MAX])
