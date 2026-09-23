"""Notification catalog — the single source of truth for what this app can tell
you about, on which channels, and whether you want it.

One list drives four things that would otherwise be four hand-maintained
copies: the settings keys, their validators, the /api/v1/config payload the
iOS app renders from, and the web settings page's notification matrix —
settings.html's `{% for n in notifications %}` loop renders whatever
main.py's settings_page() built from notify.payload(), by way of
_switch_columns(). Adding a notification type is one entry here; it reaches
both the iOS/API surface and the web page with no other edit.

CHANNEL AVAILABILITY IS A FACT ABOUT WHERE THE SENDER RUNS, not a preference.
Email leaves through scripts/notify-email.sh, which .dockerignore keeps out of
the container image — so anything fired from a web request can only push.
Declaring that here is what stops the UI rendering a toggle that would silently
do nothing.
"""

import os
import subprocess
import sys
import tempfile
import types
from collections.abc import Callable, Mapping
from dataclasses import dataclass

EMAIL = "email"
PUSH = "push"

_SHIM = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "notify-email.sh")

#: Verdicts that mean "we were asked to deliver and did not". 'disabled' is the
#: user's choice and 'unconfigured' is a deployment state the job cannot fix —
#: neither should fail a scheduled run or hold back its heartbeat.
_FAILURE_VERDICTS = ("failed", "unavailable")


@dataclass(frozen=True)
class NotificationType:
    key: str
    label: str
    help: str
    channels: tuple[str, ...]
    defaults: Mapping[str, str]      # channel -> "0" | "1"


CATALOG: tuple[NotificationType, ...] = (
    NotificationType(
        key="brief",
        label="Morning brief",
        help="Weekdays 07:15 — today's picture and the best departure window",
        channels=(EMAIL, PUSH),
        defaults=types.MappingProxyType({EMAIL: "0", PUSH: "0"}),
    ),
    NotificationType(
        key="weekly_summary",
        label="Weekly summary",
        help="Saturday mornings — last week's trips, times and best/worst day",
        channels=(EMAIL, PUSH),
        defaults=types.MappingProxyType({EMAIL: "1", PUSH: "0"}),
    ),
    NotificationType(
        key="trip_completed",
        label="Trip completed",
        help="How long the drive took, against what was anticipated and your average",
        channels=(PUSH,),
        defaults=types.MappingProxyType({PUSH: "1"}),
    ),
    NotificationType(
        key="anomaly",
        label="Commute unusually bad",
        help="Fires only inside the alert days and windows",
        channels=(EMAIL, PUSH),
        defaults=types.MappingProxyType({EMAIL: "0", PUSH: "1"}),
    ),
    # `system` governs the poller-failure alert (poll.sh, three consecutive
    # failures) ONLY. Backup and restore-test failure mail is deliberately
    # NOT wired to this switch and never will be by extending this entry:
    # neither script defines DB_PATH (`grep -rn DB_PATH scripts/` proves it —
    # only poll.sh, brief.sh, weekly-summary.sh, derive-trips.sh do), so
    # routing them through this catalog would make each inherit db.py's
    # container-default DB_PATH and hit SQLITE_CANTOPEN on every host run,
    # and both scripts are emailing about disaster in the very database a
    # dispatcher would need to open to read this preference. See
    # docs/superpowers/plans/2026-09-19-settings-redesign.md, Task 6.
    NotificationType(
        key="system",
        label="Poller failing",
        help="Three poll failures in a row — email only, because a push "
             "saying the server is down depends on the server. (Backup and "
             "restore-test failures always email on their own path, "
             "regardless of this switch.)",
        channels=(EMAIL,),
        defaults=types.MappingProxyType({EMAIL: "1"}),
    ),
)

BY_KEY: dict[str, NotificationType] = {n.key: n for n in CATALOG}


def setting_key(type_key: str, channel: str) -> str:
    return f"notify_{type_key}_{channel}"


def defaults() -> dict[str, str]:
    """Every derived settings key with its shipping default.

    These are NOT seeded into db.DEFAULT_SETTINGS. Reading with a fallback (the
    ALERT_DEFAULTS pattern) means changing a default in code actually takes
    effect for anyone who never touched the switch, instead of being frozen at
    whatever the row was seeded with.
    """
    return {setting_key(n.key, ch): v
            for n in CATALOG for ch, v in n.defaults.items()}


def _is_bool(v: str) -> bool:
    return v in ("0", "1")


def validators() -> dict[str, Callable[[str], bool]]:
    """Merged into main._SETTING_VALIDATORS. A strict 0|1 TYPE, not a
    whitelist entry — settings values reach shell arithmetic in smoke.sh."""
    return {k: _is_bool for k in defaults()}


