#!/usr/bin/env python3
"""
Manage dashboard accounts from the command line. This is also the admin override:
anyone who can run this on the machine can reset a password, unlock an account,
or print a one-time sign-in link, without signing in first.

    python3 manage.py add-user andy --admin              # prompts for a password
    python3 manage.py add-user sam --email sam@city.gov --google-only
    python3 manage.py set-password andy                   # reset; unlocks and signs out old sessions
    python3 manage.py unlock andy
    python3 manage.py login-link andy                     # one-time link, valid 10 minutes
    python3 manage.py disable sam | enable sam | remove-user sam
    python3 manage.py sign-out [andy]                     # one user, or everyone
    python3 manage.py list
    python3 manage.py status

Changes apply to a running dashboard immediately.
"""

import argparse
import getpass
import sys
import time
from datetime import datetime

import auth
import triage  # noqa: F401  (loads .env, for the Google settings shown by `status`)

DEFAULT_URL = "http://127.0.0.1:8765"


def fail(msg):
    sys.exit(f"error: {msg}")


def ask_password(username, from_stdin):
    if from_stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass(f"New password for {username}: ")
        if pw != getpass.getpass("Type it again: "):
            fail("the two passwords didn't match")
    problem = auth.password_problem(pw, username)
    if problem:
        fail(problem)
    return pw


def get_user(users, name):
    name = auth.norm(name)
    if name not in users:
        fail(f"no account called {name!r}. See: manage.py list")
    return name, users[name]


def cmd_add(a):
    users = auth.load_users()
    name = auth.norm(a.username)
    if not name or any(c.isspace() for c in name):
        fail("usernames can't be empty or contain spaces")
    if name in users:
        fail(f"{name!r} already exists. To reset its password: manage.py set-password {name}")
    if a.email and auth.find_by_email(users, a.email):
        fail(f"{a.email} is already linked to another account")
    if a.google_only and not a.email:
        fail("--google-only needs --email, the Google address this person signs in with")
    user = {"name": a.name or name, "email": auth.norm(a.email), "admin": a.admin, "created": time.time()}
    if not a.google_only:
        user["password"] = auth.hash_password(ask_password(name, a.password_stdin))
    users[name] = user
    auth.save_users(users)
    how = "Google only" if a.google_only else "password" + (" or Google" if a.email else "")
    print(f"Added {name} ({'admin, ' if a.admin else ''}signs in with {how}).")


def cmd_set_password(a):
    users = auth.load_users()
    name, u = get_user(users, a.username)
    u["password"] = auth.hash_password(ask_password(name, a.password_stdin))
    u.update(failures=[], locked_until=0)
    auth.save_users(users)
    ended = auth.end_sessions(name)
    print(f"Password for {name} reset and account unlocked. Signed out {ended} existing session(s).")


def cmd_unlock(a):
    users = auth.load_users()
    name, u = get_user(users, a.username)
    u.update(failures=[], locked_until=0)
    auth.save_users(users)
    print(f"{name} is unlocked.")


def cmd_toggle(a, disabled):
    users = auth.load_users()
    name, u = get_user(users, a.username)
    u["disabled"] = disabled
    auth.save_users(users)
    if disabled:
        auth.end_sessions(name)
    print(f"{name} is {'disabled and signed out' if disabled else 'enabled'}.")


def cmd_remove(a):
    users = auth.load_users()
    name, _ = get_user(users, a.username)
    if not a.yes and input(f"Remove {name}? Type the username to confirm: ").strip().lower() != name:
        fail("not removed")
    del users[name]
    auth.save_users(users)
    auth.end_sessions(name)
    print(f"Removed {name}.")


def cmd_link(a):
    users = auth.load_users()
    name, u = get_user(users, a.username)
    if u.get("disabled"):
        fail(f"{name} is disabled. Run: manage.py enable {name}")
    token = auth.create_login_link(name, a.minutes)
    print(f"One-time sign-in link for {name}, valid {a.minutes} minutes:\n\n"
          f"  {a.url.rstrip('/')}/auth/link?token={token}\n\n"
          "It works once. Don't paste it anywhere it could be read by someone else.")


def cmd_sign_out(a):
    name = None
    if a.username:
        name, _ = get_user(auth.load_users(), a.username)
    print(f"Ended {auth.end_sessions(name)} session(s){' for ' + name if name else ''}.")


def cmd_list(_):
    users = auth.load_users()
    if not users:
        print("No accounts yet. Create one: python3 manage.py add-user <name> --admin")
        return
    now = time.time()
    print(f"{'username':24} {'email':30} {'sign-in':16} {'state':10} last sign-in")
    for name, u in sorted(users.items()):
        how = ("password+Google" if u.get("email") else "password") if u.get("password") else "Google"
        state = ("disabled" if u.get("disabled") else "locked" if u.get("locked_until", 0) > now else "active")
        last = datetime.fromtimestamp(u["last_login"]).strftime("%Y-%m-%d %H:%M") if u.get("last_login") else "never"
        print(f"{name + (' *' if u.get('admin') else ''):24} {u.get('email') or '-':30} {how:16} {state:10} {last}")
    print("\n* admin")


def cmd_status(_):
    users = auth.load_users()
    cfg = auth.google_config(f"{DEFAULT_URL}/auth/google/callback")
    print(f"Accounts: {len(users)} ({sum(1 for u in users.values() if u.get('admin'))} admin)")
    print(f"Account file: {auth.USERS}")
    if cfg["enabled"]:
        print("Google sign-in: on")
        print(f"  Redirect URI to register with Google: {cfg['redirect']}")
        print(f"  Allowed Workspace domain(s): {', '.join(cfg['domains']) or 'none; only accounts added with --email'}")
    else:
        print("Google sign-in: off (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env)")


def main():
    ap = argparse.ArgumentParser(description="Manage dashboard accounts. Changes apply immediately.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-user", help="create an account")
    p.add_argument("username")
    p.add_argument("--email", help="Google address this person can also sign in with")
    p.add_argument("--name", help="display name")
    p.add_argument("--admin", action="store_true")
    p.add_argument("--google-only", action="store_true", help="no password; Google sign-in only")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("set-password", help="reset a password (also unlocks and signs out)")
    p.add_argument("username")
    p.add_argument("--password-stdin", action="store_true")
    p.set_defaults(fn=cmd_set_password)

    for cmd, fn, help_ in (("unlock", cmd_unlock, "clear a lockout after failed attempts"),
                           ("disable", lambda a: cmd_toggle(a, True), "block an account and sign it out"),
                           ("enable", lambda a: cmd_toggle(a, False), "re-enable an account")):
        p = sub.add_parser(cmd, help=help_)
        p.add_argument("username")
        p.set_defaults(fn=fn)

    p = sub.add_parser("remove-user", help="delete an account")
    p.add_argument("username")
    p.add_argument("--yes", action="store_true", help="don't ask to confirm")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("login-link", help="print a one-time sign-in link (admin override)")
    p.add_argument("username")
    p.add_argument("--minutes", type=int, default=10)
    p.add_argument("--url", default=DEFAULT_URL, help=f"dashboard address (default {DEFAULT_URL})")
    p.set_defaults(fn=cmd_link)

    p = sub.add_parser("sign-out", help="end sessions for one user, or everyone")
    p.add_argument("username", nargs="?")
    p.set_defaults(fn=cmd_sign_out)

    sub.add_parser("list", help="show accounts").set_defaults(fn=cmd_list)
    sub.add_parser("status", help="show sign-in settings").set_defaults(fn=cmd_status)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
