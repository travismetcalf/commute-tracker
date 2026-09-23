"""Token-based APNs sender (HTTP/2 + ES256 JWT).

Deliberately dependency-light and fail-quiet: until APNS_KEY_PATH / APNS_KEY_ID
are configured in .env (and PyJWT + cryptography + h2 are installed), every
send_* function is a silent no-op — email remains the fallback channel, so a
missing key must never break the anomaly check or the brief.

Two payload shapes:
  send_alert(title, body, extra=None) — visible notification, priority 10;
                             `extra` keys merge in as top-level payload
                             siblings of "aps" (e.g. a deep-link key the app
                             reads out of userInfo), never inside it
  send_silent()            — content-available background push, priority 5;
                             the app reloads widget timelines on receipt

Sandbox vs production matters: Xcode-installed builds register sandbox tokens,
TestFlight/App Store builds register production ones. devices.apns_env routes
each token to the right host; sending to the wrong one is a silent drop.
"""

import os
import time

from .db import get_conn, utcnow

_HOSTS = {
    "sandbox": "https://api.sandbox.push.apple.com",
    "production": "https://api.push.apple.com",
}
_jwt_cache = {"token": None, "issued": 0.0}


def _env(key: str, default: str = "") -> str:
    """Env var first; fall back to parsing .env (host jobs don't source it).

    The container gets these from the compose `environment:` block — there is
    no env_file, and .dockerignore keeps .env out of the image, so the fallback
    below never fires there. Until 2026-09-17 this said env_file, and the
    container silently had no APNs config at all.
    """
    if os.environ.get(key):
        return os.environ[key]
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    try:
        with open(env_path) as f:
            for line in f:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def configured() -> bool:
    key_path = _env("APNS_KEY_PATH")
    if not key_path or not _env("APNS_KEY_ID") or not os.path.exists(key_path):
        return False
    try:
        import h2  # noqa: F401  httpx's http2 extra
        import jwt  # noqa: F401  PyJWT + cryptography
        return True
    except ImportError:
        return False


def _bearer() -> str:
    """APNs provider JWT. Apple wants reuse for 20–60 min; refreshing more
    than once per 20 min earns TooManyProviderTokenUpdates."""
    import jwt

    if _jwt_cache["token"] and time.time() - _jwt_cache["issued"] < 45 * 60:
        return _jwt_cache["token"]
    with open(_env("APNS_KEY_PATH")) as f:
        signing_key = f.read()
    token = jwt.encode(
        {"iss": _env("APNS_TEAM_ID", "8F62W24942"), "iat": int(time.time())},
        signing_key, algorithm="ES256",
        headers={"kid": _env("APNS_KEY_ID")},
    )
    _jwt_cache.update(token=token, issued=time.time())
    return token


def _send(payload: dict, push_type: str, priority: int) -> int:
    import httpx

    with get_conn() as conn:
        devices = conn.execute(
            "SELECT id, device_token, apns_env FROM devices WHERE enabled=1"
        ).fetchall()
    if not devices:
        return 0

    topic = _env("APNS_BUNDLE_ID", "com.travismetcalf.commute")
    headers = {
        "authorization": f"bearer {_bearer()}",
        "apns-topic": topic,
        "apns-push-type": push_type,
        "apns-priority": str(priority),
    }
    sent, dead = 0, []
    with httpx.Client(http2=True, timeout=10) as client:
        for d in devices:
            host = _HOSTS.get(d["apns_env"], _HOSTS["sandbox"])
            try:
                r = client.post(f"{host}/3/device/{d['device_token']}",
                                json=payload, headers=headers)
            except httpx.HTTPError:
                continue
            if r.status_code == 200:
                sent += 1
            elif r.status_code == 410:   # Unregistered — app was deleted
                dead.append(d["id"])
    if dead:
        with get_conn() as conn:
            conn.executemany(
                "UPDATE devices SET enabled=0, last_seen_at=? WHERE id=?",
                [(utcnow(), i) for i in dead])
    return sent


def send_alert(title: str, body: str, extra: dict | None = None) -> int:
    if not configured():
        return 0
    payload = {"aps": {"alert": {"title": title, "body": body}, "sound": "default"}}
    if extra:
        # "aps" is APNs' own top-level key, holding the alert built above. An
        # `extra` entry named "aps" would silently clobber it via update().
        # Not reachable today — nothing calls send_alert with such a key —
        # but guarded so it stays that way rather than depending on callers
        # never choosing it.
        payload.update({k: v for k, v in extra.items() if k != "aps"})
    return _send(payload, push_type="alert", priority=10)


def send_silent() -> int:
    """Background push: wakes the app briefly; it reloads widget timelines
    and runs a reconcile+flush. Priority MUST be 5 for content-available."""
    if not configured():
        return 0
    return _send({"aps": {"content-available": 1}},
                 push_type="background", priority=5)
