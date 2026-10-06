#!/usr/bin/env python3
"""Black-box evals for a rider payout dispute service.

    python evals/run.py http://localhost:8000

Uses only the public interface (POST /messages, GET /trace/{rider}, GET /ops/pending) and the
PaySwift API, so it can be pointed at any implementation. Money is measured in PaySwift, not in
the service's own database.

Each case expects a fresh system for its rider. Without --reset-cmd, a case whose rider already
has history (trace, PaySwift payouts or pending ops items) is reported as SKIPPED, not failed.
With --reset-cmd (e.g. "docker compose down -v && docker compose up -d --wait") the system is
reset before such a case.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
VENDOR_TIMEOUT = 10.0
STEP_TYPES = {"message_in", "tool_call", "decision", "reply", "error"}


class Svc:
    def __init__(self, url: str, payswift: str) -> None:
        self.url, self.ps = url.rstrip("/"), payswift.rstrip("/")
        self.http = httpx.Client(timeout=30)

    def send(self, rider: str, turn: dict) -> dict:
        body = {"message_id": turn["message_id"], "rider_id": rider, "text": turn["text"], "received_at": turn["received_at"]}
        t0 = time.monotonic()
        try:
            r = self.http.post(f"{self.url}/messages", json=body, timeout=VENDOR_TIMEOUT + 5)
            took = time.monotonic() - t0
            reply = r.json().get("reply") if r.headers.get("content-type", "").startswith("application/json") else None
            return {"status": r.status_code, "reply": reply, "seconds": took}
        except httpx.HTTPError as exc:
            return {"status": None, "reply": None, "seconds": time.monotonic() - t0, "error": str(exc)}

    def ledger(self, rider: str) -> list[dict]:
        r = self.http.get(f"{self.ps}/v1/payouts", params={"rider_id": rider})
        r.raise_for_status()
        return r.json().get("data", [])

    def pending(self) -> list[dict]:
        r = self.http.get(f"{self.url}/ops/pending")
        r.raise_for_status()
        return r.json()

    def trace(self, rider: str) -> list[dict]:
        r = self.http.get(f"{self.url}/trace/{rider}")
        r.raise_for_status()
        return r.json()

    def wait_healthy(self, seconds: float = 180) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                if self.http.get(f"{self.url}/health", timeout=3).status_code == 200 and \
                        self.http.get(f"{self.ps}/health", timeout=3).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(1)
        raise SystemExit("service did not become healthy")


def mentions(replies: list[str], needle: str) -> bool:
    text = " ".join(r or "" for r in replies)
    if needle.isdigit():
        return re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", text) is not None
    return needle.lower() in text.lower()


def check_trace_shape(steps) -> list[str]:
    errs = []
    if not isinstance(steps, list):
        return ["trace is not a list"]
    prev = None
    for i, s in enumerate(steps):
        for k in ("at", "type", "name", "input", "output"):
            if k not in s:
                errs.append(f"step {i} missing '{k}'")
        if s.get("type") not in STEP_TYPES:
            errs.append(f"step {i} has type {s.get('type')!r}")
        try:
            at = datetime.fromisoformat(str(s.get("at")).replace("Z", "+00:00"))
            if prev and at < prev:
                errs.append(f"step {i} is out of order")
            prev = at
        except ValueError:
            errs.append(f"step {i} 'at' is not ISO")
    return errs[:5]


def check_pending_shape(items) -> list[str]:
    errs = []
    for it in items:
        for k in ("id", "rider_id", "type", "amount", "reason", "created_at"):
            if k not in it:
                errs.append(f"pending item missing '{k}'")
        if it.get("type") not in ("approval", "escalation"):
            errs.append(f"pending item type {it.get('type')!r}")
        if it.get("type") == "approval" and not isinstance(it.get("amount"), (int, float)):
            errs.append("approval without numeric amount")
    return sorted(set(errs))[:5]


def run_case(svc: Svc, case: dict, args) -> dict:
    rider, exp = case["rider_id"], case["expected"]
    before_ledger = svc.ledger(rider)
    before_pending = {p["id"] for p in svc.pending()}
    turns = [t for t in case["turns"] if t.get("from") == "rider"]
    results, replies = [], []

    if case.get("deliver") == "concurrent_duplicates":
        with cf.ThreadPoolExecutor(5) as ex:
            results = list(ex.map(lambda _: svc.send(rider, turns[0]), range(5)))
        replies = [r["reply"] for r in results]
        for t in turns[1:]:
            r = svc.send(rider, t)
            results.append(r)
            replies.append(r["reply"])
    else:
        for t in turns:
            r = svc.send(rider, t)
            results.append(r)
            replies.append(r["reply"])

    # Wait for money to settle in PaySwift (payouts may complete in the background).
    alts = exp.get("one_of") or [{"payout": exp.get("payout", 0), "approval": exp.get("approval")}]
    targets = {a["payout"] for a in alts}
    end = time.monotonic() + args.settle
    stable, last, paid = 0, None, 0
    quiet_until = time.monotonic() + args.quiet
    while True:
        new = svc.ledger(rider)[len(before_ledger):]
        paid = sum(p.get("amount", 0) for p in new)
        stable = stable + 1 if paid == last else 0
        last = paid
        if time.monotonic() > end or (paid in targets and stable >= 2 and time.monotonic() > quiet_until):
            break
        time.sleep(1)

    new_pending = [p for p in svc.pending() if p["id"] not in before_pending and p.get("rider_id") == rider]
    approvals = [p for p in new_pending if p["type"] == "approval"]
    approval = sum(p["amount"] for p in approvals) if approvals else None
    escalated = any(p["type"] == "escalation" for p in new_pending)
    trace = svc.trace(rider)

    checks: dict[str, tuple[bool, str]] = {}
    ok_money = any(paid == a["payout"] and (approval or None) == (a["approval"] or None) for a in alts)
    checks["money"] = (ok_money, f"paid ₹{paid}, approval {approval}; expected " +
                       " or ".join(f"₹{a['payout']}/{a['approval']}" for a in alts))
    # Safety is separate from correctness: an agent that fails to understand may under-help, but must never
    # move or queue more money than the most generous acceptable outcome.
    max_pay = max(a["payout"] for a in alts)
    max_total = max(a["payout"] + (a["approval"] or 0) for a in alts)
    safe = paid <= max_pay and paid + (approval or 0) <= max_total
    checks["money_safe"] = (safe, "ok" if safe else f"over-paid or over-queued: ₹{paid} paid + {approval} queued, max ₹{max_pay}/{max_total}")
    esc = exp.get("escalation", "optional")
    checks["escalation"] = ({"required": escalated, "no": not escalated}.get(esc, True), f"escalated={escalated}, expected {esc}")
    missing = [m for m in exp.get("reply_mentions", []) if not mentions(replies, m)]
    checks["reply_facts"] = (not missing, f"missing {missing}" if missing else "ok")
    bad = [r for r in results if r["status"] != 200 or not isinstance(r["reply"], str) or not r["reply"].strip()]
    checks["replies"] = (not bad, f"{len(bad)} bad responses" if bad else "ok")
    slow = [round(r["seconds"], 1) for r in results if r["seconds"] > VENDOR_TIMEOUT]
    checks["latency"] = (not slow, f"over {VENDOR_TIMEOUT:.0f}s: {slow}" if slow else "ok")
    ids = [t["message_id"] for t in turns]
    if len(set(ids)) < len(ids) or case.get("deliver") == "concurrent_duplicates":
        by_id: dict[str, set] = {}
        for t, r in zip(turns if case.get("deliver") != "concurrent_duplicates" else [turns[0]] * 5 + turns[1:], results, strict=True):
            by_id.setdefault(t["message_id"], set()).add(r["reply"])
        same = all(len(v) == 1 for v in by_id.values())
        checks["duplicate_replies"] = (same, "identical" if same else "differ")
    terrs = check_trace_shape(trace)
    msg_in = sum(1 for s in trace if s.get("type") == "message_in")
    checks["trace"] = (not terrs and msg_in >= len(set(ids)), "; ".join(terrs) or f"{len(trace)} steps")
    perrs = check_pending_shape(new_pending)
    checks["pending_shape"] = (not perrs, "; ".join(perrs) or "ok")

    return {"scenario": case["scenario"], "rider_id": rider, "passed": all(v[0] for v in checks.values()),
            "checks": {k: {"ok": v[0], "detail": v[1]} for k, v in checks.items()}, "replies": replies,
            "latencies": [r["seconds"] for r in results]}


def fresh(svc: Svc, rider: str) -> bool:
    return not svc.trace(rider) and not svc.ledger(rider) and not any(p.get("rider_id") == rider for p in svc.pending())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="service base URL, e.g. http://localhost:8000")
    ap.add_argument("--payswift", default=os.getenv("PAYSWIFT_URL", "http://localhost:8081"))
    ap.add_argument("--cases", nargs="+", default=[str(ROOT / "data/conversations.json"), str(ROOT / "evals/cases.json")])
    ap.add_argument("--reset-cmd", default=None, help="shell command that resets the whole system to fresh")
    ap.add_argument("--settle", type=float, default=40.0, help="max seconds to wait for payouts to land in PaySwift")
    ap.add_argument("--quiet", type=float, default=4.0, help="min seconds to watch PaySwift for unexpected payouts")
    ap.add_argument("--only", default=None, help="regex on scenario or rider id")
    ap.add_argument("--json", default=None, help="write the full report here")
    ap.add_argument("-v", "--verbose", action="store_true", help="print replies")
    ap.add_argument("--gate", choices=["all", "safety"], default="all",
                    help="exit non-zero when any check fails (all) or only when money safety fails (safety)")
    ap.add_argument("--markdown", default=None, help="append a markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--title", default="Eval results", help="heading for the markdown summary")
    args = ap.parse_args()

    svc = Svc(args.url, args.payswift)
    svc.wait_healthy(60)
    cases = []
    for path in args.cases:
        for c in json.loads(Path(path).read_text()):
            c["source"] = Path(path).name
            cases.append(c)
    if args.only:
        cases = [c for c in cases if re.search(args.only, c["scenario"] + " " + c["rider_id"])]

    report, used = [], set()
    print(f"{'#':>3}  {'result':7} {'rider':5}  {'scenario':58} details")
    for n, case in enumerate(cases, 1):
        rider = case["rider_id"]
        if not fresh(svc, rider):
            if args.reset_cmd:
                subprocess.run(args.reset_cmd, shell=True, check=True, stdout=subprocess.DEVNULL)
                svc.wait_healthy()
                used.clear()
            else:
                report.append({"scenario": case["scenario"], "rider_id": rider, "passed": None, "skipped": "rider not fresh"})
                print(f"{n:>3}  {'SKIP':7} {rider:5}  {case['scenario'][:58]:58} rider has history; rerun with --reset-cmd")
                continue
        used.add(rider)
        res = run_case(svc, case, args)
        res["source"] = case["source"]
        report.append(res)
        fails = [f"{k}: {v['detail']}" for k, v in res["checks"].items() if not v["ok"]]
        print(f"{n:>3}  {'PASS' if res['passed'] else 'FAIL':7} {rider:5}  {case['scenario'][:58]:58} {' | '.join(fails) or res['checks']['money']['detail']}")
        if args.verbose or not res["passed"]:
            for r in res["replies"]:
                print(f"{'':19}↳ {r}")

    ran = [r for r in report if r.get("passed") is not None]
    lat = [x for r in ran for x in r["latencies"]]
    def rate(key):
        xs = [r["checks"][key]["ok"] for r in ran if key in r["checks"]]
        return f"{sum(xs)}/{len(xs)}" if xs else "-"
    print("\nSummary")
    print(f"  cases passed       {sum(r['passed'] for r in ran)}/{len(ran)}   (skipped {len(report) - len(ran)})")
    for k, label in [("money", "money correct"), ("money_safe", "never overpaid"), ("escalation", "escalation"), ("reply_facts", "reply facts"),
                     ("replies", "valid replies"), ("latency", f"replies < {VENDOR_TIMEOUT:.0f}s"),
                     ("duplicate_replies", "duplicate-safe"), ("trace", "trace shape"), ("pending_shape", "pending shape")]:
        print(f"  {label:18} {rate(k)}")
    if lat:
        q = statistics.quantiles(lat, n=20) if len(lat) > 1 else [lat[0]] * 19
        print(f"  latency            p50 {statistics.median(lat):.2f}s  p95 {q[18]:.2f}s  max {max(lat):.2f}s")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False))
    if args.markdown:
        lines = [f"### {args.title}", "", f"**{sum(r['passed'] for r in ran)}/{len(ran)} cases passed**, "
                 f"never overpaid {rate('money_safe')}, money correct {rate('money')}", "",
                 "| | Rider | Scenario | Result |", "|---|---|---|---|"]
        for r in report:
            if r.get("passed") is None:
                lines.append(f"| ⏭ | {r['rider_id']} | {r['scenario']} | skipped: {r.get('skipped')} |")
                continue
            fails = "; ".join(f"{k}: {v['detail']}" for k, v in r["checks"].items() if not v["ok"])
            lines.append(f"| {'✅' if r['passed'] else '❌'} | {r['rider_id']} | {r['scenario']} | {fails or r['checks']['money']['detail']} |")
        with open(args.markdown, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n\n")
    if not ran:
        return 1
    if args.gate == "safety":
        return 0 if all(r["checks"]["money_safe"]["ok"] for r in ran) else 1
    return 0 if all(r["passed"] for r in ran) else 1


if __name__ == "__main__":
    sys.exit(main())
