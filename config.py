import os
from datetime import timedelta


class Config:
    # Flask-SQLAlchemy resolves relative SQLite paths inside Flask's instance folder.
    SQLALCHEMY_DATABASE_URI = os.environ.get("ETEWA_DATABASE_URL") or "sqlite:///etewa.sqlite3"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get("ETEWA_SECRET_KEY")
    SESSION_COOKIE_NAME = "etewa_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("ETEWA_SESSION_COOKIE_SECURE", "").lower() in {"1", "true"}
    SESSION_REFRESH_EACH_REQUEST = False
    PERMANENT_SESSION_LIFETIME = timedelta(hours=8)
    LOGIN_RATE_LIMIT = 12
    LOGIN_RATE_WINDOW_SECONDS = 900
