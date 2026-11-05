"""Operator-invoked DataClean suggestion report — the hub's ONLY
DataClean hook point (integration spec 2026-11-05).

    cd backend
    python scripts/dataclean_report.py --synthetic --out /tmp/report.json
    python scripts/dataclean_report.py --input conversations.json --out r.json
    python scripts/dataclean_report.py --mongo [--tenant TENANT_ID] --out r.json

Modes:

* default (dry-run): runs the REAL books DataClean engine in-process
  (``lib/dataclean_suggest.build_report``) over a copy/sample of
  conversation identities. It makes NO network call, writes NOTHING to
  any database, and merges nothing — the JSON report is a suggestion
  list for a human operator to review.
* ``--live``: reserved for the future live-API pass. It is INERT while
  ``DATACLEAN_ENABLED`` is off (the default): the script explains that
  and exits 2 without calling anything. Live wiring beyond the client
  in ``lib/dataclean_client.py`` is deliberately not built in this
  pass (batch + dry-run only; no request-path hooks, no scheduler).

Inputs: ``--synthetic`` (labeled synthetic sample, see
lib/dataclean_suggest.synthetic_identities), ``--input`` (a JSON list
of conversation documents or identity dicts), or ``--mongo`` (reads
the conversations collection through the app's own db handle —
read-only ``find``; requires a reachable MongoDB).
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib import dataclean_client, dataclean_suggest  # noqa: E402


def _summarize(report: dict) -> dict:
    totals = report["totals"]
    return {
        "service": report["service"], "mode": report["mode"],
        "source": report["source"], "suggestions_only": True,
        "identities_in": totals["identities_in"],
        "tenants": totals["tenants"],
        "suggested_pairs": totals["suggested_pairs"],
        "review_pairs": totals["review_pairs"],
        "changes_total": totals["changes_total"],
        "changes_by_rule": totals["changes_by_rule"],
        "fragmented_groups": totals["fragmented_groups"],
        "fragmented_customer_keys": totals["fragmented_customer_keys"],
        "dropped_field_values": report["mapping"]["dropped_field_values"],
        "engine_note": report["engine"].get("engine_note"),
    }


async def _mongo_identities(tenant_id: str | None) -> list[dict]:
    import db as db_module

    database = db_module.get_db()
    query = {"tenant_id": tenant_id} if tenant_id else {}
    conversations = await database[db_module.CONVERSATIONS].find(
        query).sort("created_at", 1).to_list(length=100000)
    return [dataclean_suggest.conversation_to_identity(c)
            for c in conversations]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--synthetic", action="store_true",
                        help="labeled synthetic identity sample")
    source.add_argument("--input", metavar="FILE",
                        help="JSON list of conversations or identities")
    source.add_argument("--mongo", action="store_true",
                        help="read conversations from MongoDB (read-only)")
    parser.add_argument("--tenant", help="restrict --mongo to one tenant")
    parser.add_argument("--out", metavar="FILE",
                        help="write the full report JSON here")
    parser.add_argument("--engine-path",
                        help="books backend tree holding lib/dataclean")
    parser.add_argument("--live", action="store_true",
                        help="reserved: live API mode (inert while "
                             "DATACLEAN_ENABLED=0)")
    args = parser.parse_args(argv)

    if args.live:
        if not dataclean_client.enabled():
            print("INERT: DATACLEAN_ENABLED is off, so live DataClean "
                  "mode does nothing. Re-run without --live for the "
                  "offline dry-run report (real engine, in-process, "
                  "no network, no writes).")
            return 2
        print("Live mode is not wired in this pass: the hub ships the "
              "client (lib/dataclean_client.py) and this offline report "
              "only. No request-path hooks exist yet — see "
              "docs/DATACLEAN_INTEGRATION.md.")
        return 2

    if args.synthetic:
        identities = dataclean_suggest.synthetic_identities()
        label = "SYNTHETIC sample (lib.dataclean_suggest.synthetic_identities)"
    elif args.input:
        with open(args.input) as fh:
            raw = json.load(fh)
        # Items already in identity shape pass through untouched;
        # conversation documents are projected by the mapping.
        identities = [
            item if set(item) >= {"id", "tenant_id", "channel", "name",
                                  "email", "phone"}
            else dataclean_suggest.conversation_to_identity(item)
            for item in raw]
        label = f"JSON input file: {args.input}"
    else:
        identities = asyncio.run(_mongo_identities(args.tenant))
        label = "MongoDB conversations collection (read-only copy)"

    if args.tenant and not args.mongo:
        identities = [i for i in identities
                      if i.get("tenant_id") == args.tenant]

    try:
        report = dataclean_suggest.build_report(
            identities, label, engine_path=args.engine_path)
    except dataclean_suggest.EngineUnavailable as exc:
        print(f"ENGINE UNAVAILABLE: {exc}")
        return 3

    print(json.dumps(_summarize(report), indent=2, sort_keys=True))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(report, fh, indent=2, sort_keys=True, default=str)
        print(f"full report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
