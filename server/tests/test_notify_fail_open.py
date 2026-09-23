"""notify_cli's guard around a database that cannot even answer "is this
switch on" — task 6 of the settings redesign plan.

Three fixtures, not one. Each is invisible to the other two's failure mode
(measured by probing sqlite3 directly, traceback last-frame included):

    unopenable  tmp_path/no-such-dir/commute.db  -> raises at db.py:288,
                sqlite3.connect (OperationalError: unable to open database
                file — the parent directory does not exist)
    corrupt     a file of garbage bytes           -> raises at db.py:290,
                the WAL pragma (bare DatabaseError, NOT OperationalError:
                "file is not a database" — isinstance(exc, OperationalError)
                is False here, which is why the guard must catch
                sqlite3.DatabaseError and not its OperationalError subclass)
    headless    a real sqlite file, one unrelated
                table, no `settings` table         -> raises at db.py:304,
                the SELECT inside notify.enabled() (OperationalError: no
                such table: settings)

All three via monkeypatch.setattr(db_mod, "DB_PATH", ...), per conftest.py's
documented reason for patching the constant rather than the env var.
"""

import io
import sqlite3

import pytest

from app import db as db_mod
from app import notify
from app.db import set_setting
from app.notify_cli import main


@pytest.fixture(params=["unopenable", "corrupt", "headless"])
def broken_db(request, tmp_path, monkeypatch):
    """Points db_mod.DB_PATH at a file that raises somewhere inside
    `with get_conn() as conn:` — at one of three different lines depending
    on `request.param`. Returns the param name so a test can assert on which
    kind it got, if it needs to."""
    kind = request.param
    if kind == "unopenable":
        path = tmp_path / "no-such-dir" / "commute.db"
        assert not path.parent.exists()  # the mutation (a missing dir) landed
    elif kind == "corrupt":
        path = tmp_path / "corrupt.db"
        path.write_bytes(b"garbage bytes, not a real sqlite file, 0123456789")
        assert path.read_bytes() != b""  # the mutation (writing the file) landed
    elif kind == "headless":
        path = tmp_path / "headless.db"
        c = sqlite3.connect(str(path))
        c.execute("CREATE TABLE unrelated (id INTEGER)")
        c.commit()
        c.close()
        tables = sqlite3.connect(str(path)).execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        assert ("settings",) not in tables  # the mutation landed: no settings table
    else:  # pragma: no cover - guards a typo in `params` above
        raise AssertionError(f"unhandled broken_db kind: {kind}")

    monkeypatch.setattr(db_mod, "DB_PATH", str(path))
    return kind


@pytest.fixture
def email_stub(monkeypatch):
    """A stand-in for notify._send_email that CAN report failure.

    A stub that always returns "sent" cannot discriminate a delivered blind
    alert from a silently lost one — that is exactly the shape of stub that
    let a prior version of this task's blind branch pass every test while
    reporting a lost alert as `email=sent`, exit 0. `state["verdict"]` lets a
    test choose what the send "did", and `calls` lets it check whether the
    real body reached the sender.
    """
    calls = []
    state = {"verdict": "sent"}

    def fake_send_email(subject, text, html=None, from_addr=None, to_addr=None):
        calls.append({"subject": subject, "text": text, "html": html,
                      "from": from_addr, "to": to_addr})
        return state["verdict"]

    monkeypatch.setattr(notify, "_send_email", fake_send_email)
    return calls, state


def _run(monkeypatch, body, *args):
    monkeypatch.setattr("sys.stdin", io.StringIO(body))
    return main(list(args))


# --- the guard engages, for all three raise sites -------------------------

def test_system_email_goes_out_under_all_three_broken_db_fixtures(
        broken_db, email_stub, monkeypatch, capsys):
    """`system` defaults to ON, so a healthy blind dispatch must actually
    send. Mutation that makes this RED: no guard at all (the raise
    propagates and the alert is never sent, in this fixture, or the process
    itself crashes without printing anything) — or a guard that misses this
    fixture's specific raise site (e.g. `except sqlite3.OperationalError`,
    which the `corrupt` fixture's bare DatabaseError sails past).

    Also pins two things nothing else in this file checked: the "(blind)"
    key suffix in the printed result line (mutation that makes THIS part
    red: `key = f"{ch}(blind)"` -> `key = ch` — nothing else here would
    notice a blind dispatch losing its only marker distinguishing it from a
    normal one), and the diagnostic stderr line notify_cli prints when it
    falls back (mutation: deleting that print call)."""
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "system", "--subject", "S")
    captured = capsys.readouterr()
    assert len(calls) == 1, f"expected one blind email send under {broken_db}"
    assert exit_code == 0
    assert "email(blind)=sent" in captured.out
    assert "database unreadable" in captured.err
    assert "dispatching 'system' blind" in captured.err


