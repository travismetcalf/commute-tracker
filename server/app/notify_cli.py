"""Command-line face of notify.send(), for the host scripts.

    printf '%s' "$BODY" | python3 -m app.notify_cli brief \
        --subject "Commute brief — Wed Sep 17" --from commute@… --to commute@…

Exit code is the contract: 0 when nothing we were asked to deliver failed, 1
when something did, 2 for a usage error. A shell caller keys its dead-man
marker off that, which is how brief.sh already treated its email send —
deliberately not swallowed with `|| true`.

A fully disabled notification exits 0. The job ran and correctly did nothing;
that is the same reasoning poll.sh uses when it touches its marker on a
deliberate window skip.
"""

import argparse
import sqlite3
import sys

from app import notify
from app.db import get_conn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="notify_cli")
    ap.add_argument("type_key")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--from", dest="from_addr")
    ap.add_argument("--to", dest="to_addr")
    ap.add_argument("--html-file")
    args = ap.parse_args(argv)

    if args.type_key not in notify.BY_KEY:
        print(f"unknown notification type: {args.type_key}", file=sys.stderr)
        return 2

    body = sys.stdin.read()
    html = None
    if args.html_file:
        try:
            with open(args.html_file, encoding="utf-8") as f:
                html = f.read()
        except OSError as e:
            print(f"cannot read --html-file: {e}", file=sys.stderr)
            return 2

    # Resolve every channel's preference, all within this ONE connection, then
    # let the connection close BEFORE anything is actually dispatched. That
    # ordering is deliberate, not just tidy: the previous shape called
    # notify.send() — which reads-then-delivers per channel — as a single
    # unit inside this try block. For a multi-channel type, a DatabaseError
    # raised partway through (email's SELECT already succeeded and the mail
    # already left; push's SELECT still to come) propagated out from under
    # an already-partial delivery, and the fallback below re-ran everything
    # blind — RE-SENDING the email that had already gone out. Splitting
    # notify.resolve() (DB-only, no delivery) from notify.dispatch()
    # (delivery-only, no DB access for EMAIL) makes that structurally
    # impossible: dispatch() below is called EXACTLY ONCE per invocation,
    # always after the connection is closed, using decisions that are either
    # fully resolved or fully blind — never a partial mix of both. See
    # notify.resolve()'s and notify.dispatch()'s docstrings, and
    # test_no_duplicate_delivery_when_a_later_channel_selects_raises in
    # test_notify_fail_open.py for the regression this prevents.
    #
    # The try block below is still the ONLY scope that covers all three ways
    # a broken database can surface: sqlite3.connect (db.py:288, e.g. a
    # missing data directory), the WAL pragma right after it (db.py:290,
    # e.g. a corrupt or locked file), and the SELECT inside notify.enabled()
    # that notify.resolve() calls per channel (db.py:304, e.g. a headless
    # file with no settings table). connect() is lazy — it does not raise
    # for a corrupt or locked file, the pragma does — so a guard placed
    # anywhere narrower misses a raise site.
    #
    # Caught as sqlite3.DatabaseError, never sqlite3.OperationalError:
    # corruption (SQLITE_NOTADB, SQLITE_CORRUPT) raises the bare DatabaseError
    # parent, for which isinstance(exc, sqlite3.OperationalError) is False.
    # An OperationalError-only guard would fail closed on exactly the failure
    # most likely to coincide with the alert being sent.
    try:
        with get_conn() as conn:
            decisions = notify.resolve(conn, args.type_key)
        blind = False
    except sqlite3.DatabaseError as e:
        print(f"notify_cli: database unreadable ({type(e).__name__}: {e}) — "
              f"dispatching {args.type_key!r} blind", file=sys.stderr)
        decisions = notify.blind_decisions(args.type_key)
        blind = True

    result = notify.dispatch(args.type_key, decisions, args.subject, body, html,
                             from_addr=args.from_addr, to_addr=args.to_addr,
                             blind=blind)

    print(" ".join(f"{k}={v}" for k, v in sorted(result.items())))
    return 1 if notify.failed(result) else 0


if __name__ == "__main__":
    sys.exit(main())
