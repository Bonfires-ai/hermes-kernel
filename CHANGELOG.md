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
