# `archive/` — Static File Archive

Long-term storage for **static, non-multimedia files** — text or binary — that the
knowledge base must keep on record and reference from its memories (notes/neurons).
Think: source documents, contracts, exports, datasets, reference PDFs-as-data, etc.

## The static guarantee
Files here are **STATIC**. Knewrall may **add, move, or copy** files into this folder
from outside sources, but the indexer **never reads, parses, or modifies their
contents**. Once a file is archived it is treated as immutable bytes; only its
existence and metadata are tracked by the KB.

## Ownership
**Written exclusively by Knewrall** (move/copy in only). To submit material for
archival, place it in [`../workdesk/`](../workdesk/README.md) and let the system
decide what to archive.

## Layout (sub-foldering)
Sharded by **archival date** (`YYYY/MM/`) to keep directories small over time:

```
archive/
  2026/
    06/
      2026-q2-report.pdf
```

## Difference from `media/`
- `media/`  → multimedia attachments (images, audio, video) for display/embedding.
- `archive/` → static documents and binaries kept as immutable records.
