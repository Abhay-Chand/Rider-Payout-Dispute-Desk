"""Deterministic rider replies built only from engine facts (used by the offline planner and as the
fallback whenever an LLM reply fails the guard). Hinglish by default, English if the rider wrote English."""
from __future__ import annotations

from datetime import date

from app.domain import policy

MAX_LISTED_TRIPS = 6


def fmt_date(iso: str | date) -> str:
    d = date.fromisoformat(iso) if isinstance(iso, str) else iso
    return f"{d.day} {d.strftime('%b')}"


def _x(v: float) -> str:
    return f"{v:g}x"


def _item_line(it: dict, en: bool) -> str:
    t, d = it.get("trip_id"), it.get("detail") or {}
    exp, paid, issue = it["expected"], it["paid"], it["issue"]
    if issue == "surge_not_applied":
        return (f"order {t} had {_x(d['surge'])} surge: ₹{exp} was due, you got ₹{paid}" if en
                else f"order {t} pe {_x(d['surge'])} surge tha: ₹{exp} banta tha, ₹{paid} mila")
    if issue == "missing_payment":
        return f"order {t} (₹{exp}) was not paid" if en else f"order {t} ka ₹{exp} nahi aaya tha"
    if issue == "fare_mismatch":
        return (f"order {t} ({d.get('distance_km'):g} km) should pay ₹{exp}, you got ₹{paid}" if en
                else f"order {t} ({d.get('distance_km'):g} km) ka ₹{exp} banta tha, ₹{paid} mila")
    if issue == "duplicate_penalty":
        return (f"the cancellation penalty for {t} was deducted twice (₹{-paid} instead of ₹{-exp})" if en
                else f"order {t} ka cancel penalty do baar kata (₹{-paid} kata, sirf ₹{-exp} banta tha)")
    if issue == "missing_incentive":
        n = d.get("completed_trips")
        return (f"you completed {n} trips, so the ₹{exp} daily incentive was due but not paid" if en
                else f"aapke {n} trips complete hue the, ₹{exp} incentive nahi mila tha")
    return f"₹{exp - paid} short ({issue})" if en else f"₹{exp - paid} kam aaya ({issue})"


def _items_text(items: list[dict], en: bool) -> str:
    missing = [i for i in items if i["issue"] == "missing_payment"]
    others = [i for i in items if i["issue"] != "missing_payment"]
    parts = [_item_line(i, en) for i in others]
    if len(missing) == 1:
        parts.append(_item_line(missing[0], en))
    elif missing:
        ids = ", ".join(i["trip_id"] for i in missing[:MAX_LISTED_TRIPS]) + ("..." if len(missing) > MAX_LISTED_TRIPS else "")
        total = sum(i["expected"] - i["paid"] for i in missing)
        parts.append(f"{len(missing)} orders were not paid ({ids}), ₹{total} in total" if en
                     else f"{len(missing)} orders ka paisa nahi aaya tha ({ids}), total ₹{total}")
    return "; ".join(parts)


def _approval_why(reason: str | None, en: bool) -> str:
    r = reason or ""
    if "shadow mode" in r:
        return "every payment is currently confirmed by the ops team" if en else "abhi har payment ops team confirm karti hai"
    if "budget" in r:
        return "today's limit for automatic payments has been reached" if en else "aaj ke automatic payments ki limit poori ho gayi hai"
    if "once per day" in r:
        return "only one automatic payment is allowed per day" if en else "ek din mein ek hi auto-payment ho sakta hai"
    if "limit" in r:
        return (f"payments above ₹{_limit(r)} need ops approval" if en
                else f"₹{_limit(r)} se zyada ka payment ops team approve karti hai")
    if "PaySwift" in r:
        return "I could not verify past payments right now" if en else "abhi purane payments verify nahi ho paye"
    return "it needs a manual check" if en else "isko manual check chahiye"


def _limit(reason: str) -> int:
    import re
    m = re.search(r"₹(\d+) auto-pay limit", reason)
    return int(m.group(1)) if m else 200


