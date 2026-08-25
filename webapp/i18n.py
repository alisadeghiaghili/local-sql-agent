"""Lightweight EN/FA localization for the web app.

Language is stored in the Flask session (``session["lang"]``); the UI strings
below are keyed by their English text, which also serves as the English
translation.  ``translate()`` falls back to the key itself for any language
without an entry, so missing keys never render empty.
"""

from __future__ import annotations

from flask import session

LANGS: tuple[str, str] = ("en", "fa")
DEFAULT_LANG = "en"

# Keyed by English string; values are the Persian translations.
_FA: dict[str, str] = {
    # base.html
    "Add user": "افزودن کاربر",
    "Logout": "خروج",
    # index.html
    "Ask your question": "سؤال خود را بپرسید",
    "English or Persian — the agent will answer against the database.": (
        "انگلیسی یا فارسی — عامل بر اساس پایگاه داده به سؤال شما پاسخ می‌دهد."
    ),
    "Show plain-language summary": "نمایش خلاصه به زبان ساده",
    "Ask": "ارسال",
    "Report": "گزارش",
    "Running your question…": "در حال پردازش سؤال شما…",
    "Results": "نتایج",
    "SUCCESS": "موفق",
    "ERROR": "خطا",
    "model": "مدل",
    "s": "ثانیه",
    "Question:": "سؤال:",
    "Generated SQL": "SQL تولیدشده",
    "Copy": "کپی",
    "Copied": "کپی شد",
    "No rows returned for this question.": "برای این سؤال هیچ ردیفی بازنگشت.",
    "Previous": "قبلی",
    "Next": "بعدی",
    "Page %(page)s of %(pages)s": "صفحه %(page)s از %(pages)s",
    "Ask · Local SQL Agent": "پرسش · Local SQL Agent",
    # login.html
    "Welcome": "خوش آمدید",
    "Sign in to ask your database a question in Persian or English.": (
        "برای پرسیدن سؤال از پایگاه داده به زبان فارسی یا انگلیسی وارد شوید."
    ),
    "Username": "نام کاربری",
    "Password": "رمز عبور",
    "Sign in": "ورود",
    "Login · Local SQL Agent": "ورود · Local SQL Agent",
    # register.html
    "Create an account for a new user of the Local SQL Agent.": (
        "برای کاربر جدید Local SQL Agent یک حساب بسازید."
    ),
    "Confirm password": "تأیید رمز عبور",
    "Create account": "ایجاد حساب",
    "Register · Local SQL Agent": "ثبت‌نام · Local SQL Agent",
    # flash messages (app.py)
    "Invalid username or password.": "نام کاربری یا رمز عبور نامعتبر است.",
    "Only an administrator can create accounts.": "فقط مدیر می‌تواند حساب ایجاد کند.",
    "Username and password are required.": "نام کاربری و رمز عبور الزامی است.",
    "Passwords do not match.": "رمزهای عبور یکسان نیستند.",
    "Username '%(name)s' is already taken.": "نام کاربری «%(name)s» قبلاً گرفته شده است.",
    "Account '%(name)s' created.": "حساب «%(name)s» ایجاد شد.",
    "Please enter a question.": "لطفاً یک سؤال وارد کنید.",
}


def get_lang() -> str:
    """Return the session's language, falling back to English."""
    lang = session.get("lang")
    return lang if lang in LANGS else DEFAULT_LANG


def translate(text: str, lang: str, **kwargs) -> str:
    """Translate ``text`` into ``lang``; unknown keys render as the English text.

    Named ``%(name)s`` placeholders may be filled via keyword arguments.
    """
    if lang == "fa":
        text = _FA.get(text, text)
    if kwargs:
        text = text % kwargs
    return text
