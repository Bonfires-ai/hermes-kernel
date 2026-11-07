"""Hermes's own tools as the kernel's catalogue, so a taught phrase can use them as steps.

The service has one bonfire, so the catalogue has one source, `agent`, PUT as `hermes` / `all` / `agent`. Its rows
come from the tools this Hermes offers its model:

- **which tools:** Hermes' registry (its built-in tools and MCP tools once their server is connected, named
  `mcp__<server>__<tool>`), less the toolsets `agent.disabled_toolsets` turns off, kept only when the data file
  `data/read_only_tools.json` lists them as read-only (the list is data). A tool Hermes defers behind `tool_search` is
  still in the registry, so it is still offered. A phrase step goes through `ctx.dispatch_tool`, which neither the
  disabled toolsets nor `pre_tool_call` guard, so the registry view applies the disabled toolsets itself (a security
  property: a phrase must never reach a tool the person turned off);
- **the row:** the tool's catalogue name, its description, the parameters a step gives (its required text
  parameters, or the ones the data file names; a phrase's values are text, so a tool whose required parameter is not
  text is left out), each annotated with its own name as its role; `x-side-effect: false`,
  `x-consent: speaker_turn`, `x-runner: agent`. A name no construction id can hold is left out (one bad row would
  make the kernel refuse the whole source);
- **short names:** an MCP tool's row is named by its bare name (`mcp__srv__web_search` -> `web_search`) when no
  other tool Hermes offers, no other row and not `think` has it; a clash keeps the long name. The source keeps the
  real name behind each row: a step is dispatched by it and its result fenced by it (Hermes fences an `mcp_*` result,
  not a bare name), and the call log maps a native call's real name to the row's (`catalogue_name`);
- **`think`** is always added: a pause in a phrase where the agent does the step itself with its own context. Both its
  parameters are required: a think step's input is "it" or a short literal;
- **offered now:** a tool whose `check_fn` fails (no search key, no vision backend) is one Hermes does not offer the
  model this turn. Its row stays in the source, so a check that flaps sends nothing again, but it is not afforded
  (`AgentSource.unavailable`, left out of the context) and a phrase step on it stops with a sentence
  (`AgentSource.tool` is `None`).

Two checks guard a step, and both are needed. The catalogue is one per install, so the registry view subtracts
`agent.disabled_toolsets` from everything (what no platform may use). Hermes also selects the *enabled* toolsets
per platform first, and `dispatch_tool` goes straight to the registry, so a person who turned a toolset off for one
platform could still have a phrase run its tool there: `enabled_for(platform, tool)` answers that at step time, and
fails closed. `side_effecting` lets a caller refuse a step on a tool Hermes offers that is not read-only.

`AgentSource.sync` PUTs the rows only when the tool set changed since the last set the service took (every install
drops the bonfire's in-flight plans): the digest is over the rows sorted by name, so the same tools in another order
are no change. A set the service refused (a 4xx) is not sent again until it changes. `sync_soon` runs `sync` on a
background daemon thread, one at a time, so a turn never waits on it; the service answers 204 before the bonfire
exists, which is a normal success.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .client import Refused

logger = logging.getLogger(__name__)

JsonValue = Any
JsonObject = dict[str, Any]

#: the catalogue source this module makes, and the one bonfire it belongs to (the service has one)
SOURCE = "agent"
PLATFORM = "hermes"
GROUP = "all"
SCHEMA_VERSION = "kernel-catalogue/v1"
THINK = "think"
#: the `think` row: the agent does the step itself; the run pauses for `phrase_continue`
THINK_ROW: JsonObject = {
    "name": THINK,
    "description": "the agent does this step itself with its own context",
    "input_schema": {
        "type": "object",
        "properties": {"instruction": {"type": "string"}, "input": {"type": "string"}},
        "required": ["instruction", "input"],
    },
    "x-args": {"instruction": {"role": "theme"}, "input": {"role": "source"}},
    "x-side-effect": False,
    "x-consent": "speaker_turn",
    "x-runner": "agent",
}
#: the read-only tools by their registry names (data, not code)
READ_ONLY_FILE = Path(__file__).resolve().parent / "data" / "read_only_tools.json"
#: what a tool's name may hold: the kernel makes its card ids from it (`card.<name lowercased>.<n>`)
_ID_PART = re.compile(r"[a-z0-9._-]+")
#: Hermes' MCP tool names: `mcp__<server>__<tool>` (`tools/mcp_tool_schema.py` `mcp_prefixed_tool_name`)
MCP_PREFIX = "mcp__"
_MAX_DESCRIPTION_CHARS = 500


@dataclass(frozen=True)
class ToolView:
    """One tool Hermes offers, as its registry holds it."""

    name: str
    toolset: str
    description: str
    #: the tool's JSON schema for its arguments (`{"type": "object", "properties": ..., "required": ...}`)
    parameters: Mapping[str, Any] = field(default_factory=dict)
    #: its `check_fn` passes: Hermes offers it to the model now (never part of a row, so never of the source's digest)
    available: bool = True


class RegistryView(Protocol):
    """The tools Hermes offers now (`HermesRegistry`)."""

    def offered(self) -> Sequence[ToolView]: ...

    def enabled_tools(self, platform: str) -> set[str]: ...


class SourceApi(Protocol):
    """The turn service's catalogue-source route (`ServiceClient`)."""

    def put_source(
        self, platform: str, group: str, source: str, catalogue: dict[str, Any], *, timeout: float = ...
    ) -> None: ...


