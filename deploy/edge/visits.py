#!/usr/bin/env python3
"""Who opened the product: a summary of Caddy's JSON access log on the edge server.

    visits.py                       # everyone, from all log files
    visits.py --since 13:00         # local time today (Asia/Almaty)
    visits.py --follow              # print one line when a new visitor appears, then keep watching
    visits.py --ignore 1.2.3.4 ...  # addresses to leave out (also read from /etc/burnrate-visits-ignore)

A visitor is one client address. For each: first and last request, how many pages were opened, what they did
(started the demo, moved the demo clock, took the quiz, paid, rehearsed a call, ...), the browser and where
they came from. Static files and health checks are not counted as activity.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
import time

LOG = "/var/log/caddy/burnrate-access.log"
IGNORE_FILE = "/etc/burnrate-visits-ignore"
TZ = dt.timezone(dt.timedelta(hours=5))  # Asia/Almaty / Astana

ACTIONS = [
    (re.compile(r"^POST /api/session"), "opened"),
    (re.compile(r"^POST /api/demo/start"), "demo"),
    (re.compile(r"^POST /api/demo/clock"), "demo clock"),
    (re.compile(r"^(PUT|POST) /api/quiz"), "quiz"),
    (re.compile(r"^GET /api/quiz/result"), "result"),
    (re.compile(r"^GET /api/billing/plans"), "plans"),
    (re.compile(r"^POST /api/billing/checkout$"), "checkout"),
    (re.compile(r"^POST /api/billing/checkout/[^/]+/pay"), "paid"),
    (re.compile(r"^POST /api/rehearsal"), "rehearsal"),
    (re.compile(r"^POST /api/statements"), "statement"),
    (re.compile(r"^POST /api/asks/[^/]+/outcome"), "ask outcome"),
    (re.compile(r"^POST /api/instructions/"), "payday action"),
    (re.compile(r"^GET /api/(cards|asks|statements)"), "browsed app"),
]
STATIC = re.compile(r"^/(assets/|favicon|robots\.txt|api/health|healthz)")
BOT = re.compile(r"bot|crawl|spider|preview|headless|python|curl|wget|httpx|go-http|node-fetch|okhttp|scan", re.I)


def browser(ua: str) -> str:
    if not ua:
        return "?"
    if BOT.search(ua):
        return "bot/script: " + ua[:60]
    os_ = next((n for k, n in [("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Mac OS X", "Mac"),
                               ("Windows", "Windows"), ("Linux", "Linux")] if k in ua), "?")
    br = next((n for k, n in [("Edg/", "Edge"), ("YaBrowser", "Yandex"), ("OPR/", "Opera"), ("Telegram", "Telegram"),
                              ("Chrome/", "Chrome"), ("Firefox/", "Firefox"), ("Safari/", "Safari")] if k in ua), "?")
    return f"{br} on {os_}"


def entries(paths):
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue
        except OSError:
            continue


def parse(e):
    r = e.get("request") or {}
    h = r.get("headers") or {}
    ip = r.get("client_ip") or r.get("remote_ip") or "?"
    uri = r.get("uri", "")
    ts = dt.datetime.fromtimestamp(e.get("ts", 0), TZ)
    return ip, ts, r.get("method", ""), uri, e.get("status", 0), (h.get("User-Agent") or [""])[0], (h.get("Referer") or [""])[0]


class Visitor:
    def __init__(self, ip, ts):
        self.ip, self.first, self.last = ip, ts, ts
        self.requests = self.pages = 0
        self.actions: dict[str, int] = {}
        self.uas: set[str] = set()
        self.refs: set[str] = set()

    def add(self, ts, method, uri, status, ua, ref):
        self.last = max(self.last, ts)
        if STATIC.match(uri):
            return
        self.requests += 1
        path = uri.split("?")[0]
        if method == "GET" and not path.startswith("/api/") and not path.startswith("/hooks/"):
            self.pages += 1
        key = f"{method} {path}"
        for rx, name in ACTIONS:
            if rx.match(key):
                self.actions[name] = self.actions.get(name, 0) + 1
                break
        if ua:
            self.uas.add(browser(ua))
        if ref and "burnrate.67.207.94.96.sslip.io" not in ref:
            self.refs.add(ref[:60])

    def line(self):
        acts = ", ".join(f"{k}×{v}" if v > 1 else k for k, v in self.actions.items()) or "looked only"
        refs = f" | from {', '.join(sorted(self.refs))}" if self.refs else ""
        return (f"{self.first:%H:%M}–{self.last:%H:%M}  {self.ip:<15}  {self.pages:>2} pages, {self.requests:>3} requests  "
                f"| {acts} | {'; '.join(sorted(self.uas)) or '?'}{refs}")


def load_ignore(extra):
    ips = set(extra)
    if os.path.exists(IGNORE_FILE):
        ips |= {x.strip() for x in open(IGNORE_FILE) if x.strip() and not x.startswith("#")}
    return ips


def summary(since, ignore):
    files = sorted(glob.glob(LOG + "*"), key=os.path.getmtime)
    people: dict[str, Visitor] = {}
    for e in entries(files):
        ip, ts, method, uri, status, ua, ref = parse(e)
        if ip in ignore or (since and ts < since):
            continue
        v = people.setdefault(ip, Visitor(ip, ts))
        v.first = min(v.first, ts)
        v.add(ts, method, uri, status, ua, ref)
    shown = [v for v in people.values() if v.requests]
    shown.sort(key=lambda v: v.first)
    print(f"{len(shown)} visitor(s){' since ' + since.strftime('%H:%M') if since else ''} (times Asia/Almaty, own addresses left out)")
    for v in shown:
        print("  " + v.line())


def follow(ignore):
    """Print a line for every new address; the first meaningful request of a visitor is enough."""
    seen = set(ignore)
    for e in entries(sorted(glob.glob(LOG + "*"), key=os.path.getmtime)):
        seen.add(parse(e)[0])
    f = open(LOG, encoding="utf-8", errors="replace")
    f.seek(0, os.SEEK_END)
    while True:
        line = f.readline()
        if not line:
            time.sleep(1)
            if os.path.getsize(LOG) < f.tell():  # rolled
                f = open(LOG, encoding="utf-8", errors="replace")
            continue
        try:
            ip, ts, method, uri, status, ua, ref = parse(json.loads(line))
        except ValueError:
            continue
        if ip in seen or STATIC.match(uri):
            continue
        seen.add(ip)
        src = f" from {ref[:60]}" if ref and "sslip.io" not in ref else ""
        print(f"NEW VISITOR {ts:%H:%M:%S} {ip} {browser(ua)} first: {method} {uri.split('?')[0]}{src}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", help="HH:MM local time today")
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--ignore", nargs="*", default=[])
    a = ap.parse_args()
    ignore = load_ignore(a.ignore)
    if a.follow:
        follow(ignore)
        return
    since = None
    if a.since:
        h, m = map(int, a.since.split(":"))
        since = dt.datetime.now(TZ).replace(hour=h, minute=m, second=0, microsecond=0)
    summary(since, ignore)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
