# hermes-kernel

A Hermes plugin and a small Docker stack that make [memory-kernel](https://github.com/Bonfires-ai/memory-kernel) your
own Hermes's memory. What you say to Hermes is built into a memory after each conversation, and the next conversation
starts with what it recalls. You can also teach Hermes named routines ("phrases") that it then reads out of your
messages and runs.

> **Anyone who can talk to this Hermes can get anything it remembers.** There is one memory for the whole install, and
> it is not split by chat or by person. That is right for one person's Hermes, or one team that shares everything. It
> is wrong for a Hermes that serves separate people who should not see each other's conversations. This version relies
> on Hermes's own allow-lists to decide who can talk to it.

## What you need

- **Hermes v0.23.0** (`hermes --version`). Other versions are not tested.
- **Docker** with Compose, on **Linux x86_64**. Running Hermes on a different machine from the stack is not supported.
- An **OpenRouter** key and a **Voyage AI** key. Both are required.
- **RAM:** plan on 10 GiB free while a conversation is being built into memory. Measured on full runs of this stack
  with short sessions, the five containers together peaked at 7.4, 8.0 and 8.1 GiB (the writer alone at 5.5, 5.9 and
  6.0 GiB) and idle held about 4.5 GiB. 16 GB of RAM in the machine is comfortable.
- **Disk:** the images take about 4.6 GB (the memory-kernel image is the big one, 3 GB). The data volumes held 0.8 GB
  (Neo4j 520 MB, Mongo 302 MB) after three short sessions, most of it fixed overhead. Leave a couple of GB free for the writer's scratch space.

## Install

```bash
git clone https://github.com/Bonfires-ai/hermes-kernel && cd hermes-kernel
cp .env.example .env
```

Fill in `.env`:

| Variable | What |
|---|---|
| `NEO4J_PASSWORD` | Any password. It **cannot contain `/`** (Neo4j reads `NEO4J_AUTH` as `user/password`). |
| `OPENROUTER_API_KEY` | Your OpenRouter key. |
| `VOYAGE_API_KEY` | Your Voyage AI key. **Required.** Compose refuses to start with it empty, but the stack's health checks never call Voyage, so a wrong or revoked key does not show up there: it shows up as sessions that fail to build (`/kernel` lists them). |
| `KT_TOKEN` | The bearer token Hermes uses to talk to the stack. Make one: `openssl rand -hex 24`. |
| `KT_BONFIRE_ID` | Leave as `hermes`. |

Then build and start the stack:

```bash
docker compose build        # slow the first time: it builds the memory-kernel image and downloads its models
docker compose up -d --wait
```

Install the plugin into Hermes and give it the token:

```bash
hermes plugins install Bonfires-ai/hermes-kernel/plugin --enable
hermes memory setup         # arrow up to "hermes-kernel", Enter; Enter for the URL; paste KT_TOKEN
hermes plugins enable hermes-kernel
```

`hermes memory setup` writes the token to `~/.hermes/.env` as `HERMES_KERNEL_TOKEN`. Use the menu: in Hermes v0.23.0,
`hermes memory setup hermes-kernel` (naming the provider) only switches it on and does not ask for the token. If you
would rather not use the menu, add that line yourself and run `hermes config set memory.provider hermes-kernel`. The URL defaults to
`http://127.0.0.1:8787`; set `HERMES_KERNEL_URL` to change it.

Start a new Hermes session. In it, type `/kernel`: it should say `Saving.`

**If Hermes was already running** (its background gateway stays up for ten minutes after a chat, and for good if you run
it as a service), these steps reach it with nothing restarted. That is why `hermes plugins enable` comes last: it makes a
running gateway reload its plugins, and by then the token is there. If you run the steps in another order, or add the
token later, memory still starts, and `/kernel` says that phrases are off until you run `hermes gateway restart`.
`hermes gateway stop` ends the gateway too, but if you have installed one as a service it acts on that service, and
everything the service runs stays stopped until you start it again.

**Only the turn service has a host port**, `127.0.0.1:8787`, behind the bearer token. The memory engine, Neo4j and
Mongo are reachable only from inside the stack.

## How memory arrives

Memory is not instant. A conversation is built into memory after its session closes:

1. A session that ends cleanly (you exit Hermes, or start a new session) closes at once. One that just goes quiet
   closes after **30 minutes** with no message.
2. The writer then builds it. Measured here, in six full runs with short sessions of a few messages, recall returned a
   session's content 131 to 173 seconds after it closed for the first session (which founds the memory), and 116 to
   137 seconds after for later ones; building took 110 to 183 seconds. Longer sessions take longer. Plan on a few
   minutes.
3. The next conversation recalls it. No restart is needed.

So a fact you give Hermes now is in its history for this session, and in its memory from the next one, a few minutes
after this one closes. The very first session founds the memory, so before it has been built there is nothing to recall
yet; `/kernel` says so.

Each Hermes turn also sends the new messages to the service as you go, so the conversation itself is stored on your
machine as it happens, ready for the writer.

## What leaves your machine

Everything that leaves, and where it goes:

- **Every message you send, before the model replies:** it goes to **Voyage AI** as a recall query (Hermes skips only
  trivial or empty ones), unless the same query was answered recently and is cached. `search_memory` calls send their
  query the same way. So your conversation reaches Voyage as you talk, not only when a session is built. This spend is
  **not** counted by the spend ceiling below.
- **Recalled memory:** what comes back is put into the prompt of each turn, so it goes to **your Hermes chat provider**
  (OpenRouter, in a default install) with the rest of the prompt.
- **A closed session:** when it is built, its messages go to **OpenRouter** (the model that reads them into memory) and
  to **Voyage AI** (embeddings).
- **A phrase's `web_search` step:** the words it searches for, which come from what you said, go to whichever search
  backend your Hermes uses. If you have not configured one, Hermes picks one itself and that may be a free, keyless
  service. Check which one with Hermes before you teach a phrase that searches.
- **Your normal Hermes chat** goes wherever your Hermes is already set up to send it.
- **Building the images** downloads packages and models from their own hosts, once.
- **Neo4j's anonymous usage report** is switched off in `compose.yaml` (`NEO4J_dbms_usage__report_enabled`). Neo4j 5.20
  and later send their server's IP address and database sizes and counts every 24 hours unless told not to. On the
  running stack, Neo4j's generated `neo4j.conf` reads `dbms.usage_report.enabled=false`.

Beyond that, nothing leaves that I know of. What I checked: the only host port is the turn service on loopback; Neo4j
and Mongo keep their data in Docker volumes on this machine; the Neo4j report is off, as above. What I did not do:
capture the stack's network traffic to prove that no library calls home.

## Cost

Building memory costs fractions of a cent per session at these sizes: in the end-to-end run, a first session of two prompts and
two replies was charged about $0.001, and the three short sessions of that run together $0.0025 to $0.0028; a 31-message
founding in an earlier measurement was charged about $0.012. Recall queries cost a Voyage embedding each, and they are
not counted here. Your Hermes chat model is separate and is whatever you already pay for.

### The spend ceiling

The writer will not spend more than **$20** in total on building memory, and caps the first session at **$1**
(`KT_FOUNDING_CAP_USD`). `KT_SPEND_CEILING_USD` sets the $20 only when the first session founds the memory: changing
it later does nothing. To change the ceiling afterwards, use `mk bonfire ceiling`. When the ceiling is reached,
sessions wait and `/kernel` says why. The ceiling is a running total, so raise it to a number above what has been spent
so far:

```bash
docker compose exec writer mk bonfire ceiling hermes 40      # "hermes" is KT_BONFIRE_ID; 40 is the new total in USD
docker compose restart mk-serve                              # optional: makes /kernel show the new numbers at once
```

`/kernel` and the service's status read the ceiling and the spend from `mk-serve`, which can lag by some minutes (in a
test, a lowered ceiling showed within three minutes and a raised one after three to six, and the spend still counted a
finished build's $0.10 estimate until `mk-serve` was restarted). Restarting it does not change what the writer does.

A held session is tried again on its own, with no restart: the writer looks for held sessions every ten minutes and
retries those that have been held for ten minutes or more, so a build starts 10 to 20 minutes after it was held, or one
round later if another build is running then (in a test, the session was built 17 minutes after the hold, with the
ceiling raised under 3 minutes after it).

## `/kernel`

Type `/kernel` in a Hermes session. It says one of:

- `Saving.`, with when the last session was built and how many closed sessions are waiting.
- `Not saving: ...` with the reason, for example the memory provider is not hermes-kernel.
- `The memory service refused the token: check HERMES_KERNEL_TOKEN.` when the token is wrong.
- That the memory service is away. Recent conversations are kept on disk and sent when it is back, up to a limit
  (1000 queued items).
- A session that failed to build, with its id: `/kernel retry <session id>` retries it.
- That the spend ceiling is holding builds, with the command above.
- `Memory is not set up`, when there is no token: run `hermes memory setup`.
- `Phrases are off`, when this Hermes loaded the plugin before it had the token: run `hermes gateway restart`.

## When something is down

Hermes keeps working. If the stack is down, Hermes answers as it would without this plugin: no recall, no phrases, and
turns wait on disk (`hermes-kernel-spool.jsonl` in your Hermes home) and are sent when the stack is back. A turn whose
recall is too slow goes on without it.

```bash
docker compose ps                  # what is running
docker compose up -d --wait        # start it again; memory is kept
docker compose logs --tail 100 writer     # why a session did not build
docker compose logs turn-service | grep call_refused      # why a phrase call was refused (its reason, never its words)
```

`/kernel` tells you whether memory is saving and why not.

## Phrases

**Teaching depends on the chat model.** It was proved on `anthropic/claude-sonnet-4.5` (the end-to-end run teaches,
confirms and runs a phrase with a real `web_search` and a `think` step the model writes). On `openai/gpt-4o-mini` it
did **not** work: asked to set up a phrase, that model called Hermes's own `skill_manage` tool (which failed) instead of
`offer_phrase` and `teach_phrase`, in three tries (the last one with the instruction not to, in the skill and in the turn's prompt block), and
then told the user that the phrase was set up when it was not. `/kernel` says nothing about phrases, so the signs are
that Hermes never reads a draft back to you, and that the phrase does nothing when you say it. Use a model that is good
at calling tools for teaching.

Ask Hermes to remember a routine ("when I say `research X`, search the web for X and summarise it"). It drafts the
phrase, reads it back, and keeps it only when you say yes. **Answer with just `yes`**: the kernel reads a bare
"yes" as a yes, but not a longer sentence such as "yes, keep it", and then the phrase stays a draft until you say
`yes` on its own. Then you say `research sourdough` and it runs the steps. In
this version a phrase only reads: its steps use Hermes's read-only tools (`web_search`) and `think`, a step the model
does itself. A step that would change something outside is refused. The plugin installs a skill that tells Hermes how.

## Platforms

Tested on the Hermes CLI. Other platforms (Telegram, Discord and the rest) are best effort: speakers are named as the
platform gives them, and a Hermes that serves several people shares one memory among them (see the warning at the top).

## Upgrading

```bash
git pull
docker compose build
docker compose up -d --wait
hermes plugins update hermes-kernel
```

Your memory stays in its Docker volumes.

## Uninstall

```bash
hermes plugins remove hermes-kernel      # also clears memory.provider
docker compose down                      # stops the stack and keeps the memory
docker compose down -v                   # stops it and DELETES the memory
```

Neither `down` removes the images, about 4.6 GB. To remove them: `docker rmi hermes-kernel-mk:2.0.1 hermes-kernel-turns:latest`
(and `neo4j:5.26-community` and `mongo:7`, if nothing else of yours uses them).

Then remove `HERMES_KERNEL_TOKEN` from `~/.hermes/.env`, and the files `hermes-kernel.json`,
`hermes-kernel-spool.jsonl` and `hermes-kernel-steps.json` in your Hermes home if they exist.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install ./service ./writer pytest pytest-asyncio mongomock ruff==0.17.4

.venv/bin/pytest service/tests -q                 # the turn service
.venv/bin/python -m pytest writer/tests -q        # the writer
scripts/name-scan.sh                              # no machine paths, keys or private names in any file
```

Set `HERMES_HOME` to a throwaway directory before running any of the scripts in `scripts/` (or any Hermes), because Hermes migrates the home it starts on; the scripts refuse to run when it is unset or inside `~/.hermes`, and the plugin's tests each run on a temporary home of their own.

The plugin's tests need Hermes v0.23.0 importable. `.github/workflows/ci.yml` builds it the way Hermes does (Python 3.14,
`uv sync --frozen` in a checkout of the `v0.23.0` tag) and runs `python -m pytest tests/plugin -q` in that environment.
`scripts/name-scan.sh` also reads private names from `.dev/private-names.txt` (git-ignored); with no name to look for it
fails, and `NAME_SCAN_GENERIC_ONLY=1` scans only for paths and keys.

`scripts/e2e.sh` runs the whole thing from nothing on a real Hermes v0.23.0 and a real stack. **It deletes data**: it
starts with `docker compose down -v` on its own project (`hermes-kernel-e2e`), uses a fresh temporary Hermes home and a
fresh token, and refuses to run without `E2E_CONFIRM=yes`, under any project name that does not start with
`hermes-kernel-e2e`, or while port 8787 answers. It spends real money (the kernel's share is under a cent; the chat model
about $0.25 with its default model) and takes about 15 minutes. Read its header first.

`service/tests/contract` checks the service's client against a real `mk serve`; it is skipped unless `KT_CONTRACT_URL`
is set. `mk serve` has no host port, so run it from a container on the stack's network; the test file's header says how.

## Licence

Apache-2.0, matching memory-kernel. See `LICENSE` and `NOTICE`. The images are built on your machine and nothing is
published: memory-kernel's setup downloads models under their own terms, which are not ours to redistribute.
