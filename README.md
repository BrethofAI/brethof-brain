# Brethof Brain

Persistent memory for AI agents, running on your own machine.

**Status: pre-release.** The code works end to end on our test installs; the
first public release, with install instructions, is not out yet.

## What runs

Three containers, on your computer or your own server:

- **hub** (`hub/`): the one door. It pairs devices, checks the licence,
  composes the memory for each prompt, and connects to your model provider.
  Its model door adds the memory to an app's own traffic: point the app's
  base URL at the hub and the app keeps its own plan and sign-in.
- **storage** (`core/`, `api/`): Postgres with pgvector, in an encrypted
  directory whose passphrase is yours. It holds the chat archive, records,
  notes and playbooks.
- **embedder** (`embedder/`): EmbeddingGemma-300m, served locally.

`deploy/local/` holds the compose file and the installer.

## The licence part

The hub's licence and device module (the gate) ships compiled, one native
module per chip type, in `gate/<arch>/`. Its source is not published.

## Licence

Copyright 2026 Brethof AI. Elastic License 2.0 (ELv2). See [LICENSE](LICENSE). This is a source-available
licence: you may use, copy and run the software for any purpose, but you may
not offer it to others as a hosted or managed service, and you may not change,
disable or circumvent the licence-key part.

## Tests

    pip install -r requirements-dev.txt
    pytest -m "not slow"
