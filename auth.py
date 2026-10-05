"""
Sign-in for the dashboard: local accounts, Google sign-in, sessions, and the
one-time links an admin can make from the command line.

Everything lives in ./auth, readable only by the account that runs the dashboard:

    auth/users.json      accounts. Passwords are stored only as scrypt hashes.
    auth/sessions.json   signed-in browsers. Tokens are stored only as SHA-256 hashes.
    auth/links.json      one-time sign-in links from `manage.py login-link`, hashed.

manage.py edits the same files, so a password reset, unlock or sign-out made from
the command line applies to a running dashboard straight away.

Google sign-in uses the OpenID Connect authorization-code flow with PKCE. The ID
token comes straight from Google's token endpoint over TLS, which is what lets us
trust its claims without checking a signature (Google's OpenID Connect guide says
as much); we still check issuer, audience, expiry, nonce, verified email and, when
a Workspace domain is set, the hosted-domain claim.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).resolve().parent) / "auth"   # DATA_DIR: a hosted disk
USERS, SESSIONS, LINKS = DIR / "users.json", DIR / "sessions.json", DIR / "links.json"

SESSION_IDLE = 24 * 3600         # over 20 hours, so WCAG 2.2.1 needs no timeout warning
SESSION_MAX = 30 * 24 * 3600
TOUCH_EVERY = 300                # write last-seen at most every 5 minutes
MAX_FAILURES = 5
FAIL_WINDOW = LOCK_SECONDS = 15 * 60
MIN_PASSWORD = 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

_lock = threading.RLock()
_cache = {}                      # path -> (mtime_ns, data)


# ---------- files ----------

def _read(path):
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return json.loads(json.dumps(hit[1]))     # a copy callers may change
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        data = {}
    _cache[path] = (mtime, data)
    return json.loads(json.dumps(data))


def _write(path, data):
    DIR.mkdir(mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    _cache.pop(path, None)


def _sha(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------- passwords ----------

def hash_password(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P,
                        maxmem=128 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password, stored):
    try:
        algo, n, r, p, salt, want = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=128 * 1024 * 1024, dklen=len(_unb64(want)))
        return hmac.compare_digest(got, _unb64(want))
    except (ValueError, AttributeError):
        return False


_DUMMY = None


def _burn(password):
    """Spend the same time as a real check, so unknown names can't be told apart by timing."""
    global _DUMMY
    _DUMMY = _DUMMY or hash_password(secrets.token_hex(8))
    verify_password(password, _DUMMY)


def password_problem(password, username=""):
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if username and password.lower() == username.lower():
        return "The password can't be the same as the username."
    if len(set(password)) < 4:
        return "That password is too easy to guess."
    return None


# ---------- accounts ----------

def norm(name):
    return (name or "").strip().lower()


def load_users():
    return _read(USERS)


def save_users(users):
    _write(USERS, users)


def has_users():
    return bool(load_users())


def find_by_email(users, email):
    email = norm(email)
    return next((n for n, u in users.items() if norm(u.get("email")) == email), None)


def public_user(name, u, method):
    return {"username": name, "name": u.get("name") or name, "email": u.get("email") or "",
            "admin": bool(u.get("admin")), "method": method}


_unknown_failures = {}           # failures against names that don't exist, kept in memory


def authenticate(username, password):
    """(user, None) on success, else (None, message). Messages never reveal whether a name exists."""
    name, now = norm(username), time.time()
    generic = "That username and password don't match."
    locked = (f"Too many attempts. Try again in {LOCK_SECONDS // 60} minutes, "
              "or ask an admin to run: manage.py unlock " + (name or "<username>"))
    with _lock:
        users = load_users()
        u = users.get(name)
        if not u or u.get("disabled") or not u.get("password"):
            _burn(password)
            fails = [t for t in _unknown_failures.get(name, []) if now - t < FAIL_WINDOW] + [now]
            _unknown_failures[name] = fails
            return None, locked if len(fails) > MAX_FAILURES else generic
        if u.get("locked_until", 0) > now:
            _burn(password)
            return None, locked
        if verify_password(password, u["password"]):
            u.update(failures=[], locked_until=0, last_login=now)
            save_users(users)
            return public_user(name, u, "password"), None
        u["failures"] = [t for t in u.get("failures", []) if now - t < FAIL_WINDOW] + [now]
        if len(u["failures"]) >= MAX_FAILURES:
            u["locked_until"], u["failures"] = now + LOCK_SECONDS, []
            save_users(users)
            return None, locked
        save_users(users)
        return None, generic


# ---------- sessions ----------

def create_session(username, method):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        sessions = {k: s for k, s in _read(SESSIONS).items() if _alive(s, now)}
        sessions[_sha(token)] = {"user": username, "method": method, "created": now, "seen": now}
        _write(SESSIONS, sessions)
    return token


def _alive(s, now):
    return now - s["seen"] < SESSION_IDLE and now - s["created"] < SESSION_MAX