def enabled(conn, type_key: str, channel: str) -> bool:
    """The single gate every sender passes through.

    An unknown type, or a channel that type does not support, is False rather
    than an error: a stored row must never conjure a channel the sender cannot
    actually reach.
    """
    n = BY_KEY.get(type_key)
    if n is None or channel not in n.channels:
        return False
    # Function-local on purpose: db.py must stay importable without
    # notify.py, and a module-level import here would make the two mutually
    # dependent the moment anything in db wants a catalog value.
    from app.db import get_setting
    return get_setting(conn, setting_key(type_key, channel),
                       n.defaults[channel]) == "1"


def payload(conn) -> list[dict]:
    """Catalog plus current values, for a client that renders the matrix from
    server data instead of a hardcoded copy of this list.

    `switches` carries the derived settings key alongside each channel. That is
    deliberate: without it a client rebuilds `notify_{key}_{channel}` itself,
    once in Jinja and once in Swift, which is exactly the duplication this
    catalog exists to remove. The server knows the key; it sends the key.
    """
    return [{"key": n.key,
             "label": n.label,
             "help": n.help,
             "channels": list(n.channels),
             "switches": [{"channel": ch,
                           "setting_key": setting_key(n.key, ch),
                           "enabled": enabled(conn, n.key, ch),
                           "default": n.defaults[ch]}
                          for ch in n.channels]}
            for n in CATALOG]


def _send_email(subject: str, text: str, html: str | None = None,
                from_addr: str | None = None, to_addr: str | None = None) -> str:
    """'sent' | 'unavailable' | 'failed'.

    The shim lives in scripts/, which .dockerignore keeps out of the image, so
    a caller inside the container gets 'unavailable' rather than a silent
    success. The body rides stdin because interpolating it into a command line
    breaks on apostrophes — the same reason brief.sh pipes rather than
    substitutes.

    NOTE: scripts/notify-email.sh itself exits 0 when no notifier is
    configured (NOTIFY_CMD unset and no canonical sender found) — it
    deliberately no-ops rather than fail every caller on a host with no mail
    setup. So a "sent" verdict here can mean the shim decided nothing needed
    to leave the machine, not that mail was actually delivered. Surprising,
    worth knowing before trusting this as proof of delivery.
    """
    if not os.path.exists(_SHIM):
        return "unavailable"
    cmd = [_SHIM]
    if from_addr:
        cmd += ["--from", from_addr]
    if to_addr:
        cmd += ["--to", to_addr]
    cmd += ["--subject", subject, "--text-file", "-"]
    tmp = None
    try:
        if html:
            # --text-file already owns stdin, so HTML needs a real file.
            tmp = tempfile.NamedTemporaryFile("w", suffix=".html",
                                              delete=False, encoding="utf-8")
            tmp.write(html)
            tmp.close()
            cmd += ["--html-file", tmp.name]
        r = subprocess.run(cmd, input=text, text=True,
                           capture_output=True, timeout=60)
        return "sent" if r.returncode == 0 else "failed"
    except (OSError, subprocess.SubprocessError):
        return "failed"
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass


def resolve(conn, type_key: str) -> dict[str, bool]:
    """Read every channel's current enabled state for a notification type, in
    ONE pass, using the given connection — and nothing else. No delivery
    happens in here.

    Deliberately split from `dispatch()`, below: a caller that must guard a
    database it cannot fully trust (notify_cli's DatabaseError guard) needs
    to resolve every channel's preference — and only THEN close its
    connection — before delivering anything. Combining "decide" and
    "deliver" into one function that also holds a connection open (the
    shape `send()` used to have, and the shape notify_cli used to wrap in a
    single guarded `with get_conn()`) meant a DatabaseError raised partway
    through a multi-channel type — email's SELECT succeeds and the mail
    already leaves, then push's SELECT raises — propagated out from under an
    already-partial delivery. The guard's fallback then re-ran the whole
    thing blind, RE-SENDING the email that had already gone out. See
    `dispatch()`'s docstring for the other half of this split, and
    `test_no_duplicate_delivery_when_a_later_channel_selects_raises` in
    test_notify_fail_open.py for the regression this prevents.
    """
    n = BY_KEY.get(type_key)
    if n is None:
        raise KeyError(f"unknown notification type: {type_key}")
    return {ch: enabled(conn, type_key, ch) for ch in n.channels}


#: Channels the blind path (blind_decisions/dispatch, below) is allowed to
#: use. Restricted to channels that can deliver without opening the database
#: at all — today that is EMAIL only, because PUSH cannot: push.send_alert()
#: opens its OWN get_conn() for the devices table (push.py:87), so a "blind"
#: push attempt would just hit the identical broken connection this path
#: exists to route around, not skip past it. This is a fact about where push
#: keeps its recipient list, not a preference — express it here, once,
#: rather than as a per-type exception scattered through blind_decisions.
_BLIND_CHANNELS = (EMAIL,)


