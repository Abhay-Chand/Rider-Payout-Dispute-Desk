"""Deterministic planner: same tools, same guardrails as the LLM planner, but intent comes from the
rule-based parser. Used when no LLM is configured, when the LLM fails or times out, and always for
messages the security screen flags."""
from __future__ import annotations

from datetime import date

from app.agent import replies
from app.agent.nlu import Claim, Interpretation
from app.agent.tools import Toolbox


def run(text: str, interp: Interpretation, tools: Toolbox, state: dict) -> str:
    en = interp.language == "english"
    parts: list[str] = []

    if interp.security_flag:
        tools.escalate("suspicious", f"{interp.security_flag} detected in rider message",
                       {"other_rider_ids": interp.other_rider_ids, "text": text[:500]})
        state["awaiting"] = None
        return replies.SECURITY[en]

    claims = list(interp.claims)
    if not claims and state.get("awaiting") == "date":
        pending = state.get("pending_kinds") or []
        # e.g. "20 wala" right after we asked for a date: parsed as a weak date by the parser.
        claims = [c for c in interp.claims]
        if not claims:
            from app.agent.nlu import extract_dates
            for _, d in extract_dates(text, tools.ctx.today, weak_ok=True):
                claims.append(Claim(date=d, kinds=set(pending), explicit_date=True, text=text))
    if claims:
        state["awaiting"] = None
        parts.extend(_handle_claims(claims, tools, en, state))
        if interp.wants_human:
            tools.escalate("rider_requested_human", "Rider asked for a person", {"text": text[:300]})
            parts.append(replies.HUMAN[en])
        return " ".join(p for p in parts if p)

    if interp.wants_human:
        tools.escalate("rider_requested_human", "Rider asked for a person", {"text": text[:300]})
        return replies.HUMAN[en]

    last = state.get("last_dates") or []
    if interp.pushback and last:
        # Re-check against the records; if the rider still disagrees, a human looks at the records.
        rechecks = []
        for iso in last[-2:]:
            o = tools.resolve_dispute(date.fromisoformat(iso), state.get("last_issue", {}).get(iso, "general"),
                                      rider_claim=text)
            rechecks.append(o)
        tools.escalate("records_dispute", "Rider disagrees with the records after a re-check",
                       {"dates": last[-2:], "text": text[:300]})
        texts = []
        for o in rechecks:
            body = replies.outcome_text(o, en)
            texts.append(body)
        lead = "I checked again." if en else "Dobara check kiya."
        tail = (" Since you still disagree with our record, I've sent it to the ops team to verify." if en
                else " Aap record se agree nahi kar rahe, isliye maine ise ops team ko verify karne bhej diya hai.")
        return f"{lead} {' '.join(texts)}{tail}"

    if interp.status_query or interp.insist or (interp.ack and (state.get("last_dates"))):
        status = tools.case_status()
        txt = replies.status_text(status, en)
        if interp.insist and any(a["status"] == "pending" for a in status.get("approvals", [])):
            txt = ("I understand. " if en else "Samajh sakta hoon. ") + txt
        if txt:
            return txt
        if interp.ack:
            return "Thank you! Message me if anything else looks wrong." if en else "Dhanyavaad! Aur koi problem ho to bataiye."

    if interp.ack:
        return "Thank you! Message me if anything else looks wrong." if en else "Dhanyavaad! Aur koi problem ho to bataiye."

    if interp.complaint:
        kinds = set()
        from app.agent.nlu import KIND_PATTERNS
        for k in ("surge", "incentive", "penalty", "missing"):
            if KIND_PATTERNS[k].search(text):
                kinds.add(k)
        state["awaiting"] = "date"
        state["pending_kinds"] = sorted(kinds)
        return replies.ASK_DETAILS[en]

    if interp.greeting:
        return replies.GREETING[en]
    state["awaiting"] = "date"
    return replies.ASK_DETAILS[en]


