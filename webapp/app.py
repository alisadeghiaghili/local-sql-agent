# SPDX-License-Identifier: BUSL-1.1
# Copyright (c) 2024-2026 Ali Sadeghi Aghili
"""Simple web front-end for the local SQL agent.

Usage (from this folder)::

    py -3.13 app.py create-user alice            # prompts for password
    py -3.13 app.py create-user alice s3cret     # password on the command line
    py -3.13 app.py                              # dev server on http://127.0.0.1:5000

Users are stored in ``app.db`` (SQLite) with hashed passwords.  Every
submitted question is logged to the ``logs`` table with its result.
"""

from __future__ import annotations

import getpass
import os
import re
import secrets
import sqlite3
import sys
import threading
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

import db
import i18n
from agent import OUTPUT_DIR, answer_question, principal_for_username

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.provenance import log_startup_notice  # noqa: E402

WEBAPP_DIR = Path(__file__).resolve().parent
SECRET_KEY_FILE = WEBAPP_DIR / ".secret_key"


def _require_admin_user() -> str:
    """Resolve the one account permitted to create new users (``/register``).

    Finding 16: this used to fall back to a literal username baked into
    source -- a real employee's, published in a repository that was
    public at audit time (finding 1), naming exactly the one account this
    app treats as privileged. There is no safe default for "the identity
    of a privileged account": a guessed or
    hardcoded one is either wrong (silently breaking a deployment that
    meant to set its own) or, worse, silently *right* for this one
    deployment and then copied as boilerplate into the next one's config.
    An unset ``ADMIN_USER`` is a misconfiguration, not something to guess
    past, so this raises rather than falling back to anything -- read
    fresh on every call (not cached in a module-level constant) so a
    process that reloads this module after fixing the environment
    variable does not keep failing on a stale value from before the fix.
    """
    admin_user = os.getenv("ADMIN_USER")
    if not admin_user:
        raise RuntimeError(
            "ADMIN_USER is not set. This app requires an explicit "
            "administrator username -- the only account permitted to "
            "create new accounts via /register -- with no default. Set "
            "the ADMIN_USER environment variable before starting the app."
        )
    return admin_user


def _flash(text: str, category: str = "message", **kwargs) -> None:
    """Flash ``text`` translated into the session's current language."""
    flash(i18n.translate(text, i18n.get_lang(), **kwargs), category)


def _secret_key() -> str:
    """Env var wins; otherwise persist a random key so sessions survive restarts.

    Finding 18: the persisted file is restricted to owner-only permissions
    immediately after being written. This file holds the Flask session-
    signing key -- anyone else with a shell on the host (another service
    account, an operator, a compromised process) who can read it can
    forge a session cookie for any user, including the ``ADMIN_USER``
    account, with no password and nothing in the auth logs. Guarded for
    Windows (``os.name != "nt"``): file mode bits are a POSIX concept and
    the deployment target this finding describes is a Linux server, but
    the ``chmod`` call itself must stay unconditional in the source so a
    Linux deployment gets it regardless of what platform last edited
    this file.
    """
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key)
    if os.name != "nt":
        os.chmod(SECRET_KEY_FILE, 0o600)
    return key


# ---------------------------------------------------------------------------
# Finding 17a: per-IP login throttling
# ---------------------------------------------------------------------------
# In-process, module-level (not per-app-instance) so it survives whatever
# create_app() does and matches the FastAPI side's own bucketing shape
# (api/middleware.py's RateLimitMiddleware) -- a fixed threshold of
# consecutive failures per source IP, the account name on the other end
# never being part of the key. The published ADMIN_USER account (finding
# 16) is exactly the target unlimited guessing would go after; capping
# guesses per IP rather than per attempted username also stops the
# obvious workaround of guessing many different usernames from one
# source. Cleared on a successful login from that IP -- this defends
# against a *stranger* guessing, not against an operator who mistyped
# their own password several times in a row.
_LOGIN_FAILURE_THRESHOLD = 30
_login_failure_counts: dict[str, int] = {}
_login_failure_lock = threading.Lock()


