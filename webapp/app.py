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
from agent import OUTPUT_DIR, answer_question

WEBAPP_DIR = Path(__file__).resolve().parent
SECRET_KEY_FILE = WEBAPP_DIR / ".secret_key"

# Only this account may create new users via /register (env ADMIN_USER overrides).
ADMIN_USER = os.getenv("ADMIN_USER", "bahmanabadi.m")


def _flash(text: str, category: str = "message", **kwargs) -> None:
    """Flash ``text`` translated into the session's current language."""
    flash(i18n.translate(text, i18n.get_lang(), **kwargs), category)


def _secret_key() -> str:
    """Env var wins; otherwise persist a random key so sessions survive restarts."""
    env_key = os.getenv("SECRET_KEY")
    if env_key:
        return env_key
    if SECRET_KEY_FILE.exists():
        return SECRET_KEY_FILE.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_FILE.write_text(key)
    return key


def create_app() -> Flask:
    db.init_db()
    app = Flask(__name__)
    app.secret_key = _secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8  # 8h login

    @app.context_processor
    def _inject_auth() -> dict[str, str | None]:
        return {"current_user": session.get("username"), "admin_user": ADMIN_USER}

    @app.context_processor
    def _inject_i18n() -> dict:
        lang = i18n.get_lang()
        return {
            "current_lang": lang,
            "langs": [(code, code.upper()) for code in i18n.LANGS],
            "_": lambda text, **kwargs: i18n.translate(text, lang, **kwargs),
        }

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
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if db.verify_user(username, password) is None:
                _flash("Invalid username or password.", "error")
            else:
                session["username"] = username
                session.permanent = True
                return redirect(url_for("index"))
        return render_template("login.html")

    @app.route("/register", methods=["GET", "POST"])
    def register():
        username = session.get("username")
        if username is None:
            return redirect(url_for("login"))
        if username != ADMIN_USER:
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
                interpret = request.form.get("interpret") == "on"
                result = answer_question(question, interpret)
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