def load_read_only(path: Path | None = None) -> tuple[set[str], dict[str, list[str]]]:
    """The data file: the read-only tools' names, and the parameters a step gives for those that name them."""
    raw = json.loads((path or READ_ONLY_FILE).read_text(encoding="utf-8"))
    rows = raw.get("tools") if isinstance(raw, dict) else None
    names: set[str] = set()
    params: dict[str, list[str]] = {}
    for row in rows if isinstance(rows, list) else []:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not name:
            continue
        names.add(name)
        named = row.get("params")
        if isinstance(named, list) and all(isinstance(p, str) for p in named):
            params[name] = list(named)
    return names, params


def _step_params(tool: ToolView, named: Sequence[str] | None) -> list[str] | None:
    """The parameters a step of `tool` gives: the ones the data file names, else the tool's required ones; each must
    be a text property. `None`: the tool cannot be a step."""
    props = tool.parameters.get("properties")
    props = props if isinstance(props, Mapping) else {}
    required = tool.parameters.get("required")
    wanted = list(named) if named is not None else [p for p in required or [] if isinstance(p, str)]
    for param in wanted:
        schema = props.get(param)
        if not isinstance(schema, Mapping) or schema.get("type") != "string":
            return None
    return wanted


def _bare(name: str) -> str | None:
    """An MCP tool's own name (`mcp__<server>__<tool>` -> `<tool>`, split at the first `__` after the server), else
    `None`."""
    if not name.startswith(MCP_PREFIX):
        return None
    _, sep, tool = name[len(MCP_PREFIX) :].partition("__")
    return tool if sep and tool else None


def _names(offered: Sequence[ToolView], kept: Sequence[ToolView]) -> dict[str, str]:
    """Each kept tool's catalogue name by its real one: an MCP tool's bare name when that name is unique among the kept
    tools' bare names and no tool Hermes offers, or `think`, has it; else its real name."""
    taken = {tool.name.lower() for tool in offered} | {THINK}
    bare = {tool.name: b for tool in kept if (b := _bare(tool.name)) is not None and _ID_PART.fullmatch(b.lower())}
    counts = Counter(b.lower() for b in bare.values())
    names: dict[str, str] = {}
    for tool in kept:
        b = bare.get(tool.name)
        names[tool.name] = b if b is not None and counts[b.lower()] == 1 and b.lower() not in taken else tool.name
    return names