def _client_ip() -> str:
    return request.remote_addr or "unknown"


def _login_throttled(ip: str) -> bool:
    with _login_failure_lock:
        return _login_failure_counts.get(ip, 0) >= _LOGIN_FAILURE_THRESHOLD


def _record_login_failure(ip: str) -> None:
    with _login_failure_lock:
        _login_failure_counts[ip] = _login_failure_counts.get(ip, 0) + 1


def _clear_login_failures(ip: str) -> None:
    with _login_failure_lock:
        _login_failure_counts.pop(ip, None)


# ---------------------------------------------------------------------------
# Finding 17b: CSRF -- a session-bound token, enforced on POST
# ---------------------------------------------------------------------------
# No new dependency (Flask-WTF is the obvious alternative -- see
# tests/security_audit/test_flask_hardening.py's module docstring): a
# random token is minted into the session the first time a form that
# needs one is rendered, embedded as a hidden field, and compared with
# constant-time equality against the form submission. /login is
# deliberately exempt: it is the one form submitted before any session
# exists to bind a token to, and enforcing it here would make the login
# throttling above unreachable for a client that has never done a GET
# first (exactly how a credential-stuffing script behaves) -- the
# module docstring's own reasoning already treats login CSRF as the
# lesser risk (a browser's default SameSite=Lax already blocks most
# cross-site posts) next to /register, which creates accounts, and the
# index page's query form, which is why both of those two are enforced.
_CSRF_SESSION_KEY = "_csrf_token"
_CSRF_EXEMPT_ENDPOINTS = {"login", "static"}


def _csrf_token() -> str:
    token = session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_hex(16)
        session[_CSRF_SESSION_KEY] = token
    return token