def _no_change(o: dict, en: bool) -> str:
    st = o.get("statement") or {}
    claim = o.get("claim") or {}
    issue, d = claim.get("issue", "general"), fmt_date(o["date"])
    n = st.get("completed_trips", 0)
    disc = st.get("discrepancies") or []
    if issue == "incentive":
        inc = next((x for x in disc if x.get("kind") == "incentive"), None)
        if n >= policy.INCENTIVE_MIN_TRIPS:
            return (f"On {d} you had {n} completed trips and the ₹{policy.DAILY_INCENTIVE} incentive was already paid." if en
                    else f"{d} ko aapke {n} completed trips the aur ₹{policy.DAILY_INCENTIVE} incentive mil chuka hai.")
        _ = inc
        return (f"On {d} our records show {n} completed trips. The incentive needs {policy.INCENTIVE_MIN_TRIPS} completed trips, so it was not due." if en
                else f"{d} ko humare record mein aapke {n} completed trips hain. Incentive {policy.INCENTIVE_MIN_TRIPS} trips pe milta hai, isliye nahi bana.")
    if issue == "penalty_duplicate":
        return (f"On {d} each rider cancellation was charged ₹10 once, which is correct." if en
                else f"{d} ko har rider-cancel pe ₹10 penalty ek hi baar kata hai, jo sahi hai.")
    return (f"I checked {d}: {n} completed trips, ₹{st.get('expected_total')} was due and ₹{st.get('paid_total')} was paid. Nothing is short." if en
            else f"{d} ka payout check kiya: {n} completed trips, ₹{st.get('expected_total')} banta tha aur ₹{st.get('paid_total')} mila. Koi kami nahi hai.")


def outcome_text(o: dict, en: bool, trip_facts: dict[str, dict] | None = None) -> str:
    d = fmt_date(o["date"])
    status = o["status"]
    claim = o.get("claim") or {}
    trip_facts = trip_facts or {}
    if status == "auto_paid":
        sent = o.get("payout_status") == "paid"
        what = _items_text(o["items"], en)
        amt = o["amount"]
        tail = ((f"I have sent you ₹{amt}." if sent else f"₹{amt} is being sent to you now, it should arrive shortly.") if en
                else (f"₹{amt} aapko bhej diya hai." if sent else f"₹{amt} ka payment process ho raha hai, thodi der mein aa jayega."))
        return f"{d}: {what}. {tail}"
    if status == "approval_pending":
        what = _items_text(o["items"], en)
        why = _approval_why(o.get("approval_reason"), en)
        return (f"{d}: {what}. ₹{o['amount']} is due; {why}, so it has gone to the ops team for approval and will be paid once approved."
                if en else f"{d}: {what}. ₹{o['amount']} banta hai; {why}, isliye ye ops team approve karegi, approve hote hi aa jayega.")
    if status == "already_handled":
        prev = o.get("previously_handled") or []
        total = sum(p["difference"] for p in prev)
        pending = any(p.get("state") == "awaiting_approval" for p in prev)
        rejected = prev and all(p.get("state") == "rejected" for p in prev)
        if rejected:
            return (f"{d}: the ops team already reviewed this and did not approve it." if en
                    else f"{d}: ops team ise pehle hi review kar chuki hai aur approve nahi hua.")
        if pending:
            return (f"{d}: ₹{total} is already with the ops team for approval." if en
                    else f"{d}: ₹{total} pehle se ops approval mein hai, approve hote hi aa jayega.")
        return (f"{d}: the ₹{total} difference has already been paid to you." if en
                else f"{d}: ₹{total} ka farak aapko pehle hi bhej diya gaya hai.")
    if status == "out_of_window":
        return (f"{d} is more than {_window(o)} days ago. We can only review payouts from the last {_window(o)} days." if en
                else f"{d} {_window(o)} din se purana hai. Hum sirf pichhle {_window(o)} din ke payout disputes dekh sakte hain.")
    if status == "no_data":
        return (f"I could not find any trips or payouts for you on {d}. Could you check the date or share the order ID?" if en
                else f"{d} ko aapke koi trip ya payout record nahi mile. Date ya order ID dobara check karke bata dijiye.")
    # nothing_owed
    for tid in claim.get("trip_ids") or []:
        f = trip_facts.get(tid)
        if f and f.get("found"):
            if claim.get("issue") == "surge" and f["surge"] == 1:
                return (f"Order {tid} on {d} had no surge (1x). You were paid ₹{f['paid']}, which is correct for {f['distance_km']:g} km." if en
                        else f"Order {tid} ({d}) pe surge nahi tha (1x). {f['distance_km']:g} km ke hisaab se ₹{f['paid']} sahi mila hai.")
            if f["status"] == "cancelled_by_customer":
                return (f"Order {tid} on {d} was cancelled by the customer. Customer cancellations don't earn a fare, and you weren't charged a penalty." if en
                        else f"Order {tid} ({d}) customer ne cancel kiya tha. Customer-cancel pe fare nahi banta, aur aapka koi penalty bhi nahi kata.")
            if f["status"] == "cancelled_by_rider" and f.get("issue") == "ok":
                return (f"Order {tid} on {d} was cancelled from your side, so the ₹10 rider-cancellation penalty applies and was charged once." if en
                        else f"Order {tid} ({d}) aapki taraf se cancel hua tha, isliye ₹10 penalty ek baar kati hai, jo policy ke hisaab se sahi hai.")
            if f.get("issue") == "ok" and f["status"] == "completed":
                return (f"Order {tid} on {d}: ₹{f['expected']} was due ({f['distance_km']:g} km, {_x(f['surge'])}) and ₹{f['paid']} was paid. That is correct." if en
                        else f"Order {tid} ({d}): {f['distance_km']:g} km, {_x(f['surge'])} pe ₹{f['expected']} banta tha aur ₹{f['paid']} mila. Ye sahi hai.")
    return _no_change(o, en)


