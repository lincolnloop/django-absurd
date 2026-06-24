---
description:
  Refresh the project's durable knowledge — distill docs/WHY.md, then retire consumed
  specs/plans into docs/HISTORY.md.
---

Run the two project skills in order — **capture before delete**:

1. Invoke the **`capture-why`** skill: read `docs/specs/` + `docs/plans/` and the code,
   and refresh `docs/WHY.md` with the durable "why" (intent + load-bearing reasons).
2. Then invoke the **`archive-specs`** skill: retire specs/plans that are now obsolete
   or fully captured, recording each in `docs/HISTORY.md` with an `origin/main` SHA link
   before deleting it.

The order is the safety mechanism: `capture-why` must run first so a doc's "why" is
preserved before `archive-specs` removes the file. `archive-specs`'s confirm gate still
applies — never delete anything without explicit approval.
