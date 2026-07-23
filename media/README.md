# `media/` — Multimedia Attachments

Object storage for multimedia referenced by the knowledge base: images, audio,
video, PDFs and similar rich attachments.

## Ownership
**Written almost exclusively by Knewrall.** When the system needs to retain a piece
of media referenced by a note or neuron, it copies/moves it here. Do not drop loose
files here by hand — use [`../workdesk/`](../workdesk/README.md).

## Layout (sub-foldering)
Sharded by **ingest date** (`YYYY/MM/`) so directories stay small over time:

```
media/
  2026/
    06/
      diagram.png
```

## Indexing
Media files are **tracked/referenced** by the KB (a note or neuron points to them)
but their **contents are never parsed or modified** by the indexer. Treat stored
media as immutable once filed.