def session_user(token):
    if not token:
        return None
    now, key = time.time(), _sha(token)
    s = _read(SESSIONS).get(key)
    if not s or not _alive(s, now):
        return None
    u = load_users().get(s["user"])
    if not u or u.get("disabled"):
        return None
    if now - s["seen"] > TOUCH_EVERY:
        with _lock:
            sessions = _read(SESSIONS)
            if key in sessions:
                sessions[key]["seen"] = now
                _write(SESSIONS, sessions)
    return public_user(s["user"], u, s["method"])


def end_session(token):
    if not token:
        return
    with _lock:
        sessions = _read(SESSIONS)
        if sessions.pop(_sha(token), None):
            _write(SESSIONS, sessions)


def end_sessions(username=None):
    """Sign out one user everywhere, or everyone. Returns how many sessions ended."""
    with _lock:
        sessions = _read(SESSIONS)
        keep = {k: s for k, s in sessions.items() if username and s["user"] != username}
        _write(SESSIONS, keep)
        return len(sessions) - len(keep)


# ---------- one-time links (admin override) ----------

def create_login_link(username, minutes=10):
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        links = {k: l for k, l in _read(LINKS).items() if l["expires"] > now}
        links[_sha(token)] = {"user": username, "expires": now + minutes * 60}
        _write(LINKS, links)
    return token


def use_login_link(token):
    with _lock:
        links = _read(LINKS)
        link = links.pop(_sha(token or ""), None)
        _write(LINKS, links)
    if not link or link["expires"] < time.time():
        return None
    u = load_users().get(link["user"])
    return link["user"] if u and not u.get("disabled") else None


# ---------- Google sign-in ----------

def google_config(default_redirect):
    domains = [d.strip().lower() for d in os.environ.get("GOOGLE_ALLOWED_DOMAIN", "").split(",") if d.strip()]
    cfg = {
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        "secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
        "domains": domains,
        "redirect": os.environ.get("GOOGLE_REDIRECT_URI", "").strip() or default_redirect,
    }
    cfg["enabled"] = bool(cfg["client_id"] and cfg["secret"])
    return cfg


_pending = {}                    # state -> {verifier, nonce, next, created}


def google_start(cfg, next_url):
    now = time.time()
    for k in [k for k, p in _pending.items() if now - p["created"] > 600]:
        del _pending[k]
    state, nonce, verifier = (secrets.token_urlsafe(24) for _ in range(3))
    _pending[state] = {"verifier": verifier, "nonce": nonce, "next": next_url, "created": now}
    params = {
        "client_id": cfg["client_id"], "redirect_uri": cfg["redirect"], "response_type": "code",
        "scope": "openid email profile", "state": state, "nonce": nonce, "prompt": "select_account",
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
    }
    if len(cfg["domains"]) == 1:
        params["hd"] = cfg["domains"][0]          # a hint only; the claim is checked below
    return f"{GOOGLE_AUTH}?{urlencode(params)}", state


def google_finish(cfg, code, state):
    """(username, next_url, None) on success, else (None, None, message)."""
    pending = _pending.pop(state or "", None)
    if not pending or time.time() - pending["created"] > 600:
        return None, None, "That Google sign-in expired or was already used. Please try again."
    try:
        r = requests.post(GOOGLE_TOKEN, timeout=15, data={
            "code": code, "client_id": cfg["client_id"], "client_secret": cfg["secret"],
            "redirect_uri": cfg["redirect"], "grant_type": "authorization_code",
            "code_verifier": pending["verifier"],
        })
        r.raise_for_status()
        claims = json.loads(_unb64(r.json()["id_token"].split(".")[1]))
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return None, None, "Google didn't confirm the sign-in. Please try again."

    now = time.time()
    if (claims.get("iss") not in GOOGLE_ISSUERS or claims.get("aud") != cfg["client_id"]
            or claims.get("exp", 0) < now - 60 or claims.get("nonce") != pending["nonce"]):
        return None, None, "Google's answer didn't check out, so you weren't signed in."
    email = norm(claims.get("email"))
    if not email or not claims.get("email_verified"):
        return None, None, "That Google account has no verified email address."
    hd = norm(claims.get("hd"))
    if cfg["domains"] and hd not in cfg["domains"]:
        return None, None, f"Sign in with your {' or '.join(cfg['domains'])} Google account."

    with _lock:
        users = load_users()
        name = find_by_email(users, email)
        if name and users[name].get("disabled"):
            return None, None, "That account is disabled. Ask an admin to re-enable it."
        if not name:
            if not cfg["domains"]:
                return None, None, (f"{email} isn't allowed to use this dashboard yet. Ask an admin to run: "
                                    f"manage.py add-user <name> --email {email} --google-only")
            name = email
            users[name] = {"name": claims.get("name") or email, "email": email, "admin": False,
                           "created": now, "source": "google"}
        users[name]["last_login"] = now
        users[name].setdefault("name", claims.get("name") or email)
        save_users(users)
    return name, pending["next"], None
