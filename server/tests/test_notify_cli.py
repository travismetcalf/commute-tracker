import io
import os
import subprocess
import sys

from app import notify
from app.db import set_setting
from app.notify_cli import main

SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cli(db_path, *args, body="body"):
    env = {**os.environ, "DB_PATH": str(db_path), "PYTHONPATH": SERVER_DIR}
    return subprocess.run([sys.executable, "-m", "app.notify_cli", *args],
                          input=body, text=True, capture_output=True,
                          cwd=SERVER_DIR, env=env)


def test_disabled_type_exits_zero_and_says_so(conn, tmp_path):
    """The brief being off is a completed run, not a failed one — the job's
    heartbeat must still be allowed to advance."""
    r = run_cli(tmp_path / "test.db", "brief", "--subject", "S")
    assert r.returncode == 0
    assert "email=disabled" in r.stdout
    assert "push=disabled" in r.stdout


def test_unknown_type_exits_two(conn, tmp_path):
    r = run_cli(tmp_path / "test.db", "no_such_type", "--subject", "S")
    assert r.returncode == 2


def test_body_comes_from_stdin(conn, monkeypatch):
    """Bodies contain apostrophes; they must never be interpolated into a
    command line. This test verifies stdin is actually captured and passed to
    the sender."""
    # weekly_summary has email enabled by default
    captured = {}

    def fake_send_email(subject, text, html=None, from_addr=None, to_addr=None):
        captured["text"] = text
        return "sent"

    monkeypatch.setattr(notify, "_send_email", fake_send_email)
    body_text = "it's a long one"
    monkeypatch.setattr("sys.stdin", io.StringIO(body_text))

    exit_code = main(["weekly_summary", "--subject", "S"])
    assert exit_code == 0
    assert captured["text"] == body_text


def test_html_file_reaches_the_sender(conn, monkeypatch, tmp_path):
    """F8: --html-file is read off disk and threaded through main() ->
    notify.dispatch() -> notify._send_email(), but nothing asserted on the
    `html` argument actually arriving at the sender. A caller that dropped
    it anywhere along that chain (dispatch() forgetting to forward it, or
    main() forgetting to pass it in) would silently degrade every
    HTML-bearing send to text-only — weekly_summary's HTML body is the
    concrete case this protects, and test_body_comes_from_stdin above never
    exercises --html-file at all."""
    captured = {}

    def fake_send_email(subject, text, html=None, from_addr=None, to_addr=None):
        captured["html"] = html
        return "sent"

    monkeypatch.setattr(notify, "_send_email", fake_send_email)
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))

    html_path = tmp_path / "body.html"
    html_path.write_text("<p>hello</p>", encoding="utf-8")

    # weekly_summary's email default is ON, so no set_setting needed.
    exit_code = main(["weekly_summary", "--subject", "S",
                      "--html-file", str(html_path)])
    assert exit_code == 0
    assert captured["html"] == "<p>hello</p>"


def test_unreadable_html_file_is_a_clean_usage_error(conn, monkeypatch):
    """The OSError guard around --html-file shares the same untested path —
    a typo'd path must exit 2 with a stderr message, not degrade silently
    to a text-only send or crash with a traceback."""
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))
    exit_code = main(["weekly_summary", "--subject", "S",
                      "--html-file", "/no/such/file.html"])
    assert exit_code == 2


def test_delivery_failure_exits_one(conn, monkeypatch):
    """Exit code 1 when a channel we were asked to use did not deliver."""
    # Enable email channel for brief
    set_setting(conn, "notify_brief_email", "1")
    conn.commit()  # Commit so main()'s fresh connection sees the change

    def fake_send_email(subject, text, html=None, from_addr=None, to_addr=None):
        return "failed"

    monkeypatch.setattr(notify, "_send_email", fake_send_email)
    monkeypatch.setattr("sys.stdin", io.StringIO("body"))

    exit_code = main(["brief", "--subject", "S"])
    assert exit_code == 1