def _row(tool: ToolView, params: list[str], name: str) -> JsonObject:
    props = tool.parameters.get("properties")
    copied: dict[str, JsonValue] = {p: copy.deepcopy(props[p]) for p in params} if isinstance(props, Mapping) else {}
    description = " ".join(tool.description.split())
    if len(description) > _MAX_DESCRIPTION_CHARS:
        description = description[: _MAX_DESCRIPTION_CHARS - 1] + "…"
    required: list[JsonValue] = list(params)
    return {
        "name": name,
        "description": description,
        "input_schema": {"type": "object", "properties": copied, "required": required},
        "x-args": {p: {"role": p} for p in params},
        "x-side-effect": False,
        "x-consent": "speaker_turn",
        "x-runner": "agent",
    }


def _rows_of(
    tools: Iterable[ToolView],
    read_only: set[str],
    params: Mapping[str, Sequence[str]] | None,
) -> tuple[list[JsonObject], dict[str, ToolView]]:
    """The rows, and the tool behind each by its catalogue name."""
    offered = list(tools)
    steps: list[tuple[ToolView, list[str]]] = []
    seen = {THINK}
    for tool in offered:
        if tool.name not in read_only:
            continue
        key = tool.name.lower()
        if key in seen or not _ID_PART.fullmatch(key):
            logger.warning(
                "hermes-kernel: %r cannot be a phrase step (its name clashes or no card id holds it)", tool.name
            )
            continue
        step = _step_params(tool, (params or {}).get(tool.name))
        if step is None:
            logger.info("hermes-kernel: %s takes a parameter that is not text; it is not a phrase step", tool.name)
            continue
        seen.add(key)
        steps.append((tool, step))
    names = _names(offered, [tool for tool, _ in steps])
    rows = [_row(tool, step, names[tool.name]) for tool, step in steps]
    rows.append(copy.deepcopy(THINK_ROW))
    return rows, {names[tool.name]: tool for tool, _ in steps}


def _hermes_entries() -> list[object]:
    from tools.registry import registry  # pyright: ignore[reportMissingImports]

    return list(registry.get_all_entries())


def _hermes_check(check_fn: Callable[[], object], memo: dict[object, bool]) -> bool:
    """Hermes' own availability check, through its TTL cache with a memo per read (as `get_definitions`, whose cache
    it is in a single-profile Hermes). Where Hermes bypasses that cache (a multiplex profile not resolved from this
    thread, a request-bound browser session) a probe here could fail closed on a tool Hermes does offer, so the tool
    counts as offered. The same holds when Hermes' registry cannot be imported: there is nothing to ask."""
    try:
        from tools.registry import (  # pyright: ignore[reportMissingImports]
            CHECK_FN_CACHE_BYPASS,
            _memo_check,
            check_fn_cache_scope,
        )
    except ImportError:
        return True
    try:
        if check_fn_cache_scope() == CHECK_FN_CACHE_BYPASS:
            return True
    except Exception:  # noqa: BLE001 — Hermes' scope unreadable: never hide what it may offer
        return True
    return bool(_memo_check(check_fn, memo))


def _hermes_disabled() -> set[str]:
    from hermes_cli.config import load_config_readonly  # pyright: ignore[reportMissingImports]

    agent = (load_config_readonly() or {}).get("agent")
    value = agent.get("disabled_toolsets") if isinstance(agent, dict) else None
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    return {str(part) for part in value} if isinstance(value, list) else set()


def _hermes_platform_toolsets(platform: str) -> set[str]:
    from hermes_cli.config import load_config_readonly  # pyright: ignore[reportMissingImports]
    from hermes_cli.tools_config import _get_platform_tools  # pyright: ignore[reportMissingImports]

    return {str(t) for t in _get_platform_tools(load_config_readonly(), platform)}


def _hermes_resolve(toolset: str) -> Iterable[str]:
    from toolsets import resolve_toolset  # pyright: ignore[reportMissingImports]

    return [str(n) for n in resolve_toolset(toolset)]