def blind_decisions(type_key: str) -> dict[str, bool]:
    """The `resolve()`-equivalent for when there is no database connection
    worth trusting: catalog DEFAULTS instead of a stored preference,
    restricted to `_BLIND_CHANNELS`.

    A channel outside `_BLIND_CHANNELS` is simply absent from the returned
    dict. `dispatch()` treats an absent channel as "never even attempted to
    decide this one" — omitted from the result entirely — not as
    "disabled", which is a real, stored preference's answer.
    """
    n = BY_KEY.get(type_key)
    if n is None:
        raise KeyError(f"unknown notification type: {type_key}")
    return {ch: n.defaults[ch] == "1" for ch in n.channels if ch in _BLIND_CHANNELS}


def dispatch(type_key: str, decisions: Mapping[str, bool], subject: str, text: str,
            html: str | None = None, from_addr: str | None = None,
            to_addr: str | None = None, extra: dict | None = None,
            blind: bool = False) -> dict[str, str]:
    """Deliver using an ALREADY-RESOLVED decision per channel.

    No database access happens in here for EMAIL at all; PUSH opens its own
    (push.py:87), independent of whatever connection produced `decisions`.
    That is what makes calling `resolve()` (or `blind_decisions()`) and this
    function as two separate steps — with any connection closed in between —
    safe for a caller like notify_cli's guard: by the time any channel's
    message leaves, the decision for EVERY channel has already been made, so
    a database fault can only ever happen BEFORE delivery starts, never
    after a partial delivery has already gone out. See `resolve()`'s
    docstring for the bug this eliminates.

    Returns a per-channel verdict so callers can log what actually happened.
    A disabled channel reports 'disabled', never 'sent' — brief.sh keys its
    dead-man marker off this, and a failure reported as success is exactly
    how a dead job looks healthy for a week.

    A channel missing from `decisions` is skipped entirely — absent from the
    result — which is how the blind path expresses "I never even tried to
    decide this one" (see `blind_decisions()`).

    `blind` changes only the result dict's KEYS: each channel key gets a
    "(blind)" suffix (e.g. `"email(blind)"`) so a caller — notify_cli's
    printed line, in particular — can always tell a blind dispatch from a
    normal one. The VALUES are untouched: they reuse the same vocabulary
    this has always used ('sent' / 'failed' / 'unavailable' / 'disabled'),
    so `failed()` needs no separate vocabulary for the blind case, and this
    function must call the real sender and use its real return value for
    exactly that reason — hardcoding "sent" here would make a lost alert
    look identical to a delivered one, worse than the bug this exists to fix.

    `extra` is push-only (e.g. a deep-link key) — `_send_email` has no
    channel for arbitrary payload keys, so the email arm ignores it rather
    than gain a parameter it cannot use.
    """
    n = BY_KEY.get(type_key)
    if n is None:
        raise KeyError(f"unknown notification type: {type_key}")
    out: dict[str, str] = {}
    for ch in n.channels:
        if ch not in decisions:
            continue
        key = f"{ch}(blind)" if blind else ch
        if not decisions[ch]:
            out[key] = "disabled"
        elif ch == EMAIL:
            out[key] = _send_email(subject, text, html, from_addr, to_addr)
        elif ch == PUSH:
            from app import push
            if not push.configured():
                out[key] = "unconfigured"
            else:
                try:
                    # Deliberately broad, and scoped to this one call only:
                    # the delivery path (JWT signing in push._bearer(), the
                    # APNs HTTP round-trip, push._send()'s own get_conn())
                    # can raise from jwt/httpx/sqlite3/OSError depending on
                    # what is broken, and narrowly enumerating those would be
                    # brittle. Mirrors _send_email's own catch, above, so a
                    # push-delivery fault degrades to "failed" instead of
                    # propagating out of dispatch() — a KeyError for an
                    # unknown type_key (above) or a ValueError for an
                    # unhandled channel (below) are programming errors and
                    # still raise.
                    sent = push.send_alert(subject, text, extra)
                except Exception as exc:
                    print(f"notify.dispatch: push delivery raised "
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                    out[key] = "failed"
                else:
                    out[key] = "sent" if sent > 0 else "failed"
        else:
            # A channel constant with no dispatch arm here would otherwise
            # fall through to the push branch silently and misroute.
            raise ValueError(f"unhandled notification channel: {ch!r}")
    return out


def send(conn, type_key: str, subject: str, text: str, html: str | None = None,
         from_addr: str | None = None, to_addr: str | None = None,
         extra: dict | None = None) -> dict[str, str]:
    """Fan one notification out to whichever of its channels are enabled.

    A thin composition of `resolve()` + `dispatch()`, using the SAME
    connection throughout — the shape every caller except notify_cli's guard
    wants (one open connection, decide-then-deliver, same as before this
    function existed as two pieces). notify_cli's guard instead calls
    `resolve()` and `dispatch()` itself, separately, precisely so it can
    close the connection between them; see `dispatch()`'s docstring for why
    that separation matters there and not here.
    """
    return dispatch(type_key, resolve(conn, type_key), subject, text, html,
                    from_addr, to_addr, extra)


def failed(result: dict[str, str]) -> bool:
    return any(v in _FAILURE_VERDICTS for v in result.values())
