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
  with short sessions, the five containers together peaked at 7.4 and 8.0 GiB (the writer alone at 5.5 and 5.9 GiB)
  and idle held about 4.5 GiB. 16 GB of RAM in the machine is comfortable.
- **Disk:** the images take about 4.6 GB (the memory-kernel image is the big one, 3 GB). The data volumes held about
  0.8 GB after three short sessions, most of it fixed overhead. Leave a couple of GB free for the writer's scratch space.

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
hermes plugins enable hermes-kernel
hermes memory setup         # arrow up to "hermes-kernel", Enter; Enter for the URL; paste KT_TOKEN
```

`hermes memory setup` writes the token to `~/.hermes/.env` as `HERMES_KERNEL_TOKEN`. If you would rather not use the
menu, add that line yourself and run `hermes config set memory.provider hermes-kernel`. The URL defaults to
`http://127.0.0.1:8787`; set `HERMES_KERNEL_URL` to change it.

Start a new Hermes session. In it, type `/kernel`: it should say `Saving.`

**Only the turn service has a host port**, `127.0.0.1:8787`, behind the bearer token. The memory engine, Neo4j and
Mongo are reachable only from inside the stack.

## How memory arrives

Memory is not instant. A conversation is built into memory after its session closes:

1. A session that ends cleanly (you exit Hermes, or start a new session) closes at once. One that just goes quiet
   closes after **30 minutes** with no message.
2. The writer then builds it. Measured here, in three full runs with short sessions of a few messages, recall returned a
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

- **When a session is built:** its messages go to **OpenRouter** (the model that reads them into memory) and to
  **Voyage AI** (embeddings).
- **When Hermes recalls:** each recall query goes to **Voyage AI** to be embedded, when it is not already cached. This
  spend is **not** counted by the spend ceiling below.
- **Your normal Hermes chat** goes wherever your Hermes is already set up to send it.
- Nothing else leaves: Neo4j, Mongo and the memory data stay in Docker volumes on this machine, and the service port
  is bound to loopback.

## Cost

Building memory costs fractions of a cent per session at these sizes: the first session in the end-to-end run below
(two messages) was charged $0.0009, all three short sessions of that run together $0.0025 to $0.0028, and a 31-message founding
in an earlier measurement was charged about $0.012. Recall queries cost a Voyage embedding each on a cache miss. Your Hermes chat model is separate and is whatever
you already pay for.

### The spend ceiling

The writer will not spend more than **$20** in total on building memory (`KT_SPEND_CEILING_USD`), and caps the first
session at **$1** (`KT_FOUNDING_CAP_USD`). When the ceiling is reached, sessions wait and `/kernel` says why. The
ceiling is a running total, so raise it to a number above what has been spent so far:

```bash
docker compose exec writer mk bonfire ceiling hermes 40      # "hermes" is KT_BONFIRE_ID; 40 is the new total in USD
docker compose restart mk-serve                              # so that /kernel shows the new ceiling
```

Without the restart, `/kernel` and the service's status keep showing the old ceiling.

Held sessions are tried again on their own, every ten minutes, once the ceiling is above what is spent.

## `/kernel`

Type `/kernel` in a Hermes session. It says one of:

- `Saving.`, with when the last session was built and how many closed sessions are waiting.
- `Not saving: ...` with the reason, for example the memory provider is not hermes-kernel, or the token is wrong.
- That the memory service is away. Recent conversations are kept on disk and sent when it is back, up to a limit
  (1000 queued items).
- A session that failed to build, with its id: `/kernel retry <session id>` retries it.
- That the spend ceiling is holding builds, with the command above.

## When something is down

Hermes keeps working. If the stack is down, Hermes answers as it would without this plugin: no recall, no phrases, and
turns wait on disk (`hermes-kernel-spool.jsonl` in your Hermes home) and are sent when the stack is back. A turn whose
recall is too slow goes on without it.

```bash
docker compose ps                  # what is running
docker compose up -d --wait        # start it again; memory is kept
docker compose logs --tail 100 writer     # why a session did not build
```

`/kernel` tells you whether memory is saving and why not.

## Phrases

Ask Hermes to remember a routine ("when I say `research X`, search the web for X and summarise it"). It drafts the
phrase, reads it back, and keeps it only when you say yes. **Answer with just `yes`**: the kernel reads a bare
"yes" as a yes, but not a longer sentence such as "yes, keep it", and then the phrase stays a draft until you say
`yes` on its own. Then you say `research sourdough` and it runs the steps. In
this version a phrase only reads: its steps use Hermes's read-only tools (`web_search`) and `think`, a step the model
does itself. A step that would change something outside is refused. The plugin installs a skill that tells Hermes how.

## Platforms

Tested on the Hermes CLI. Other platforms (Telegram, Discord and the rest) are best effort: speakers are named as the
platform gives them, and a Hermes that serves several people shares one memory among them (see the warning at the top).

## Uninstall

```bash
hermes plugins remove hermes-kernel      # also clears memory.provider
docker compose down                      # stops the stack and keeps the memory
docker compose down -v                   # stops it and DELETES the memory
```

Then remove `HERMES_KERNEL_TOKEN` from `~/.hermes/.env`, and the files `hermes-kernel.json`,
`hermes-kernel-spool.jsonl` and `hermes-kernel-steps.json` in your Hermes home if they exist.

## Development

```bash
.venv/bin/pytest service/tests -q                 # the turn service
.venv/bin/python -m pytest writer/tests -q        # the writer
python -m pytest tests/plugin -q                  # the plugin; needs Hermes v0.23.0 importable
scripts/name-scan.sh                              # no machine paths, keys or private names in any file
```

`scripts/e2e.sh` runs the whole thing from nothing on a real Hermes and a real stack. **It deletes data**: it starts
with `docker compose down -v` on its own project (`hermes-kernel-e2e`), uses a fresh temporary Hermes home, and
refuses to run without `E2E_CONFIRM=yes`, under the project name `hermes-kernel`, or while port 8787 answers. It spends
real money (cents) and takes about half an hour. Read its header first.

`service/tests/contract` checks the service's client against a real `mk serve`; it is skipped unless `KT_CONTRACT_URL`
is set. `mk serve` has no host port, so run it from a container on the stack's network.

## Licence

Apache-2.0, matching memory-kernel. See `LICENSE` and `NOTICE`. The images are built on your machine and nothing is
published: memory-kernel's setup downloads models under their own terms, which are not ours to redistribute.
