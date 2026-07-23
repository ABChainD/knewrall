# `neurons/` — Structured Knowledge Nodes

This folder holds the **source of truth** of the knowledge graph: one `.json` file
per node (a "Neuron"), plus an auto-generated `.md` companion for human reading and
clickable navigation in editors.

## Ownership
**Written almost exclusively by Knewrall** (the middleware/CRUD layer). Do **not**
hand-create or hand-edit files here. AI agents must go through the middleware API
(`propose_node`, `propose_link`); they never write to this folder directly.

## Layout (sub-foldering)
Files are sharded into 256 buckets by the **first two hex characters of the node UUID**
to keep any single directory small as the graph grows:

```
neurons/
  ab/
    ab086df7-5eb1-47cd-86e7-d0d882954d6b.json   <- source of truth
    ab086df7-5eb1-47cd-86e7-d0d882954d6b.md     <- generated companion
  2c/
    2c14bd7a-9ca8-4bdb-9b0e-629d277bcdc0.json
    ...
```

The shard is purely a function of the UUID, so a file's location is always
deterministic and never needs rebalancing.

## File format
Each `.json` follows [`schemas/master.schema.json`](../schemas/master.schema.json)
and is saved **deterministically** (sorted keys, sorted arrays) so identical data
produces byte-identical files — this keeps Git/Dropbox syncing conflict-free.