def create_app() -> Flask:
    log_startup_notice()
    db.init_db()
    admin_user = _require_admin_user()
    app = Flask(__name__)
    app.secret_key = _secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8  # 8h login

    # Finding 17c: cookie hardening. Set explicitly rather than left to
    # Flask's own defaults or a browser's -- see this module's docstring
    # in tests/security_audit/test_flask_hardening.py for why relying on
    # either is "a decision someone else can change". SESSION_COOKIE_SECURE
    # means the cookie is withheld over plain HTTP: a local dev server run
    # over http:// will look like login silently does not "stick" --
    # documented in webapp/WebApp.md rather than left to be rediscovered.
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Strict",
    )

    @app.context_processor
    def _inject_auth() -> dict[str, str | None]:
        return {"current_user": session.get("username"), "admin_user": admin_user}

    @app.context_processor
    def _inject_i18n() -> dict:
        lang = i18n.get_lang()
        return {
            "current_lang": lang,
            "langs": [(code, code.upper()) for code in i18n.LANGS],
            "_": lambda text, **kwargs: i18n.translate(text, lang, **kwargs),
            "csrf_token": _csrf_token,
        }

    @app.before_request
    def _check_csrf():
        """Reject a token-less POST to a form that needs one (finding 17b).

        Runs before every view, so a request never reaches a route's own
        logic (including a route's "redirect to /login if not signed in",
        which is a 302, not the 400/403 this defence is meant to produce)
        without a valid token first. See the module-level comment above
        ``_CSRF_EXEMPT_ENDPOINTS`` for why ``login`` is excluded.
        """
        if request.method != "POST" or request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
            return None
        submitted = request.form.get("csrf_token", "")
        expected = session.get(_CSRF_SESSION_KEY, "")
        if not expected or not secrets.compare_digest(submitted, expected):
            abort(400)
        return None

    @app.route("/lang/<lang>")
    def set_lang(lang):
        if lang in i18n.LANGS:
            session["lang"] = lang
        referrer = request.referrer or ""
        if referrer.startswith(request.host_url):
            return redirect(referrer)
        return redirect(url_for("index"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "username" in session:
            return redirect(url_for("index"))
        if request.method == "POST":
            ip = _client_ip()
            # Finding 17a: checked BEFORE touching the database at all --
            # once an IP is throttled, a guess costs it nothing further to
            # keep guessing against, which is the point.
            if _login_throttled(ip):
                return ("Too many failed login attempts. Try again later.", 429)
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if db.verify_user(username, password) is None:
                _record_login_failure(ip)
                _flash("Invalid username or password.", "error")
            else:
                _clear_login_failures(ip)
                session["username"] = username
                session.permanent = True
                return redirect(url_for("index"))
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        username = session.get("username")
        if username is None:
            return redirect(url_for("login"))
        if username != admin_user:
            _flash("Only an administrator can create accounts.", "error")
            return redirect(url_for("index"))
        if request.method == "POST":
            new_username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirm = request.form.get("confirm", "")
            if not new_username or not password:
                _flash("Username and password are required.", "error")
            elif password != confirm:
                _flash("Passwords do not match.", "error")
            else:
                try:
                    db.create_user(new_username, password)
                except sqlite3.IntegrityError:
                    _flash("Username '%(name)s' is already taken.", "error", name=new_username)
                else:
                    _flash("Account '%(name)s' created.", "success", name=new_username)
                    return redirect(url_for("register"))
        return render_template("register.html", username=username)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/download/<filename>")
    def download(filename):
        if "username" not in session:
            return redirect(url_for("login"))
        # Only serve files the agent itself generated, never arbitrary paths.
        if not re.fullmatch(r"output_[0-9]{8}_[0-9]{6}(?:_[0-9]+)?\.csv", filename):
            abort(404)
        return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)

    @app.route("/", methods=["GET", "POST"])
    def index():
        if "username" not in session:
            return redirect(url_for("login"))
        result = None
        if request.method == "POST":
            question = request.form.get("question", "").strip()
            if not question:
                _flash("Please enter a question.", "error")
            else:
                # Finding 13: the caller must identify itself so the column
                # ACL (appdb.key_store / the admin panel's "ستون‌ها" action)
                # actually applies to this session -- see agent.py's
                # principal_for_username and its module-level comment.
                # Finding 14: the checkbox's own value is honoured exactly
                # as submitted, never overridden -- answer_question's
                # default covers only the caller who omits the argument.
                interpret = request.form.get("interpret") == "on"
                result = answer_question(
                    question,
                    interpret=interpret,
                    principal=principal_for_username(session["username"]),
                )
                db.log_query(
                    username=session["username"],
                    question=question,
                    status=result["status"],
                    generated_sql=result["sql"],
                    interpretation=result["interpretation"],
                    output_file=result["output_file"],
                    row_count=result["row_count"],
                    error_message=result["error_message"],
                    elapsed_seconds=result["elapsed_seconds"],
                )
                if result["status"] != "SUCCESS":
                    flash(result["error_message"], "error")
        return render_template(
            "index.html",
            username=session["username"],
            result=result,
            output_filename=(
                Path(result["output_file"]).name
                if result and result["output_file"]
                else None
            ),
        )

    return app


app = create_app()


def _cli_create_user(argv: list[str]) -> int:
    """create-user <username> [password] — password prompts if omitted."""
    if len(argv) < 2:
        print("Usage: app.py create-user <username> [password]")
        return 2
    username = argv[1].strip()
    password = argv[2] if len(argv) > 2 else getpass.getpass("Password: ")
    if not username or not password:
        print("Username and password must not be empty.")
        return 2
    try:
        db.create_user(username, password)
    except Exception as exc:  # noqa: BLE001 - sqlite3.IntegrityError and friends
        print(f"Failed to create user: {exc}")
        return 1
    print(f"User '{username}' created.")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "create-user":
        return _cli_create_user(argv)
    app.run(host="127.0.0.1", port=5000, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