def _handle_claims(claims: list[Claim], tools: Toolbox, en: bool, state: dict) -> list[str]:
    out: list[str] = []
    trip_facts: dict[str, dict] = {}
    by_date: dict[date, Claim] = {}
    order: list[date] = []

    for c in claims:
        own_trip_dates = []
        for tid in c.trip_ids:
            f = tools.lookup_trip(tid)
            trip_facts[tid] = f
            if f["found"]:
                own_trip_dates.append(date.fromisoformat(f["date"]))
            else:
                if f["reason"] == "not on this rider's account":
                    tools.escalate("trip_not_on_account", f"Rider {tools.ctx.rider_id} asked about {tid}",
                                   {"trip_id": tid, "text": c.text[:300]})
                out.append(f"I couldn't find order {tid} on your account. Please check the order ID." if en
                           else f"Order {tid} aapke account mein nahi mila. Order ID dobara check kar lijiye.")
        days = sorted(set(own_trip_dates)) or ([c.date] if c.date and not c.trip_ids else [])
        for d in days:
            if d in by_date:
                prev = by_date[d]
                prev.kinds |= c.kinds
                prev.trip_ids += [t for t in c.trip_ids if t not in prev.trip_ids]
                prev.duplicate |= c.duplicate
                prev.waiver |= c.waiver
                prev.claimed_amount = prev.claimed_amount or c.claimed_amount
                prev.claimed_trip_count = prev.claimed_trip_count or c.claimed_trip_count
            else:
                by_date[d] = Claim(date=d, trip_ids=[t for t in c.trip_ids if trip_facts.get(t, {}).get("date") == d.isoformat()]
                                   or list(c.trip_ids), kinds=set(c.kinds), duplicate=c.duplicate, waiver=c.waiver,
                                   claimed_trip_count=c.claimed_trip_count, claimed_amount=c.claimed_amount, text=c.text)
                order.append(d)

    last_issue = state.setdefault("last_issue", {})
    for d in order:
        c = by_date[d]
        issue = c.issue()
        own_trips = [t for t in c.trip_ids if trip_facts.get(t, {}).get("found")]
        o = tools.resolve_dispute(d, "penalty_duplicate" if issue == "penalty_waiver" else
                                  "general" if issue == "distance" else issue, own_trips, c.text)
        last_issue[d.isoformat()] = issue
        text = replies.outcome_text(o, en, trip_facts)
        if o["status"] in ("auto_paid", "approval_pending"):
            text += replies.claimed_amount_note(c.claimed_amount, o["amount"], en)
        if issue == "incentive" and o["status"] == "nothing_owed":
            text += replies.claimed_count_note(c.claimed_trip_count, (o.get("statement") or {}).get("completed_trips", 0), en)
        if o["status"] == "no_data":
            # The reply asks for the date again, so the next bare number ("sorry 18 tha") is that date.
            state["awaiting"] = "date"
            state["pending_kinds"] = sorted(c.kinds - {"general"})
        if o["status"] == "out_of_window":
            shortfall = sum(x.get("difference", 0) for x in (o.get("statement") or {}).get("discrepancies", [])
                            if x.get("difference", 0) > 0)
            if shortfall > 0:
                tools.escalate("out_of_window_shortfall", f"₹{shortfall} shortfall on {d.isoformat()}, outside the window",
                               {"date": d.isoformat(), "shortfall": shortfall})
                text += (" I've still flagged it to the ops team to take a look." if en
                         else " Phir bhi maine ops team ko dekhne ke liye bhej diya hai.")
        if issue == "distance":
            for t in own_trips:
                f = trip_facts[t]
                text = (f"Order {t} is recorded as {f['distance_km']:g} km, and ₹{f['paid']} matches that distance. "
                        f"I can't verify the distance myself, so I've sent it to the ops team to check." if en
                        else f"Order {t} ka recorded distance {f['distance_km']:g} km hai, uske hisaab se ₹{f['paid']} sahi mila. "
                        f"Distance main yahan verify nahi kar sakta, isliye ops team ko check karne bhej diya hai.") + (
                        "" if o["status"] == "nothing_owed" else " " + replies.outcome_text(o, en, trip_facts))
            tools.escalate("distance_dispute", f"Rider disputes recorded distance on {', '.join(own_trips) or d.isoformat()}",
                           {"trips": own_trips, "date": d.isoformat(), "text": c.text[:300]})
        if issue == "penalty_waiver":
            pen = [t for t in own_trips if trip_facts[t]["status"] == "cancelled_by_rider"]
            if pen:
                text = (f"Order {pen[0]} was cancelled from your side, and policy charges ₹10 for a rider cancellation. "
                        f"I've sent your request to the ops team; they can decide on an exception." if en
                        else f"Order {pen[0]} aapki taraf se cancel hua tha, policy mein rider-cancel pe ₹10 penalty lagti hai. "
                        f"Aapki request maine ops team ko bhej di hai, exception wahi decide karenge.") + (
                        "" if o["status"] == "nothing_owed" else " " + replies.outcome_text(o, en, trip_facts))
            tools.escalate("penalty_waiver_request", f"Penalty waiver requested for {', '.join(own_trips) or d.isoformat()}",
                           {"trips": own_trips, "date": d.isoformat(), "text": c.text[:300]})
        out.append(text)

    dates = state.setdefault("last_dates", [])
    for d in order:
        iso = d.isoformat()
        if iso in dates:
            dates.remove(iso)
        dates.append(iso)
    del dates[:-4]
    return out
