"""Deterministic message understanding for rider texts (Hinglish + English).

Used three ways:
  1. security screen (always on): impersonation / prompt-injection never reach the money tools;
  2. the offline planner when no LLM is configured;
  3. "parser hints" handed to the LLM so it doesn't have to guess dates or trip ids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

TRIP_RE = re.compile(r"\b[Tt]\s?-?(\d{5,7})\b")
RIDER_RE = re.compile(r"\b[Rr]\s?-?0*(\d{1,4})\b")

MONTHS = {"jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
          "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
          "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))
_NOT_DATE_AFTER = r"(?!\s*(?:order|orders|trip|trips|deliver|km|kms|kilo|rs\b|rupay|rupaye|rupee|rupees|₹|baar|bar\b|times|minute|min\b|ghante|hour|%|x\b))"

# Strong date forms. Weak form ("20 wala") is only a date when we just asked the rider for one.
DATE_PATTERNS = [
    re.compile(r"\b(20\d\d)-(\d{1,2})-(\d{1,2})\b"),                                         # 2026-09-20
    re.compile(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b(?!\s*(?:km|din|days|ghante|hours))"),                    # 20/09, 20-09-2026
    re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({_MONTH_ALT})\b", re.I),          # 19 sept, 19th Sep
    re.compile(rf"\b({_MONTH_ALT})\s*(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I),                    # Sep 19
    re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)\b", re.I),                                       # 19th
    re.compile(r"\b(\d{1,2})\s*(?:tarikh|tareekh|tarik|tarakh|date\b|taarikh)", re.I),  # 20 tarikh
    re.compile(r"\b(\d{1,2})\s+(?:ko|ke din|wale din)\b", re.I),        # 20 ko
    re.compile(r"\b(?:on|date|dated)\s+(\d{1,2})\b" + _NOT_DATE_AFTER, re.I),                   # on 19
]
WEAK_DATE = re.compile(r"\b(\d{1,2})\s*(?:wala|wale|wali|vala|vale|ka|ki|tha|thi|the|hai)?\s*[.!]*$|\b(\d{1,2})\s*(?:wala|wale|wali|vala|vale)\b", re.I)

RELATIVE = [(re.compile(r"\b(aaj|aj|today)\b", re.I), 0),
            (re.compile(r"\b(kal|kl|yesterday)\b", re.I), -1),
            (re.compile(r"\b(parso|parson|day before yesterday)\b", re.I), -2)]

KIND_PATTERNS = {
    "surge": re.compile(r"\b(surge|sarge|serge|boost|peak|multiplier|rush)\w*", re.I),
    "incentive": re.compile(r"\b(incentive|incentiv|insentive|insentiv|bonus|target)\w*", re.I),
    "penalty": re.compile(r"\b(penalty|penality|panelty|fine|kata|kaata|kaat|kat\s?gaya|deduct|cancel\w*)\b", re.I),
    "missing": re.compile(r"(paisa|paise|payment|pay|amount|money|kuch bhi)\s*(hi\s*)?(nahi|nhi|nai|not)\s*(aaya|aya|mila|aayi|ayi)|"
                          r"\bmissing\b|\bnot\s+(paid|received|credited)|\bdidn'?t\s+(get|receive)|\b(nahi|nhi)\s+aaya\b|\bunpaid\b", re.I),
    "distance": re.compile(r"\b(distance|doori|km|kms|kilometer|kilometre)s?\b.{0,25}\b(galat|wrong|kam|less|incorrect|sahi nahi)"
                           r"|\b(galat|wrong|incorrect)\b.{0,20}\b(distance|km)\b", re.I),
    "general": re.compile(r"\b(kam|galat|wrong|less|short|incorrect|gadbad|problem|issue|kum)\b", re.I),
}
DUPLICATE_RE = re.compile(r"\b(do|2|two|teen|3)\s*(baar|bar|times|time)\b|\btwice\b|\bdouble\b|\bdobara\s+kat", re.I)
WAIVER_RE = re.compile(r"\b(accident|emergency|hospital|bimar|sick|puncture|tyre|police|meri galti nahi|not my fault|"
                       r"customer ne (bola|kaha)|customer asked|kyun kaata|kyu kata|kyon kata|why.*(penalty|deduct))", re.I)
PUSHBACK_RE = re.compile(r"\b(dobara|phir se|fir se|recheck|re-check|check again|again check|galat check|"
                         r"nahi nahi|nahi yaar|aisa nahi|wrong hai|sahi se check|thik se check|properly check|that'?s wrong|not correct|"
                         r"you are wrong|aap galat)\b", re.I)
STATUS_RE = re.compile(r"\b(kab tak|kab aayega|kab ayega|kab milega|kab tk|status|when will|when do i|kitna time|"
                       r"aaya kya|aa gaya|mila kya|update)\b", re.I)
INSIST_RE = re.compile(r"\b(abhi de do|abhi do|de do|dedo|turant|jaldi|immediately|right now|now only|abhi chahiye)\b", re.I)
ACK_RE = re.compile(r"^\s*(ok+|okay|thik hai|theek hai|thik h|ok bhai|thanks|thank you|thnx|shukriya|dhanyavad|done|accha|achha|got it|cool|haan|ha|hmm+)\b", re.I)
GREETING_RE = re.compile(r"^\s*(hi+|hello|hey|namaste|namaskar|hii+|helo|good (morning|evening|afternoon))\b", re.I)
HUMAN_RE = re.compile(r"\b(manager|human|insaan|real person|kisi se baat|call karo|call me|team se baat|ops team|"
                      r"escalate|complaint (karna|karni|darj))\b", re.I)
COMPLAINT_RE = re.compile(r"\b(payout|payment|paisa|paise|pay|salary|earning|kamai|amount|money|order|trip)\b", re.I)

INJECTION_RE = re.compile(
    r"(ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier|your)\s+(rules|instructions|prompt)|"
    r"^\s*(system|assistant|developer)\s*:|\bsystem prompt\b|\byou are now\b|\bdeveloper mode\b|\bjailbreak\b|"
    r"\bact as (an? )?(admin|ops|system)|\boverride\b|<\|.*?\|>|\bsaare disputes approve\b|"
    r"\bapprove (all|everything|saare)\b|\bnew instructions\b|\bdisregard\b.{0,30}\b(rules|instructions))",
    re.I | re.M,
)
ENGLISH_HINT = re.compile(r"\b(the|was|but|only|please|got|my|for|did|not|is|i|it|on|of|and|you|are|me|to|have|what|why|when|didn'?t|get|pay|now)\b", re.I)
HINGLISH_HINT = re.compile(r"\b(hai|nahi|nhi|ka|ki|ke|ko|mila|aaya|kya|bhai|kyun|kab|mera|maine|tha|thi|kiye|karo|wala|kal|aur)\b", re.I)


# ---- normalisation: riders type Devanagari, Hindi number words and lists of days ----------------
_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
# Devanagari words -> the Hinglish spelling the patterns below already understand.
_DEV_WORDS = {
    "कल": "kal", "परसों": "parso", "परसो": "parso", "आज": "aaj", "तारीख": "tarikh", "तारीख़": "tarikh", "तरीख": "tarikh",
    "को": "ko", "का": "ka", "की": "ki", "के": "ke", "वाला": "wala", "सर्ज": "surge", "सर्च": "surge", "इंसेंटिव": "incentive",
    "इन्सेंटिव": "incentive", "बोनस": "bonus", "पेनल्टी": "penalty", "पेनाल्टी": "penalty", "जुर्माना": "fine", "कटा": "kata", "काटा": "kata",
    "ऑर्डर": "order", "आर्डर": "order", "ट्रिप": "trip", "पैसा": "paisa", "पैसे": "paise", "पेमेंट": "payment", "पेमेन्ट": "payment",
    "नहीं": "nahi", "नही": "nahi", "मिला": "mila", "मिले": "mile", "आया": "aaya", "आए": "aaye", "कम": "kam", "गलत": "galat",
    "बार": "baar", "दो": "do", "दोबारा": "dobara", "कब": "kab", "तक": "tak", "किए": "kiye", "किये": "kiye", "थे": "the", "था": "tha",
    "और": "aur", "भाई": "bhai", "सितंबर": "sep", "सितम्बर": "sep",
}
# Python's \b doesn't work inside Devanagari (vowel signs aren't \w), so bound words by "not a Devanagari character".
_DEV_WORD_RE = re.compile(r"(?<![\u0900-\u097F])(?:" + "|".join(sorted(map(re.escape, _DEV_WORDS), key=len, reverse=True))
                          + r")(?![\u0900-\u097F])")
# Hindi number words used for dates, with common Hinglish spellings.
_NUM_WORDS = {
    1: "ek", 2: "do", 3: "teen", 4: "char|chaar", 5: "paanch|panch", 6: "chhe|chhah|chah", 7: "saat|sat", 8: "aath|ath",
    9: "nau|no", 10: "das", 11: "gyarah|gyara|igyarah", 12: "barah|bara|baarah", 13: "terah|tera", 14: "chaudah|chauda|choda",
    15: "pandrah|pandra|pandarah", 16: "solah|sola", 17: "satrah|satra|sattrah", 18: "atharah|athara|atthara|attharah|atharha",
    19: "unnis|unees|unnees", 20: "bees|bis", 21: "ikkis|ikis|ikkees", 22: "bais|baees", 23: "teis|tees", 24: "chaubis|chobis",
    25: "pachchis|pachis", 26: "chhabbis|chabbis", 27: "sattais|satais", 28: "atthais|athais", 29: "untees|unatis", 30: "tees|tis",
    31: "iktees|ikattis",
}
_NUM_WORD_RE = re.compile(
    r"\b(" + "|".join(f"(?P<n{n}>{alts})" for n, alts in _NUM_WORDS.items()) + r")\s+(?=(?:tarikh|tareekh|tarik|ko\b|sep|sept))", re.I)
# "20 aur 21 dono din", "19, 20 & 21 ko" -> one date marker per day.
_DAY_LIST_RE = re.compile(r"\b(\d{1,2}(?:\s*(?:,|aur|and|&|or|ya)\s*\d{1,2})+)\s+(?:dono\s+|teeno\s+|teenon\s+|both\s+)?"
                          r"(din|days|tarikh|tareekh|ko|wale din)\b", re.I)


def normalize(text: str) -> str:
    t = text.translate(_DEV_DIGITS)
    t = _DEV_WORD_RE.sub(lambda m: _DEV_WORDS[m.group(0)], t)

    def num(m: re.Match) -> str:
        n = next(int(k[1:]) for k, v in m.groupdict().items() if v)
        return f"{n} "
    t = _NUM_WORD_RE.sub(num, t)
    t = _DAY_LIST_RE.sub(lambda m: " aur ".join(f"{d} ko" for d in re.findall(r"\d{1,2}", m.group(1))), t)
    return t


@dataclass
class Claim:
    date: date | None = None
    trip_ids: list[str] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    duplicate: bool = False
    waiver: bool = False
    claimed_trip_count: int | None = None
    claimed_amount: int | None = None
    explicit_date: bool = False
    text: str = ""

    def issue(self) -> str:
        if "distance" in self.kinds:
            return "distance"
        if "penalty" in self.kinds and self.waiver:
            return "penalty_waiver"
        if "penalty" in self.kinds:
            return "penalty_duplicate" if self.duplicate or not self.waiver else "penalty"
        for k in ("surge", "incentive", "missing"):
            if k in self.kinds:
                return {"missing": "missing_payment"}.get(k, k)
        return "general"


@dataclass
class Interpretation:
    language: str
    claims: list[Claim]
    other_rider_ids: list[str]
    injection: bool
    pushback: bool
    status_query: bool
    insist: bool
    ack: bool
    greeting: bool
    wants_human: bool
    complaint: bool
    numbers: list[int]

    @property
    def security_flag(self) -> str | None:
        if self.other_rider_ids:
            return "impersonation"
        if self.injection:
            return "prompt_injection"
        return None


def detect_language(text: str) -> str:
    hi = len(HINGLISH_HINT.findall(text))
    en = len(ENGLISH_HINT.findall(text))
    return "english" if en >= 2 and hi == 0 else "hinglish"


def _resolve_day(day: int, month: int | None, today: date) -> date | None:
    if not 1 <= day <= 31:
        return None
    year, m = today.year, month or today.month
    try:
        d = date(year, m, day)
    except ValueError:
        return None
    if d > today:  # "20" said on the 5th means last month's 20th
        if month is None:
            m_prev = 12 if today.month == 1 else today.month - 1
            y_prev = year - 1 if today.month == 1 else year
            try:
                d = date(y_prev, m_prev, day)
            except ValueError:
                return None
        else:
            d = date(year - 1, m, day)
    return d


def extract_dates(text: str, today: date, weak_ok: bool = False) -> list[tuple[int, date]]:
    """Returns (position, date) pairs found in the text."""
    found: list[tuple[int, date]] = []
    taken: list[tuple[int, int]] = []

    def add(pos: tuple[int, int], d: date | None) -> None:
        if d and not any(a <= pos[0] < b or a < pos[1] <= b for a, b in taken):
            found.append((pos[0], d))
            taken.append(pos)

    for m in DATE_PATTERNS[0].finditer(text):
        try:
            add(m.span(), date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in DATE_PATTERNS[1].finditer(text):
        if "." in m.group(0) and not m.group(3):
            continue  # "7.4" is a distance, not 7 April
        mon = int(m.group(2))
        if 1 <= mon <= 12:
            add(m.span(), _resolve_day(int(m.group(1)), mon, today))
    for m in DATE_PATTERNS[2].finditer(text):
        add(m.span(), _resolve_day(int(m.group(1)), MONTHS[m.group(2).lower()], today))
    for m in DATE_PATTERNS[3].finditer(text):
        add(m.span(), _resolve_day(int(m.group(2)), MONTHS[m.group(1).lower()], today))
    for pat in DATE_PATTERNS[4:]:
        for m in pat.finditer(text):
            add(m.span(1), _resolve_day(int(m.group(1)), None, today))
    for pat, delta in RELATIVE:
        for m in pat.finditer(text):
            add(m.span(), today + timedelta(days=delta))
    if weak_ok and not found:
        for m in WEAK_DATE.finditer(text):
            g = m.group(1) or m.group(2)
            add(m.span(), _resolve_day(int(g), None, today))
    return sorted(found)


_SPLIT_RE = re.compile(r"\s+(?:aur|and|also|plus|tatha|&)\s+|[;!?]+\s*|\.\s+|\n+", re.I)
COUNT_RE = re.compile(r"\b(\d{1,2})\s*(?:se\s+zyada\s+|se\s+jyada\s+|\+\s*)?(?:order|orders|trip|trips|delivery|deliveries)\b", re.I)
# Only an amount the rider says is *short* counts as a claimed shortfall ("300 rupay kam aaye", "short by Rs 40").
AMOUNT_RE = re.compile(
    r"(?:(?:₹|rs\.?|inr|rupees?)\s*(\d{1,6})|\b(\d{1,6})\s*(?:₹|rs\b|rupay|rupaye|rupees?|rupiya|rupaiye|/-)?)"
    r"\s*(?:ka\s+|ke\s+)?(?:kam|kum|less|short|missing|kata|kaat)\b"
    r"|\bshort\s+by\s+(?:₹|rs\.?\s*)?(\d{1,6})", re.I)


def parse(text: str, rider_id: str, today: date, awaiting_date: bool = False) -> Interpretation:
    original = text.strip()
    clean = normalize(original)
    others = sorted({f"R{int(n):03d}" for n in RIDER_RE.findall(clean)} - {rider_id})
    claims: list[Claim] = []
    last_date: date | None = None
    for seg in [s for s in _SPLIT_RE.split(clean) if s and s.strip()]:
        dates = extract_dates(seg, today, weak_ok=awaiting_date)
        trips = [f"T{n}" for n in TRIP_RE.findall(seg)]
        kinds = {k for k, p in KIND_PATTERNS.items() if p.search(seg)}
        if "missing" in kinds and "surge" not in kinds and "incentive" not in kinds:
            kinds.discard("general")
        cnt = COUNT_RE.search(seg)
        amt = AMOUNT_RE.search(seg)
        c = Claim(
            date=dates[0][1] if dates else None, explicit_date=bool(dates), trip_ids=trips,
            kinds={k for k in kinds}, duplicate=bool(DUPLICATE_RE.search(seg)), waiver=bool(WAIVER_RE.search(seg)),
            claimed_trip_count=int(cnt.group(1)) if cnt else None,
            claimed_amount=int(amt.group(1) or amt.group(2) or amt.group(3)) if amt else None, text=seg.strip(),
        )
        if len(dates) > 1:  # "19 aur 20 ko incentive" -> one claim per date
            for _, d in dates[1:]:
                claims.append(Claim(date=d, explicit_date=True, kinds=set(c.kinds), duplicate=c.duplicate, text=c.text))
        if c.date:
            last_date = c.date
        if c.date or c.trip_ids or (c.kinds - {"general"}) or c.claimed_amount or c.claimed_trip_count:
            claims.append(c)

    # Segments that carry an issue but no date inherit the nearest preceding date ("kal ka payout kam aaya. 12 se zyada order").
    merged: list[Claim] = []
    for c in claims:
        if not c.date and not c.trip_ids and merged:
            prev = merged[-1]
            prev.kinds |= c.kinds
            prev.duplicate |= c.duplicate
            prev.waiver |= c.waiver
            prev.claimed_trip_count = prev.claimed_trip_count or c.claimed_trip_count
            prev.claimed_amount = prev.claimed_amount or c.claimed_amount
            continue
        if not c.date and not c.trip_ids and last_date:
            c.date = last_date
        merged.append(c)
    # A claim with neither a date nor a trip is not actionable on its own.
    actionable = [c for c in merged if c.date or c.trip_ids]

    numbers = [int(n) for n in re.findall(r"\d+", clean)]
    return Interpretation(
        language=detect_language(original), claims=actionable, other_rider_ids=others,
        injection=bool(INJECTION_RE.search(clean)), pushback=bool(PUSHBACK_RE.search(clean)),
        status_query=bool(STATUS_RE.search(clean)), insist=bool(INSIST_RE.search(clean)),
        ack=bool(ACK_RE.search(clean)), greeting=bool(GREETING_RE.search(clean)), wants_human=bool(HUMAN_RE.search(clean)),
        complaint=bool(COMPLAINT_RE.search(clean) or KIND_PATTERNS["general"].search(clean) or any(
            KIND_PATTERNS[k].search(clean) for k in ("surge", "incentive", "penalty", "missing"))),
        numbers=numbers,
    )