def test_brief_stays_silent_under_all_three_broken_db_fixtures(
        broken_db, email_stub, monkeypatch):
    """`brief`'s email default is "0". A blind dispatch built from
    defaults must still respect a default of off — "fail open" must not
    mean "fail open for everything". Mutation that makes this RED: treating
    every notification type as ON on the blind path."""
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "brief", "--subject", "S")
    assert calls == []
    assert exit_code == 0


def test_push_never_attempts_delivery_on_the_blind_path(
        broken_db, email_stub, monkeypatch):
    """`anomaly`'s push default is "1" — if the blind path dispatched by
    default alone with no channel restriction, it would try to push. But
    push.send_alert opens its OWN get_conn() (push.py:87), so a blind push
    would just hit the identical broken database. Using a spy rather than
    relying on `email_stub` alone: this proves push.send_alert is never
    CALLED, not merely that its result is ignored.

    `email_stub` is included even though `anomaly`'s email default ("0")
    means this test never exercises it today — every other test in this
    file uses it, and without it this test would fall through to the REAL
    scripts/notify-email.sh shim the moment anomaly's email default (or the
    blind-decision logic) ever changed. A test that shells out by accident
    is exactly the kind of thing that should not depend on today's
    defaults staying put.

    F5: `push.configured()` is False in this test environment (no APNS key
    on disk), which ITSELF stops dispatch() from ever calling
    push.send_alert — regardless of whether `_BLIND_CHANNELS` correctly
    excludes push. Without pinning `configured()` True, this test's
    precondition is never satisfied: `push_calls == []` would hold even if
    `_BLIND_CHANNELS` were mutated to include PUSH, because dispatch() would
    just take the "unconfigured" branch instead of ever reaching
    push.send_alert. Forcing `configured()` True makes the spy the only
    thing standing between a broadened `_BLIND_CHANNELS` and an actual call."""
    from app import push

    monkeypatch.setattr(push, "configured", lambda: True)
    push_calls = []

    def spy_send_alert(title, body, extra=None):
        push_calls.append((title, body, extra))
        return 1

    monkeypatch.setattr(push, "send_alert", spy_send_alert)
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "anomaly", "--subject", "S")
    assert push_calls == []
    assert calls == []  # anomaly's email default is off too
    assert exit_code == 0


# --- the guard does NOT engage when the database is fine -------------------

def test_stored_zero_still_silences_system_when_db_is_readable(
        conn, email_stub, monkeypatch):
    """A readable database with the switch off must still silence `system`
    — fail-open must not become an unconditional bypass of a real, readable
    preference. Mutation that makes this RED: `send_blind` (or an
    unconditional fallback) firing regardless of whether the normal path
    already succeeded."""
    set_setting(conn, "notify_system_email", "0")
    conn.commit()
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "system", "--subject", "S")
    assert calls == []
    assert exit_code == 0


def test_stored_one_still_sends_system_when_db_is_readable(
        conn, email_stub, monkeypatch):
    """The other half of the control: a readable database with the switch
    explicitly on must send through the NORMAL path (not just because the
    catalog default happens to also be "1")."""
    set_setting(conn, "notify_system_email", "1")
    conn.commit()
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "system", "--subject", "S")
    assert len(calls) == 1
    assert exit_code == 0


# --- the trap: a stub that only ever says "sent" would hide both of these --

def test_blind_failure_is_not_reported_as_success(
        broken_db, email_stub, monkeypatch, capsys):
    """The obvious six-line blind branch — "we decided to send, so report
    sent" — passes every other test in this file while turning a silently
    lost alert into a green `email=sent`, exit 0. That is strictly worse
    than today's bug, where a non-zero exit at least exists to be noticed.
    This is the one test that stub design cannot pass by accident."""
    calls, state = email_stub
    state["verdict"] = "failed"
    exit_code = _run(monkeypatch, "probe", "system", "--subject", "S")
    out = capsys.readouterr().out
    assert len(calls) == 1  # delivery WAS attempted
    assert exit_code == 1
    assert "sent" not in out
    assert "failed" in out


