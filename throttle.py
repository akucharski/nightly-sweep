"""
Request pacing for the crawl: slow, irregular, and quick to back off.

Every request to the site waits a randomised gap after the one before it, and
only one request is ever in flight per host, so the site sees one steady visitor
rather than a burst. Now and then the pacer adds a longer "reading" pause. If
the server answers 429 or 503 it honours Retry-After, halves its own speed and
tries once more; after a run of calm responses it speeds back up.

Pacing is about not being a burden. The crawler still names itself in its
User-Agent and still obeys robots.txt, including any Crawl-delay it sets.
"""

import random
import threading
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import requests

PROFILES = {
    "human":    {"label": "Human-paced", "min": 4.0, "max": 12.0, "pause_chance": 0.08, "pause": [20, 45]},
    "polite":   {"label": "Polite", "min": 1.5, "max": 4.0, "pause_chance": 0.04, "pause": [8, 15]},
    "standard": {"label": "Standard", "min": 0.4, "max": 0.8, "pause_chance": 0.0, "pause": [0, 0]},
}

OTHER_HOST_GAP = (0.3, 1.0)     # link checks against other sites, still paced
BACKOFF_STATUSES = (429, 503)
MAX_MULTIPLIER = 16
MAX_RETRY_AFTER = 300            # seconds; never sit longer than this on one request
CALM_TO_SPEED_UP = 10            # good responses in a row before halving the slowdown


class Cancelled(Exception):
    pass


def host_key(url):
    return urlparse(url).netloc.lower().replace("www.", "")


def profile(settings):
    """The pace for a project's settings: a named profile, or custom min/max seconds."""
    if settings.get("speed") in PROFILES:
        return PROFILES[settings["speed"]]
    lo = float(settings.get("custom_min", 2))
    hi = float(settings.get("custom_max", 5))
    return {"label": f"Custom {lo:g}–{hi:g}s", "min": lo, "max": max(lo, hi),
            "pause_chance": 0.0, "pause": [0, 0]}


def retry_after(resp, default):
    value = (resp.headers.get("Retry-After") or "").strip()
    seconds = default
    if value.isdigit():
        seconds = int(value)
    elif value:
        try:
            seconds = (parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            pass
    return min(max(seconds, 1), MAX_RETRY_AFTER)


class Pacer:
    def __init__(self, home, pace, floor=0.0, cancel=None, on_event=None):
        self.home = home
        self.min_gap, self.max_gap = pace["min"], pace["max"]
        self.pause_chance, self.pause = pace["pause_chance"], pace["pause"]
        self.floor = floor                       # robots.txt Crawl-delay, if any
        self.cancel = cancel or threading.Event()
        self.on_event = on_event or (lambda kind, msg: None)
        self.multiplier = 1.0
        self.calm = 0
        self._lock = threading.Lock()
        self._hosts = {}                         # host -> Lock: one request in flight
        self._next = {}                          # host -> earliest next start

    def host_lock(self, host):
        with self._lock:
            return self._hosts.setdefault(host, threading.Lock())

    def sleep(self, seconds):
        if seconds > 0 and self.cancel.wait(seconds):
            raise Cancelled()
        if self.cancel.is_set():
            raise Cancelled()

    def before(self, host):
        self.sleep(self._next.get(host, 0) - time.monotonic())

    def after(self, host, status):
        if host != self.home:
            self._next[host] = time.monotonic() + random.uniform(*OTHER_HOST_GAP)
            return
        gap = max(self.floor, random.uniform(self.min_gap, self.max_gap)) * self.multiplier
        if self.pause_chance and random.random() < self.pause_chance:
            extra = random.uniform(*self.pause)
            gap += extra
            self.on_event("pause", f"Reading pause, {extra:.0f}s")
        if self.multiplier > 1 and status is not None and status < 400:
            self.calm += 1
            if self.calm >= CALM_TO_SPEED_UP:
                self.multiplier = max(1.0, self.multiplier / 2)
                self.calm = 0
                self.on_event("fast", f"Server is calm again; pace back to x{self.multiplier:g}")
        self._next[host] = time.monotonic() + gap

    def backoff(self, host, resp):
        wait = retry_after(resp, 30 if host == self.home else 10)
        if host == self.home:
            self.multiplier = min(self.multiplier * 2, MAX_MULTIPLIER)
            self.calm = 0
            self.on_event("slow", f"Site answered {resp.status_code}; waiting {wait:.0f}s, "
                                  f"then crawling x{self.multiplier:g} slower")
        else:
            self.on_event("slow", f"{host} answered {resp.status_code}; waiting {wait:.0f}s")
        self.sleep(wait)


class PacedSession(requests.Session):
    """A requests.Session whose every request goes through the pacer."""

    def __init__(self, pacer):
        super().__init__()
        self.pacer = pacer

    def request(self, method, url, *args, **kwargs):
        host = host_key(url)
        with self.pacer.host_lock(host):
            self.pacer.before(host)
            resp = None
            try:
                resp = super().request(method, url, *args, **kwargs)
                if resp.status_code in BACKOFF_STATUSES:
                    self.pacer.backoff(host, resp)
                    resp = super().request(method, url, *args, **kwargs)
                return resp
            finally:
                self.pacer.after(host, resp.status_code if resp is not None else None)
