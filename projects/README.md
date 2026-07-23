# `projects/` — External Projects (Read-Only Source of Knowledge)

A place to drop full sub-project folders or cloned code repositories that you want
the agents to **read and work in**, but that are **not part of the Knewrall knowledge
base**.

## The boundary rule
- Project contents may be used as a **source of knowledge** for the KB (agents can
  read them and distill insights into Neurons/Notes).
- Knewrall **scripts must NEVER modify** anything under `projects/` as a side effect
  of indexing or KB maintenance. The indexer ignores this folder entirely.
- Agents and users **may** freely use and modify project contents when explicitly
  doing development work in them — that's normal coding, separate from KB activity.

In short: **KB automation treats `projects/` as read-only; humans/agents doing
project work treat it as a normal workspace.**

## Sync
Project folders (often large repo clones with their own `.git`) are **not synced as
part of the KB** — this folder is git-ignored by default. Manage each project's
version control on its own.

## Layout
One subfolder per project; each keeps its own internal structure.
