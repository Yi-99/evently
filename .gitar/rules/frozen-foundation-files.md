---
title: "Frozen foundation files need a foundation: PR"
description: "Changes to the shared foundation (backend/docs/IMPLEMENTATION_PLAN.md §5 rule 1) must be deliberate: PR title starts with `foundation:` and the description says why."
slug: frozen-foundation-files
type: check
when: "A pull request changes any file in the frozen foundation list below"
actions: "Fail the check and comment unless the PR title starts with `foundation:`"
---

# Frozen foundation files

The lead owns the shared foundation; feature packages build on top of it and must not edit it
(backend/docs/IMPLEMENTATION_PLAN.md, §5 "Rules for every package", rule 1). The frozen set is:

- `backend/app/core/**` (settings, context, db, security, ids, deps)
- `backend/app/models/**`
- `backend/app/realtime/bus.py`, `backend/app/realtime/events.py`
- `backend/app/llm/**`
- `backend/app/agents/runtime.py`
- `backend/app/schemas/**`
- `backend/app/integrations/outreach.py` (signatures)
- `backend/app/providers/base.py`, `backend/app/transcription/base.py`
- `backend/app/main.py`
- `backend/tests/conftest.py`
- `backend/pyproject.toml`

## What to check

1. List the changed files that match the frozen set.
2. If there are none, pass silently.
3. If there are some and the PR title starts with `foundation:` (case-insensitive), pass, but
   confirm the PR description explains *why* the foundation had to change and which packages are
   affected. If the description is missing that, post a comment asking for it (do not fail).
4. Otherwise **fail** the check with a comment that lists the frozen files touched and says:
   "Foundation files are frozen for feature packages. Either drop these changes and describe the
   needed change in your PR description for the lead, or retitle the PR `foundation: <summary>`."

## Allowed without the prefix

- Pure observability hooks in `backend/app/agents/runtime.py` and `backend/app/llm/anthropic_client.py` that only
  add `app.observability.*` calls, keep every signature and do not change control flow (package 16).
- Dependency *additions* to `backend/pyproject.toml` are still a foundation change: require the prefix and a
  sentence naming the new dependency and why it is needed (rule 6).
