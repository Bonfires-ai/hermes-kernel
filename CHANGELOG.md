# Changelog

## Unreleased

The first release: a Hermes plugin and a small Docker stack (memory-kernel behind a local turn service and a writer)
that make memory-kernel a person's own Hermes memory and turn reader.

- Memory: finished turns go to the turn service; a closed session is built into the memory by the writer; the next
  plan recalls it. Recall is added to the model's context before it answers.
- Phrases: a person can teach Hermes a phrase (one or more forms and a body of read-only steps), confirm it, and run it
  with `run_phrase`. A `think` step is done by the model.
- `/kernel` says whether memory is saving, which sessions failed to build and how to retry them, and what holds
  building (the spend ceiling).
- Hermes keeps working when the stack is down: no recall, and turns wait on disk until it is back.
- Pinned to memory-kernel v2.0.1 (the image installs it from the tag); torch and the pre-installs are pinned exactly.
- A plain "yes" confirms a phrase (the read-back asks for one); a phrase is not a Hermes skill (the block and the skill say so);
  the spend-ceiling line in `/kernel` says to restart `mk-serve`.
- Neo4j's anonymous usage report is off.
- Needs Hermes v0.23.0 and Docker on Linux x86_64. The stack is built on your machine; nothing is published.
- A think step whose instruction holds an earlier result treats it as data: a search snippet can no longer become the
  job the model is told to do. An instruction the teacher wrote stays a job.
- The agent is named `hermes` in memory on every Hermes profile; it used to take the profile's name.
- A Hermes that was already running picks up the token: `hermes plugins enable` now comes after `hermes memory setup`,
  and reloads the running gateway with nothing restarted. Otherwise `/kernel` says that phrases are off and how to turn
  them on.
- `run_phrase` takes a role given beside `phrase`, the block shows its arguments, and the service logs a refused call by
  its reason, never its words.
- `search_memory` results are fenced; a step ledger that could not be saved leaves the next start unsure; a step's
  failure is cut with its fence intact; tied message times can no longer make a session unbuildable.
- The scripts refuse every way Hermes could reach the real home, and the name scan stops on a failed grep and on a names
  file with no names. The plugin's tests each run on a temporary home.
- README: upgrading, a Hermes that was already running, what `/kernel` can show. `.env.example` lists the optional
  settings. A root `.dockerignore`.
- A person keeps a phrase by clicking a card, on the CLI and in a private Telegram chat. The keep comes only from
  Hermes's real `clarify` result, seen in a hook, never from what the model says, passes or reads back from a tool. It
  does not hold against a model that runs shell commands as your user with Hermes's `terminal` tool, which can read the
  plugin's token and call the service directly (as it already could to forge a plain yes). Everywhere else, and when
  there is no card, a plain "yes" keeps it as before. Throw it away and Change it close the draft, so a later "yes"
  cannot revive it while this Hermes runs.
