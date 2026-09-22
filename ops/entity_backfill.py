#!/usr/bin/env python3
"""Build Ace's entity layer from the memory he already has. Dry run by default.

  python -m ops.entity_backfill                      # preview; writes NOTHING
  python -m ops.entity_backfill --apply              # do it
  python -m ops.entity_backfill --apply              # again: must be a no-op in numbers
  python -m ops.entity_backfill --report out.json    # machine-readable alongside stdout
  python -m ops.entity_backfill --rollback --yes-drop-entity-layer

WHY DRY RUN IS THE DEFAULT. This reads every one of Ace's ~8600 source records. It writes
only to new `ace_*` tables and never to `facts`, `turns`, `daybank_items`, `summaries` or
the profile — and it PROVES that with a before/after row-count and content-hash manifest
per original table, computed the same way Codex's independent checker computes it. A
default that writes would mean the first thing anyone did with this tool was the
irreversible thing.

The dry run is not a separate simulation. Postgres DDL and DML are both transactional, so
it creates the layer, runs every phase for real, prints the numbers and rolls the whole
transaction back. The preview and the apply are the same code.

NO MODEL CALLS. NO NETWORK. Resolution is exact normalized alias equality against an
in-memory dictionary, and that is the whole of it.
"""

import argparse
import json
import os
import sys
from pathlib import Path

# Run from anywhere: `-m ops.entity_backfill` puts the repo root on sys.path already, but
# a direct `python ops/entity_backfill.py` does not.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ace2.backend import entities, entity_migrate    # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ops.entity_backfill",
        description=("Build the durable entity layer from Ace's existing memory. "
                     "Dry run by default; --apply is the only thing that writes. "
                     "Originals are never modified, and the report proves it."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Originals (facts, turns, daybank_items, summaries, the profile) are "
                "never rewritten, re-statused or deleted. Resolution is exact "
                "normalized alias equality only — no fuzzy matching, no model call. "
                "Unresolved sources stay queryable and are counted in the report."))
    p.add_argument("--apply", action="store_true",
                   help="actually write. Without this nothing is persisted.")
    p.add_argument("--rollback", action="store_true",
                   help="drop ONLY this layer's tables (requires --yes-drop-entity-layer)")
    p.add_argument("--yes-drop-entity-layer", action="store_true",
                   help="confirm --rollback")
    p.add_argument("--report", metavar="PATH", default=None,
                   help="also write the full report as JSON to PATH")
    p.add_argument("--json", action="store_true",
                   help="print the JSON report to stdout instead of the text one")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="stop after N sources per corpus (for a quick look)")
    p.add_argument("--corpus", default=",".join(entities.CORPORA),
                   help="comma-separated subset of: " + ",".join(entities.CORPORA))
    p.add_argument("--graph-seeds", dest="graph_seeds", action="store_true", default=True,
                   help="seed review rows from the newest graph_cache row (default on)")
    p.add_argument("--no-graph-seeds", dest="graph_seeds", action="store_false",
                   help="skip the graph seed phase entirely")
    p.add_argument("--resume", action="store_true",
                   help=("start each corpus from its checkpoint instead of rescanning. "
                         "A full rescan is the default because it is idempotent and it "
                         "is what makes a second --apply a provable no-op."))
    p.add_argument("--max-new-entities", type=int, default=0, metavar="N",
                   help="hard cap on entities created in one run (0 = no cap)")
    p.add_argument("--database-url", default=None,
                   help=("connect here instead of $DATABASE_URL. Intended for a "
                         "disposable local copy; never point this at production."))
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if not entities.enabled():
        print("DATABASE_URL is not set — nothing to read. Nothing was written.")
        return 2

    if args.rollback:
        out = entity_migrate.rollback(confirm=args.yes_drop_entity_layer)
        print("ROLLBACK — these tables and NOTHING else:")
        for t in out["tables"]:
            print("    " + t)
        print("  facts / turns / daybank_items / summaries / the profile are NOT in that "
              "list and are not touched.")
        if out.get("error"):
            print("  " + out["error"])
            return 2
        print("  dropped: %s" % ", ".join(out.get("dropped") or []))
        si = out.get("source_integrity") or {}
        print("  originals unchanged: %s" % si.get("ok"))
        for d in si.get("drift") or []:
            print("  DRIFT: %s" % d)
        return 0 if si.get("ok") else 1

    rep = entity_migrate.run(
        apply=args.apply,
        corpora=[c.strip() for c in (args.corpus or "").split(",") if c.strip()],
        limit=args.limit or None,
        graph_seeds=args.graph_seeds,
        resume=args.resume,
        max_new_entities=args.max_new_entities or 0,
        report_path=args.report)
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(entity_migrate.render(rep))
        if not args.apply:
            print("\n(dry run — nothing was written. Re-run with --apply to keep it.)")
    return 0 if rep.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