def test_blind_path_preserves_body_subject_from_and_to(
        broken_db, email_stub, monkeypatch):
    """A second way the six-line shortcut goes wrong: dispatching without
    passing every argument through faithfully. Mutation that makes the body
    half RED: `_send_email(subject, "", ...)` or similar. `--from` matters
    beyond this test file too — brief.sh's sender discipline (see its own
    comments) depends on --from actually reaching the sender, and a blind
    path that silently dropped or swapped it would misfile mail with no
    error anywhere. `calls[0]` already recorded subject/from/to before this
    test started checking them — only `text` was pinned previously."""
    calls, _ = email_stub
    body_text = "commute-tracker poller failed 3x in a row:\n\nreal traceback here"
    monkeypatch.setattr("sys.stdin", io.StringIO(body_text))
    exit_code = main(["system", "--subject", "commute-tracker poller FAIL (3 consecutive)",
                      "--from", "alerts@example.com", "--to", "ops@example.com"])
    assert exit_code == 0
    assert calls[0]["text"] == body_text
    assert calls[0]["text"] != ""
    assert calls[0]["subject"] == "commute-tracker poller FAIL (3 consecutive)"
    assert calls[0]["from"] == "alerts@example.com"
    assert calls[0]["to"] == "ops@example.com"


# --- the blind rule must be catalog-driven, not type-name-driven -----------

def test_weekly_summary_email_goes_out_under_all_three_broken_db_fixtures(
        broken_db, email_stub, monkeypatch):
    """`weekly_summary` is the one catalog entry where a data-driven blind
    rule and a `type_key == "system"` special case DISAGREE: its channels
    are (EMAIL, PUSH), its email default is "1", and it carries no stored
    row in the real DB — so it must send on the blind path exactly like
    `system` does, but for a completely different type_key. Every other
    test in this file picks a type whose blind-path outcome is identical
    under a `system`-only hardcode (system sends; brief and anomaly stay
    silent either way) — this is the one that actually pins the rule to
    catalog data. Mutation that makes this RED: `blind_decisions` (or
    dispatch's blind branch) special-cased on `type_key == "system"`."""
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "weekly_summary", "--subject", "S")
    assert len(calls) == 1, f"expected one blind email send under {broken_db}"
    assert exit_code == 0


# --- the resolve/dispatch split: no duplicate delivery on a late raise -----

def test_no_duplicate_delivery_when_a_later_channel_selects_raises(
        conn, email_stub, monkeypatch):
    """The regression this task's resolve()/dispatch() split exists to
    prevent. `weekly_summary` is multi-channel (EMAIL then PUSH, in that
    declared order). Before the split, notify_cli wrapped a single
    read-then-deliver call (notify.send()) in the guard: if the DATABASE
    ERROR arrived after email's preference had already been read (and the
    mail already sent) but before push's read completed, the exception
    still propagated out of that one call, the guard's fallback ran
    send_blind() as though NOTHING had been delivered yet, and the email
    went out a second time — an identical duplicate, not a corrected retry.

    Reproduced here by making the per-channel read raise partway through
    notify.resolve()'s iteration over (EMAIL, PUSH) — the same shape the
    real bug needs (successful reads before the failure, at least one
    successful delivery already committed), without needing to reproduce
    real WAL-checkpoint timing. Guarded: asserts the read actually reached
    PUSH (i.e. EMAIL's read really did complete) before concluding anything
    about duplicate sends."""
    calls_to_enabled = []
    real_enabled = notify.enabled

    def flaky_enabled(conn, type_key, channel):
        calls_to_enabled.append(channel)
        if channel == notify.PUSH:
            raise sqlite3.DatabaseError("simulated corruption mid-resolve")
        return real_enabled(conn, type_key, channel)

    monkeypatch.setattr(notify, "enabled", flaky_enabled)
    calls, _ = email_stub
    exit_code = _run(monkeypatch, "probe", "weekly_summary", "--subject", "S")

    assert calls_to_enabled == [notify.EMAIL, notify.PUSH], (
        "the read must have reached EMAIL (and succeeded) before PUSH raised "
        "— otherwise this isn't reproducing the bug's precondition")
    assert len(calls) == 1, (
        f"expected exactly one email delivery, got {len(calls)}: {calls}")
    assert exit_code == 0