def _hermes_disabled_tools(toolsets: Collection[str]) -> set[str]:
    """The tool names Hermes itself subtracts for the disabled `toolsets` (`toolsets.resolve_toolset`, which follows a
    composite toolset's includes). Without Hermes' resolver the toolset names alone decide."""
    try:
        from toolsets import resolve_toolset  # pyright: ignore[reportMissingImports]
    except ImportError:
        return set()
    names: set[str] = set()
    for toolset in toolsets:
        try:
            names.update(str(n) for n in resolve_toolset(toolset))
        except Exception:  # noqa: BLE001 — an unknown toolset name resolves to nothing
            continue
    return names


class HermesRegistry:
    """The tools this Hermes offers: its registry's entries (`ToolEntry`: `name`, `toolset`, `schema`, `check_fn`)
    less the toolsets `agent.disabled_toolsets` turns off (by toolset, and by the tool names Hermes resolves for them),
    each marked `available` by its `check_fn` (a check that raises: not available). Hermes' modules are imported on
    each read, so a tool registered later (an MCP server connecting) is seen on the next read. A config that cannot
    be read raises: no tool is offered rather than one the person turned off."""

    def __init__(
        self,
        *,
        entries: Callable[[], Iterable[object]] | None = None,
        disabled: Callable[[], set[str]] | None = None,
        disabled_tools: Callable[[Collection[str]], set[str]] | None = None,
        platform_toolsets: Callable[[str], set[str]] | None = None,
        resolve: Callable[[str], Iterable[str]] | None = None,
        check: Callable[[Callable[[], object], dict[object, bool]], bool] | None = None,
    ) -> None:
        self._entries = entries or _hermes_entries
        self._disabled = disabled or _hermes_disabled
        self._disabled_tools = disabled_tools or _hermes_disabled_tools
        self._platform_toolsets = platform_toolsets or _hermes_platform_toolsets
        self._resolve = resolve or _hermes_resolve
        self._check = check or _hermes_check

    def _available(self, entry: object, memo: dict[object, bool]) -> bool:
        check_fn = getattr(entry, "check_fn", None)
        if not callable(check_fn):
            return True
        try:
            return self._check(check_fn, memo)
        except Exception:  # noqa: BLE001 — a check that cannot answer: Hermes would not offer the tool either
            logger.warning("hermes-kernel: the availability check of %s failed", getattr(entry, "name", "?"))
            return False

    def enabled_tools(self, platform: str) -> set[str]:
        """The tools `platform` may use: the names of its enabled toolsets, expanded as Hermes does, less the disabled
        toolsets (only tools `offered` returns count). Raises when Hermes cannot answer."""
        enabled: set[str] = set()
        for toolset in self._platform_toolsets(platform):
            enabled.update(self._resolve(toolset))
        return enabled & {tool.name for tool in self.offered()}

    def offered(self) -> list[ToolView]:
        off = self._disabled()
        off_tools = self._disabled_tools(off) if off else set()
        memo: dict[object, bool] = {}
        tools: list[ToolView] = []
        for entry in self._entries():
            name, toolset = getattr(entry, "name", None), getattr(entry, "toolset", "")
            if not isinstance(name, str) or toolset in off or name in off_tools:
                continue
            schema = getattr(entry, "schema", None)
            schema = schema if isinstance(schema, dict) else {}
            parameters = schema.get("parameters")
            description = schema.get("description") or getattr(entry, "description", "") or ""
            tools.append(
                ToolView(
                    name,
                    str(toolset),
                    str(description),
                    parameters if isinstance(parameters, dict) else {},
                    self._available(entry, memo),
                )
            )
        return tools


