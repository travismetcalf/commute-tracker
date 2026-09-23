"""Coverage for _send_email()'s subprocess contract.

Every other test either replaces _send_email via the spy fixture (test_notify_
send.py) or hits its early "unavailable" return (no shim on disk). Nothing
actually executes the shim and checks what it was handed — so the
stdin-not-argv contract and the tempfile-cleanup-on-raise contract were
verified only by a reviewer reading the code. Flipping notify_brief_email on
is exactly what first exercises this path for real, since the branch's
headline promise is that re-enabling an email is a settings change, not a
deploy.
"""

import os
import tempfile
from pathlib import Path

from app import notify


def _write_shim(tmp_path: Path, argv_file: Path, stdin_file: Path,
                exit_code: int = 0, html_capture: Path | None = None) -> Path:
    """A fixture script that records exactly what it was invoked with.

    If invoked with --html-file, it also copies that file's content out to
    `html_capture` WHILE the shim is still running — _send_email's own
    cleanup deletes the tempfile in its `finally:` right after the subprocess
    exits, before the test ever gets a chance to read it back.
    """
    shim = tmp_path / "fake-notify-email.sh"
    lines = [
        "#!/usr/bin/env bash",
        f"printf '%s\\n' \"$@\" > \"{argv_file}\"",
        f'cat > "{stdin_file}"',
    ]
    if html_capture is not None:
        lines += [
            'args=("$@")',
            'for i in "${!args[@]}"; do',
            '  if [[ "${args[$i]}" == "--html-file" ]]; then',
            f'    cp "${{args[$((i+1))]}}" "{html_capture}"',
            "  fi",
            "done",
        ]
    lines.append(f"exit {exit_code}")
    shim.write_text("\n".join(lines) + "\n")
    shim.chmod(0o755)
    return shim


def test_body_rides_stdin_not_argv(tmp_path, monkeypatch):
    """A body containing an apostrophe must never show up on the command
    line — only on stdin, per the docstring's own claim."""
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    shim = _write_shim(tmp_path, argv_file, stdin_file, exit_code=0)
    monkeypatch.setattr(notify, "_SHIM", str(shim))

    body = "it's a long one with an apostrophe"
    verdict = notify._send_email("Subject line", body,
                                 from_addr="a@example.com",
                                 to_addr="b@example.com")

    assert verdict == "sent"
    assert stdin_file.read_text() == body

    argv = argv_file.read_text()
    assert body not in argv
    assert "--from" in argv and "a@example.com" in argv
    assert "--to" in argv and "b@example.com" in argv
    assert "--subject" in argv and "Subject line" in argv
    assert "--text-file" in argv and "-" in argv.splitlines()


def test_from_and_to_are_omitted_when_not_given(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    shim = _write_shim(tmp_path, argv_file, stdin_file, exit_code=0)
    monkeypatch.setattr(notify, "_SHIM", str(shim))

    verdict = notify._send_email("S", "body")

    assert verdict == "sent"
    argv = argv_file.read_text()
    assert "--from" not in argv
    assert "--to" not in argv


def test_nonzero_exit_is_a_failed_verdict(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    shim = _write_shim(tmp_path, argv_file, stdin_file, exit_code=1)
    monkeypatch.setattr(notify, "_SHIM", str(shim))

    assert notify._send_email("S", "body") == "failed"


def test_zero_exit_is_a_sent_verdict(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    shim = _write_shim(tmp_path, argv_file, stdin_file, exit_code=0)
    monkeypatch.setattr(notify, "_SHIM", str(shim))

    assert notify._send_email("S", "body") == "sent"


def test_html_goes_to_a_tempfile_named_by_html_file(tmp_path, monkeypatch):
    argv_file = tmp_path / "argv.txt"
    stdin_file = tmp_path / "stdin.txt"
    html_capture = tmp_path / "html_capture.html"
    shim = _write_shim(tmp_path, argv_file, stdin_file, exit_code=0,
                       html_capture=html_capture)
    monkeypatch.setattr(notify, "_SHIM", str(shim))

    verdict = notify._send_email("S", "text body", html="<p>hi &amp; bye</p>")
    assert verdict == "sent"

    argv_parts = argv_file.read_text().splitlines()
    assert "--html-file" in argv_parts
    html_path = argv_parts[argv_parts.index("--html-file") + 1]
    # The shim copied the file's content out while it still existed — read
    # THAT, since _send_email's own finally: has already deleted html_path
    # by the time control returns here.
    assert html_capture.read_text() == "<p>hi &amp; bye</p>"
    # And confirm the cleanup actually happened: no leaked tempfile.
    assert not os.path.exists(html_path)


def test_html_tempfile_is_cleaned_up_even_when_the_subprocess_raises(
        tmp_path, monkeypatch):
    """The finally: block's whole job is to not leak the html tempfile when
    subprocess.run() itself raises (OSError / SubprocessError), not just on
    a clean non-zero exit."""
    created = {}
    orig_ntf = tempfile.NamedTemporaryFile

    def spy_ntf(*a, **kw):
        f = orig_ntf(*a, **kw)
        created["name"] = f.name
        return f

    def boom(*a, **kw):
        raise OSError("simulated: no such file or directory")

    # _SHIM only needs to exist for the os.path.exists() gate; subprocess.run
    # is replaced outright so it never actually runs.
    shim = tmp_path / "unused-shim.sh"
    shim.write_text("#!/usr/bin/env bash\nexit 0\n")
    shim.chmod(0o755)
    monkeypatch.setattr(notify, "_SHIM", str(shim))
    monkeypatch.setattr(notify.tempfile, "NamedTemporaryFile", spy_ntf)
    monkeypatch.setattr(notify.subprocess, "run", boom)

    verdict = notify._send_email("S", "body", html="<p>hi</p>")

    assert verdict == "failed"
    assert created.get("name")
    assert not os.path.exists(created["name"])
