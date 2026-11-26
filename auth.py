import math
import re
import secrets
import time
from collections import deque
from threading import Lock

from flask import Blueprint, current_app, request, session
from sqlalchemy.exc import IntegrityError
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash

from models import User, db


auth = Blueprint("auth", __name__, url_prefix="/api")
PROFILE_FIELDS = {"full_name": 160, "city": 120, "phone": 40, "bio": 2000}
EMAIL_LOCAL = re.compile(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+")
EMAIL_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
PHONE = re.compile(r"[+0-9(). xX-]+")
INVALID_UNICODE = re.compile(r"[\ud800-\udfff]")
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32), method="scrypt")


class LoginLimiter:
    """Bound the number of login attempts per IP over a rolling time window."""

    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self.attempts = {}
        self.lock = Lock()

    def retry_after(self, ip):
        with self.lock:
            now = time.monotonic()
            cutoff = now - self.window
            for key, attempts in list(self.attempts.items()):
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if not attempts:
                    del self.attempts[key]
            if ip not in self.attempts and len(self.attempts) >= 4096:
                return self.window
            attempts = self.attempts.setdefault(ip, deque())
            if len(attempts) >= self.limit:
                return max(1, math.ceil(attempts[0] + self.window - now))
            attempts.append(now)
            return 0


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


@auth.before_request
def protect_auth_requests():
    secret = current_app.secret_key
    if not secret or len(secret) < 32:
        return {"error": "Account services are unavailable. Configure the server session secret."}, 503
    if request.method in {"POST", "PATCH", "DELETE"}:
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied.isascii() or not secrets.compare_digest(expected, supplied):
            return {"error": "Your security token expired. Refresh the page and try again.", "code": "csrf"}, 403
        request.max_content_length = 16 * 1024


@auth.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


@auth.errorhandler(RequestEntityTooLarge)
def body_too_large(_error):
    return {"error": "The request is too large."}, 413


def json_object():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def normalize_email(value):
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email.isascii() or len(email) > 254 or email.count("@") != 1:
        return None
    local, domain = email.split("@")
    labels = domain.split(".")
    if (len(local) > 64 or not EMAIL_LOCAL.fullmatch(local) or local.startswith(".")
            or local.endswith(".") or ".." in local or len(labels) < 2
            or not all(EMAIL_LABEL.fullmatch(label) for label in labels)):
        return None
    return email


def profile_values(data, *, require_name=False):
    values = {}
    if require_name and "full_name" not in data:
        raise ValueError("Enter your full name.")
    for name, limit in PROFILE_FIELDS.items():
        if name not in data:
            continue
        value = data[name]
        label = name.replace("_", " ").capitalize()
        if value is None and name != "full_name":
            values[name] = None
            continue
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text.")
        value = value.strip()
        if len(value) > limit:
            raise ValueError(f"{label} must be no more than {limit} characters.")
        if name == "full_name" and not value:
            raise ValueError("Enter your full name.")
        if INVALID_UNICODE.search(value) or any(
            ord(char) < 32 and (name != "bio" or char not in "\n\r\t") for char in value
        ):
            raise ValueError(f"{label} contains an invalid character.")
        if name == "phone" and value and (not PHONE.fullmatch(value) or not any(char.isdigit() for char in value)):
            raise ValueError("Enter a valid phone number or leave it blank.")
        values[name] = value or None
    return values


def serialize_user(user, *, include_phone=False):
    data = {
        "id": f"account:{user.id}",
        "full_name": user.full_name,
        "email": user.email,
        "city": user.city,
        "bio": user.bio,
        "created_at": user.created_at.isoformat(),
    }
    if include_phone:
        data["phone"] = user.phone
    return data


def signed_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        return None
    user = db.session.get(User, user_id)
    if user is None or user.deactivated_at is not None:
        session.clear()
        return None
    return user


def start_session(user):
    session.clear()
    session["user_id"] = user.id
    session.permanent = True
    return {"user": serialize_user(user), "csrf_token": csrf_token()}


def valid_password(value, minimum):
    return (
        isinstance(value, str) and minimum <= len(value) <= MAX_PASSWORD_LENGTH
        and bool(value.strip()) and not INVALID_UNICODE.search(value)
    )


@auth.post("/register")
def register():
    data = json_object()
    if data is None:
        return {"error": "Submit a JSON object."}, 400
    if data.keys() - (PROFILE_FIELDS.keys() | {"email", "password"}):
        return {"error": "The registration contains an unsupported field."}, 400
    email = normalize_email(data.get("email"))
    if email is None:
        return {"error": "Enter a valid email address."}, 400
    password = data.get("password")
    if not valid_password(password, MIN_PASSWORD_LENGTH):
        return {"error": f"Use a password between {MIN_PASSWORD_LENGTH} and {MAX_PASSWORD_LENGTH} characters."}, 400
    try:
        values = profile_values(data, require_name=True)
    except ValueError as error:
        return {"error": str(error)}, 400
    if db.session.scalar(db.select(User.id).filter_by(email=email)) is not None:
        return {"error": "An account already uses this email address."}, 409
    user = User(email=email, password_hash=generate_password_hash(password, method="scrypt"), **values)
    db.session.add(user)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        if db.session.scalar(db.select(User.id).filter_by(email=email)) is not None:
            return {"error": "An account already uses this email address."}, 409
        raise
    return start_session(user), 201


@auth.post("/login")
def login():
    limiter = current_app.extensions["etewa_login_limiter"]
    retry_after = limiter.retry_after(request.remote_addr or "unknown")
    if retry_after:
        return {"error": "Too many sign-in attempts. Try again later."}, 429, {"Retry-After": str(retry_after)}
    data = json_object()
    if data is None:
        return {"error": "Submit a JSON object."}, 400
    email = normalize_email(data.get("email"))
    password = data.get("password")
    password_valid = valid_password(password, 1)
    user = db.session.scalar(db.select(User).filter_by(email=email)) if email is not None else None
    matches = check_password_hash(user.password_hash if user is not None else DUMMY_HASH, password if password_valid else "")
    if not password_valid or not matches or user is None or user.deactivated_at is not None:
        return {"error": "Invalid email or password."}, 401
    return start_session(user)


@auth.post("/logout")
def logout():
    session.clear()
    return {"user": None, "csrf_token": csrf_token()}


@auth.get("/me")
def me():
    user = signed_in_user()
    return {"user": serialize_user(user) if user is not None else None, "csrf_token": csrf_token()}


@auth.route("/profile", methods=["GET", "PATCH"])
def profile():
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to access your profile."}, 401
    if request.method == "PATCH":
        data = json_object()
        if not data or data.keys() - PROFILE_FIELDS.keys():
            return {"error": "Only full name, city, phone and bio can be updated."}, 400
        try:
            values = profile_values(data)
        except ValueError as error:
            return {"error": str(error)}, 400
        for key, value in values.items():
            setattr(user, key, value)
        db.session.commit()
    return {"user": serialize_user(user, include_phone=True)}
