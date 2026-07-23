# `notes/` — Structured Markdown Notes

Distilled, link-enriched Markdown notes that the system produces from raw input.
After Knewrall extracts entities, it writes the note here and appends standard
markdown links back to the relevant Neurons for clickable navigation.

## Ownership
**Written mostly by Knewrall.** Raw, unstructured material you want to capture
should be dropped in [`../workdesk/`](../workdesk/README.md) instead; the system
processes it and produces the finished note here.

## Layout (sub-foldering)
Notes are sharded by **ingest date** (`YYYY/MM/`) to cap the number of files per
directory over long-term use:

```
notes/
  2026/
    06/
      2026-06-17-meeting-recap.md
```

## Linking
Notes link to Neurons with standard relative markdown links, e.g.
`[Canonical Name](../../../neurons/ab/<uuid>.md)` (depth depends on the note's date
folder). The indexer scans `notes/**/*.md` recursively and records these links as
edges in the ephemeral index.