def _digest(rows: list[JsonObject]) -> str:
    ordered = sorted(rows, key=lambda row: str(row.get("name")))
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class AgentSource:
    """The agent catalogue source, PUT to the turn service when it changes."""

    def __init__(
        self,
        client: SourceApi,
        registry: RegistryView,
        read_only: set[str],
        *,
        params: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._read_only = set(read_only)
        self._params = dict(params or {})
        self._lock = threading.Lock()
        #: one PUT at a time (the digest check and the send are one step)
        self._send_lock = threading.Lock()
        #: the digest of the set the service last took, and of the last one it refused
        self._sent: str | None = None
        self._refused: str | None = None
        #: a background sync is running
        self._syncing = False
        #: a background failure was logged and no sync has succeeded since
        self._failed = False
        #: the rows as last read from the registry (the call log asks on every tool call)
        self._rows: list[JsonObject] | None = None
        #: the composable tools Hermes did not offer at that read (their check fails), by catalogue name
        self._unavailable: set[str] = set()
        #: a failure of `enabled_for` was logged
        self._enabled_failed = False
        #: by catalogue name, the real name of every row read in this process (kept when the tool goes away: a result
        #: held for it is still fenced by it)
        self._real: dict[str, str] = {}
        #: by real name, the catalogue name of each row of the last read
        self._catalogue: dict[str, str] = {}

    def _current(self) -> tuple[list[JsonObject], dict[str, ToolView]]:
        rows, kept = _rows_of(self._registry.offered(), self._read_only, self._params)
        with self._lock:
            self._rows = rows
            gone = set(self._real) - set(kept)  # was a row, no longer offered
            self._unavailable = {name for name, tool in kept.items() if not tool.available} | gone
            self._real.update({name: tool.name for name, tool in kept.items()})
            self._catalogue = {tool.name: name for name, tool in kept.items()}
        return rows, kept

    def unavailable(self) -> set[str]:
        """The composable tools Hermes did not offer at the last read of the registry: afforded to the model by
        nobody this turn."""
        with self._lock:
            return set(self._unavailable)

    def sync(self, *, timeout: float | None = None) -> None:
        """PUT the agent source when the tool set changed. Safe before the bonfire exists: the service stores the
        source and answers 204. Raises the client's `ServiceError` (the service away, or its refusal); the caller logs
        it and goes on."""
        with self._send_lock:
            rows, _ = self._current()
            digest = _digest(rows)
            with self._lock:
                if digest in (self._sent, self._refused):
                    return
            catalogue: JsonObject = {"schema_version": SCHEMA_VERSION, "tools": list(rows)}
            try:
                if timeout is None:
                    self._client.put_source(PLATFORM, GROUP, SOURCE, catalogue)
                else:
                    self._client.put_source(PLATFORM, GROUP, SOURCE, catalogue, timeout=timeout)
            except Refused as exc:
                with self._lock:
                    self._refused = digest
                logger.error("hermes-kernel: the turn service refused the tool catalogue: %s", exc.detail)
                raise
            with self._lock:
                self._sent, self._refused = digest, None
            logger.info(
                "hermes-kernel: the tool catalogue went to the kernel (%s)", ", ".join(str(r["name"]) for r in rows)
            )

    def sync_soon(self) -> None:
        """Run `sync` on a background daemon thread, never on the caller's: one at a time (a call while one runs is a
        no-op), the thread started on the first call. A failure is logged once and tried again by a later call."""
        with self._lock:
            if self._syncing:
                return
            self._syncing = True
        try:
            threading.Thread(target=self._sync_in_background, name="hermes-kernel-tools", daemon=True).start()
        except BaseException:
            with self._lock:
                self._syncing = False
            raise

    def _sync_in_background(self) -> None:
        try:
            self.sync()
        except Exception as exc:  # noqa: BLE001 — a failed sync costs a later turn's catalogue, never a turn
            with self._lock:
                first = not self._failed
                self._failed = True
            (logger.warning if first else logger.debug)("hermes-kernel: could not send the tool catalogue (%s)", exc)
        else:
            with self._lock:
                self._failed = False
        finally:
            with self._lock:
                self._syncing = False

    def side_effecting(self, names: Iterable[str]) -> set[str]:
        """Those of `names` (catalogue or real names) that Hermes offers now but the data file does not list as
        read-only: a phrase step may not call them. A registry read that raises is raised: the caller treats a raise
        as a refusal, never as allowed."""
        real = {name: self.real_name(name) or name for name in names if name != THINK}
        asked = {n for n, r in real.items() if r not in self._read_only}
        if not asked:
            return set()
        offered = {tool.name for tool in self._registry.offered()}
        return {n for n in asked if real[n] in offered}

    def enabled_for(self, platform: str, tool_name: str) -> bool:
        """Whether a phrase step may run `tool_name` (a catalogue or real name) on `platform`: the tool is in the
        platform's enabled toolsets and not in a disabled one. Fails closed: any error is False, logged once."""
        try:
            real = self.real_name(tool_name) or tool_name
            return real in self._registry.enabled_tools(platform)
        except Exception:  # noqa: BLE001 — unknown means not allowed
            with self._lock:
                first = not self._enabled_failed
                self._enabled_failed = True
            (logger.warning if first else logger.debug)(
                "hermes-kernel: could not resolve the enabled tools of %s; no step runs there", platform, exc_info=True
            )
            return False

    def tool(self, name: str) -> ToolView | None:
        """The tool behind the catalogue row `name` (its `name` the real one), as Hermes offers it now; `None` when
        it is no step any more (its MCP server is down, its toolset was turned off, its check fails) or is `think`."""
        tool = self._current()[1].get(name)
        return tool if tool is not None and tool.available else None

    def real_name(self, name: str) -> str | None:
        """The Hermes name behind the catalogue row `name`: what `dispatch_tool` takes and the untrusted-result fence
        decides by. Known from any read of the registry in this process (read now if it is not known yet); `None` when
        no read mapped it (its tool not offered since the start: an MCP server not connected yet), never the name
        itself, since Hermes fences an `mcp_*` result and not a bare short name."""
        with self._lock:
            known = self._real.get(name)
        if known is None:
            try:
                self._current()
            except Exception:  # noqa: BLE001 — the registry unreadable: the name as it is
                logger.warning("hermes-kernel: could not read the agent's tools to name %s", name, exc_info=True)
            with self._lock:
                known = self._real.get(name)
        return known

    def catalogue_name(self, real: str) -> str | None:
        """The catalogue row of the agent's tool `real` (a native call's name), from the rows last read; `None` when it
        is no step."""
        with self._lock:
            rows = self._rows
        if rows is None:
            self._current()
        with self._lock:
            return self._catalogue.get(real)

    def real_names(self) -> dict[str, str]:
        """The rows of the last read whose catalogue name is not the real one: catalogue name -> real name."""
        with self._lock:
            return {name: real for real, name in self._catalogue.items() if name != real}

    def steps(self) -> list[tuple[str, list[str]]]:
        """The tools a step can call now, `(catalogue name, parameters)`, from the rows last read: the composable ones
        Hermes offers, then `think`."""
        with self._lock:
            rows, off = self._rows, set(self._unavailable)
        if rows is None:
            rows = self._current()[0]
            with self._lock:
                off = set(self._unavailable)
        out: list[tuple[str, list[str]]] = []
        for row in rows:
            name, schema = str(row.get("name")), row.get("input_schema")
            props = schema.get("properties") if isinstance(schema, dict) else None
            if name not in off:
                out.append((name, [str(p) for p in props] if isinstance(props, dict) else []))
        return out

    def params(self, name: str) -> list[str] | None:
        """The parameters a step of tool `name` gives, from the rows last read; `None` when it is not one of the agent's
        composable tools (or is `think`)."""
        with self._lock:
            rows = self._rows
        if rows is None:
            rows = self._current()[0]
        for row in rows:
            if row.get("name") == name and name != THINK:
                schema = row.get("input_schema")
                props = schema.get("properties") if isinstance(schema, dict) else None
                return [str(p) for p in props] if isinstance(props, dict) else []
        return None