def _window(o: dict) -> int:
    import re
    m = re.search(r"last (\d+) days", o.get("reason", ""))
    return int(m.group(1)) if m else 7


def claimed_amount_note(claimed: int | None, found: int, en: bool) -> str:
    if not claimed or claimed == found:
        return ""
    return (f" You mentioned ₹{claimed}, but our records show only a ₹{found} difference." if en
            else f" Aapne ₹{claimed} bataya, lekin record mein ₹{found} ka hi farak mila.")


def claimed_count_note(claimed: int | None, actual: int, en: bool) -> str:
    if not claimed or claimed <= actual:
        return ""
    return (f" (You mentioned {claimed}; if you think our record is wrong, tell me and I'll send it to the ops team.)" if en
            else f" (Aapne {claimed} bataya; agar lagta hai record galat hai to bataiye, main ops team ko bhej dunga.)")


ASK_DETAILS = {
    False: "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye.",
    True: "Which day or which order looks wrong? Please share the date or the order ID.",
}
GREETING = {
    False: "Namaste! Payout mein koi problem hai to date ya order ID ke saath bataiye, main check kar deta hoon.",
    True: "Hello! If something is wrong with a payout, tell me the date or order ID and I'll check it.",
}
SECURITY = {
    False: "Main sirf is number se jude account ke payout ki madad kar sakta hoon. Aapke apne account mein koi problem ho to date ya order ID bataiye.",
    True: "I can only help with the payouts of the account linked to this number. If something is wrong with your own payouts, share the date or order ID.",
}
HUMAN = {
    False: "Maine aapki baat ops team ko bhej di hai, wo aapse sampark karenge.",
    True: "I've passed this to the ops team; they will get back to you.",
}
INTERNAL_ERROR = {
    False: "Maaf kijiye, abhi check nahi ho paya. Maine ops team ko bata diya hai, wo dekh lenge.",
    True: "Sorry, I couldn't complete the check right now. I've flagged it to the ops team.",
}


def status_text(status: dict, en: bool) -> str:
    lines = []
    for p in status.get("payouts", []):
        d = fmt_date(p["for_date"])
        if p["status"] == "paid":
            lines.append(f"₹{p['amount']} for {d} has been paid via PaySwift." if en
                         else f"{d} ka ₹{p['amount']} PaySwift pe bhej diya gaya hai.")
        elif p["status"] in ("pending", "retrying"):
            lines.append(f"₹{p['amount']} for {d} is being processed and should arrive shortly." if en
                         else f"{d} ka ₹{p['amount']} process ho raha hai, thodi der mein aa jayega.")
        else:
            lines.append(f"₹{p['amount']} for {d} hit a problem; the ops team is on it." if en
                         else f"{d} ke ₹{p['amount']} mein dikkat aayi hai, ops team dekh rahi hai.")
    for a in status.get("approvals", []):
        d = fmt_date(a["for_date"]) if a.get("for_date") else ""
        if a["status"] == "pending":
            lines.append(f"₹{a['amount']} for {d} is waiting for ops approval; it will be paid as soon as it's approved." if en
                         else f"{d} ka ₹{a['amount']} ops approval ke liye gaya hai, approve hote hi aa jayega.")
        elif a["status"] == "rejected":
            lines.append(f"₹{a['amount']} for {d} was not approved by the ops team." if en
                         else f"{d} ka ₹{a['amount']} ops team ne approve nahi kiya.")
    if any(e["status"] == "pending" for e in status.get("escalations", [])) and not lines:
        lines.append("Your case is with the ops team." if en else "Aapka case ops team ke paas hai, wo dekh rahe hain.")
    return " ".join(lines)
