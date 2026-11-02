"""Turn what the caller said (Prisma transcripts) into evidence: phone, account, case, IFSC, UPI handle.

Two rules run through the whole file:

  1. NEVER OVERWRITE WHAT WAS HEARD. A Finding's `value` is what the transcript said (digit words turned into
     digits, nothing else). A guess goes in the separate `suggested` field and is never used as the value.
  2. EVERY FINDING CARRIES ITS PROVENANCE: the exact `quote` it came from, the arrival time `t` of that
     transcript, and `source_index`, its position in the input list.

All numbers are strings, never ints, so leading zeros survive ("0047138296").

NOTE FOR safety.py LATER: the output gate should not rely on digit patterns alone. Uncle must be able to read
back the CALLER's digits, so the rule should be based on where digits came from: allowed if they appear in
Uncle's fictional fact sheet or in the caller's own transcript (a Finding's quote), blocked otherwise.
`looks_like_real_mobile` below is a pattern check only, kept simple on purpose, and checks whole digit runs
only (not 10-digit windows inside longer numbers).

Facts the validators rely on (checked 1 Nov 2026):
  - Indian mobile numbers are 10 digits starting with 6, 7, 8 or 9: Wikipedia, "Mobile telephone numbering in
    India" ("start with either 9, 8, 7 or 6"). Secondary source; I did not read TRAI's own plan.
  - IFSC = 4 letters, "0", then 6 letters or digits (11 characters): Wikipedia and several bank pages agree.
    Secondary sources; a few pages disagree on details, the majority agree on 4+1+6.
  - Account numbers of 9 to 18 digits: UNVERIFIED. Finance blogs repeat it, but I found no RBI rule. It is used
    only as a loose shape check.
  - UPI provider handles: UNVERIFIED, see mirage/config.py.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from mirage.config import KNOWN_UPI_PROVIDERS, SYNTHETIC_UPI_PROVIDERS

KINDS_EXPECTED = ("phone", "account", "case")


# --------------------------------------------------------------------------- public data shapes

@dataclass(frozen=True)
class Transcript:
    """One transcript from Prisma. `t` is when it arrived (seconds); `audio_s` is how much speech it covered."""
    text: str
    t: float
    audio_s: float = 0.0


@dataclass(frozen=True)
class Finding:
    kind: str                          # phone | account | case | ambiguous | ifsc | letters | vpa
    value: str                         # AS HEARD (digit words -> digits). Never replaced by a guess.
    valid: bool                        # does the value fit the format for its kind?
    reason: str                        # why it is not valid, or a caution; "" when there is nothing to say
    confidence: str                    # keyword | weak_keyword | expected | length | none | heard
    needs_confirmation: bool           # the FSM should have the caller repeat or spell it
    quote: str                         # exact transcript text it came from ("a | b" when joined across two)
    t: float                           # arrival time of the first transcript it came from
    source_index: int                  # index of that transcript in the input list
    suggested: str | None = None       # a GUESS, kept apart from `value`
    candidates: tuple[str, ...] = ()   # for kind "ambiguous": what it could be


# --------------------------------------------------------------------------- validators (strings in, bool out)

def looks_like_real_mobile(digits: str) -> bool:
    """True for a number shaped like a real Indian mobile: 10 digits starting 6-9, with an optional 91 or 0 in
    front. This is the strict check the safety gate uses. It has NO mode parameter on purpose: synthetic
    mode must never weaken the gate."""
    if not (digits.isascii() and digits.isdigit()):
        return False
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return len(digits) == 10 and digits[0] in "6789"


def is_synthetic_phone(digits: str) -> bool:
    """Our staged-demo convention: 10 digits starting 00 (never a real mobile). Only used in synthetic mode."""
    return digits.isascii() and digits.isdigit() and len(digits) == 10 and digits.startswith("00")


def is_valid_ifsc(code: str) -> bool:
    return re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", code) is not None


def is_plausible_account(digits: str) -> bool:
    """Shape only: 9 to 18 digits (the range is UNVERIFIED, see the module docstring)."""
    return digits.isascii() and digits.isdigit() and 9 <= len(digits) <= 18


def is_valid_vpa(vpa: str) -> bool:
    """A UPI id is ASCII: name, "@", handle. A name heard in Devanagari cannot pass."""
    return re.fullmatch(r"[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9-]{1,63}", vpa) is not None


# --------------------------------------------------------------------------- word tables

def _norm(raw: str) -> str:
    """Compare words in one form: Unicode NFC, no zero-width characters, lower case."""
    return unicodedata.normalize("NFC", raw).replace("‌", "").replace("‍", "").lower()


norm_word = _norm   # public name, so other modules compare words the same way


def _table(**groups: tuple[str, ...]) -> dict[str, str]:
    return {_norm(w): key for key, words in groups.items() for w in words}


# "oh" and "ओ" are left out on purpose (too ambiguous), and so is "एट" ("at" in a spoken UPI id).
DIGIT_WORDS: dict[str, str] = _table(**{
    "0": ("ज़ीरो", "जीरो", "शून्य", "सिफ़र", "सिफर", "zero", "jeero", "shunya", "sifar", "ziro"),
    "1": ("एक", "ek", "one", "वन"),
    "2": ("दो", "do", "two", "टू"),
    "3": ("तीन", "teen", "tin", "three", "थ्री"),
    "4": ("चार", "char", "chaar", "four", "फोर"),
    "5": ("पाँच", "पांच", "पाच", "paanch", "panch", "paach", "five", "फाइव"),
    "6": ("छह", "छः", "छे", "छै", "chhe", "chhah", "six", "सिक्स"),
    "7": ("सात", "saat", "seven", "सेवन"),
    "8": ("आठ", "aath", "eight"),
    "9": ("नौ", "nau", "nao", "nine", "नाइन"),
})
MULTIPLIERS: dict[str, str] = _table(**{"2": ("double", "डबल", "dubbal"), "3": ("triple", "ट्रिपल", "tripal")})

# word -> (kind, specific). "Specific" words say what the number is; bare "number" does not.
KEYWORDS: dict[str, tuple[str, bool]] = {
    **{_norm(w): ("account", True) for w in ("account", "अकाउंट", "अकाउन्ट", "खाता", "खाते")},
    **{_norm(w): ("case", True) for w in ("case", "केस")},
    **{_norm(w): ("phone", True) for w in ("mobile", "मोबाइल", "phone", "फोन", "whatsapp", "व्हाट्सएप")},
    **{_norm(w): ("phone", False) for w in ("number", "नंबर", "नम्बर")},
}
IFSC_WORDS = {_norm(w) for w in ("ifsc", "आईएफएससी")}
UPI_WORDS = {_norm(w) for w in ("upi", "यूपीआई", "vpa", "वीपीए")}
FILLER_WORDS = {_norm(w) for w in (
    "है", "hai", "hain", "हैं", "का", "ka", "की", "ke", "id", "आईडी", "code", "कोड", "ye", "yeh", "ये", "यह",
    "मेरा", "मेरी", "mera", "meri", "my", "is", "the", "and", "और",
)}
DOT_WORDS = {_norm(w) for w in ("dot", "डॉट", "डोट", "बिंदु")}
UNDERSCORE_WORDS = {_norm(w) for w in ("underscore", "अंडरस्कोर")}
DASH_WORDS = {_norm(w) for w in ("dash", "hyphen", "डैश", "हाइफ़न")}
AT_WORDS = {_norm(w) for w in ("at", "एट")}
AT_TAIL = ({_norm("the"), _norm("द")}, {_norm("rate"), _norm("रेट")})   # "at the rate" / "एट द रेट"
LETTER_NAMES: dict[str, str] = _table(**{
    "A": ("ए",), "B": ("बी",), "C": ("सी",), "D": ("डी",), "E": ("ई",), "F": ("एफ",), "G": ("जी",),
    "H": ("एच",), "I": ("आई",), "J": ("जे",), "K": ("के",), "L": ("एल",), "M": ("एम",), "N": ("एन",),
    "O": ("ओ",), "P": ("पी",), "Q": ("क्यू",), "R": ("आर",), "S": ("एस",), "T": ("टी",), "U": ("यू",),
    "V": ("वी",), "W": ("डब्ल्यू",), "X": ("एक्स",), "Y": ("वाई",), "Z": ("ज़ेड", "जेड"),
})

# Sentence-ending marks. A comma is NOT one: people say "एक, दो, तीन" and the digits belong together.
BREAK_CHARS = set("।॥.?!;…\n")
# A word is Devanagari or ordinary word characters; the danda (U+0964/0965) is left out so it stays punctuation.
_WORD = r"[\wऀ-ॣ०-ॿ‌‍]+"
_TOKEN_RE = re.compile(rf"{_WORD}(?:[._@]{_WORD})*|\S")


# --------------------------------------------------------------------------- tokens

@dataclass
class _Tok:
    kind: str      # digit | numeral | mult | word | sep | break | at | vpa
    raw: str       # exactly as in the transcript
    norm: str
    src: int       # index of the transcript
    start: int     # character offsets in that transcript
    end: int
    val: str = ""  # digit characters (digit, numeral) or the repeat count (mult)


def _tokenize(text: str, src: int) -> list[_Tok]:
    toks: list[_Tok] = []
    for m in _TOKEN_RE.finditer(text):
        raw, norm = m.group(), _norm(m.group())
        if len(raw) == 1 and not (raw.isalnum() or raw == "_"):
            kind = "break" if raw in BREAK_CHARS else "at" if raw == "@" else "sep"
            toks.append(_Tok(kind, raw, norm, src, m.start(), m.end()))
        elif "@" in raw:
            toks.append(_Tok("vpa", raw, norm, src, m.start(), m.end()))
        elif all(c.isdigit() for c in raw):
            toks.append(_Tok("numeral", raw, norm, src, m.start(), m.end(), "".join(str(unicodedata.digit(c)) for c in raw)))
        elif norm in DIGIT_WORDS:
            toks.append(_Tok("digit", raw, norm, src, m.start(), m.end(), DIGIT_WORDS[norm]))
        elif norm in MULTIPLIERS:
            toks.append(_Tok("mult", raw, norm, src, m.start(), m.end(), MULTIPLIERS[norm]))
        else:
            toks.append(_Tok("word", raw, norm, src, m.start(), m.end()))
    while toks and toks[-1].kind == "break":   # Prisma ends every segment with "।": that is not a real break
        toks.pop()
    return toks


def _stream(transcripts: Sequence[Transcript], window_s: float) -> list[_Tok]:
    """All transcripts as one token list. A "break" token is inserted wherever two transcripts are too far apart
    in time to be one dictation. Gap = (when the next one's speech began) - (when the previous one arrived)."""
    stream: list[_Tok] = []
    for i, tr in enumerate(transcripts):
        if i > 0:
            prev = transcripts[i - 1]
            if (tr.t - tr.audio_s) - prev.t > window_s:
                stream.append(_Tok("break", "", "", i, 0, 0))
        stream.extend(_tokenize(tr.text, i))
    return stream


def _digit_at(toks: list[_Tok], k: int, claimed: set[int]) -> tuple[str, int] | None:
    """If a digit (or "double X") starts at k, return (digits, tokens used)."""
    if k >= len(toks) or k in claimed:
        return None
    t = toks[k]
    if t.kind in ("digit", "numeral"):
        return t.val, 1
    if t.kind == "mult" and k + 1 < len(toks) and k + 1 not in claimed and toks[k + 1].kind == "digit":
        return toks[k + 1].val * int(t.val), 2
    return None


@dataclass
class _Run:
    digits: str
    first: int    # token index of the first digit token
    last: int
    near_other_run: bool = False   # one unknown word separates it from another digit run


def _build_runs(toks: list[_Tok], claimed: set[int]) -> list[_Run]:
    runs: list[_Run] = []
    i = 0
    while i < len(toks):
        hit = _digit_at(toks, i, claimed)
        if not hit:
            i += 1
            continue
        digits, used = hit
        first, last, j = i, i + used - 1, i + used
        while True:
            k = j
            while k < len(toks) and toks[k].kind == "sep" and k not in claimed:   # "एक, दो" or "चार-सात"
                k += 1
            nxt = _digit_at(toks, k, claimed)
            if not nxt:
                break
            digits += nxt[0]
            last, j = k + nxt[1] - 1, k + nxt[1]
        runs.append(_Run(digits, first, last))
        i = last + 1
    for a, b in zip(runs, runs[1:]):   # one unknown word between two runs: a digit may have been missed there
        between = [t for t in toks[a.last + 1:b.first] if t.kind != "sep"]
        if len(between) == 1 and between[0].kind == "word" and between[0].norm not in KEYWORDS:
            a.near_other_run = b.near_other_run = True
    return runs


# --------------------------------------------------------------------------- provenance helpers

def _provenance(toks: list[_Tok], idxs: Sequence[int], transcripts: Sequence[Transcript]) -> tuple[str, float, int]:
    """Exact quote (one slice per transcript, joined by " | "), arrival time, and source index."""
    spans: dict[int, list[int]] = {}
    for k in idxs:
        s = spans.setdefault(toks[k].src, [toks[k].start, toks[k].end])
        s[0], s[1] = min(s[0], toks[k].start), max(s[1], toks[k].end)
    order = sorted(spans)
    quote = " | ".join(transcripts[s].text[spans[s][0]:spans[s][1]] for s in order)
    return quote, transcripts[order[0]].t, order[0]


def _make(toks, idxs, transcripts, **fields) -> Finding:
    quote, t, source_index = _provenance(toks, idxs, transcripts)
    return Finding(quote=quote, t=t, source_index=source_index, **fields)


# --------------------------------------------------------------------------- digit runs: which number is it?

def _keyword_before(toks: list[_Tok], first: int, claimed: set[int], window: int = 5) -> tuple[str, bool] | None:
    """Look back up to `window` words for a word that says what the number is. A specific word (account, case,
    phone) beats the bare word "number", and a nearer word beats a farther one. Stops at a sentence break,
    at another number, or at a claimed token."""
    hits: list[tuple[int, str, bool]] = []
    seen, k = 0, first - 1
    while k >= 0 and seen < window:
        t = toks[k]
        if t.kind in ("break", "digit", "numeral", "mult", "vpa", "at") or k in claimed:
            break
        if t.kind != "sep":
            seen += 1
            if t.norm in KEYWORDS:
                kind, specific = KEYWORDS[t.norm]
                hits.append((seen, kind, specific))
        k -= 1
    specific_hits = [h for h in hits if h[2]]
    best = min(specific_hits or hits, default=None)
    return (best[1], best[2]) if best else None


AMOUNT_UNITS = {_norm(w) for w in (
    "rupaye", "rupaya", "rupiye", "rupees", "rupee", "rs", "inr", "lakh", "lakhs", "lac", "hazaar", "hazar", "hajar",
    "thousand", "crore", "karod", "रुपये", "रुपए", "रुपया", "हज़ार", "हजार", "लाख", "करोड़", "करोड")}
AMOUNT_PREFIXES = {_norm(w) for w in ("rs", "inr", "₹")}


def _amount_unit(toks: list[_Tok], run: "_Run") -> str | None:
    """If the number is money (a unit right after it, or Rs / INR / the rupee sign right before), return that word."""
    k = run.last + 1                                  # the unit must come right after: "..., Rs 500" is a new phrase
    if k < len(toks) and toks[k].norm in AMOUNT_UNITS:
        return toks[k].raw
    k = run.first - 1
    if k >= 1 and toks[k].kind == "break" and toks[k].raw == "." and toks[k - 1].norm in AMOUNT_PREFIXES:   # "Rs. 500"
        return toks[k - 1].raw
    if k >= 0 and toks[k].norm in AMOUNT_PREFIXES:
        return toks[k].raw
    return None


def _fits(expected: str, n: int) -> bool:
    return {"phone": n in (10, 11, 12), "account": 9 <= n <= 18, "case": n >= 3}.get(expected, False)


def _classify(run: _Run, toks, claimed, expected) -> tuple[str, str, tuple[str, ...]]:
    """-> (kind, confidence, candidates). Keywords first; the FSM's `expected` hint only breaks a tie when there
    is no specific keyword (it beats the bare word "number"); length is the last resort."""
    n = len(run.digits)
    hit = _keyword_before(toks, run.first, claimed)
    if hit and hit[1]:
        return hit[0], "keyword", ()
    if expected and _fits(expected, n):
        return expected, "expected", ()
    if hit:
        return hit[0], "weak_keyword", ()
    if n == 10:
        return "ambiguous", "none", ("phone", "account")
    if n == 9 or 11 <= n <= 18:
        return "account", "length", ()
    return "ambiguous", "none", ()


def _check(kind: str, digits: str, synthetic_mode: bool) -> tuple[bool, str]:
    if kind == "phone":
        if looks_like_real_mobile(digits):
            return True, ""
        if is_synthetic_phone(digits):
            return (True, "synthetic number (00 prefix)") if synthetic_mode else (
                False, "starts with 00: accepted only in synthetic mode")
        return False, f"{len(digits)} digits; a mobile is 10 digits starting 6-9"
    if kind == "account":
        ok = is_plausible_account(digits)
        return ok, "" if ok else f"{len(digits)} digits; accounts are usually 9 to 18 (unverified range)"
    return True, ""


# --------------------------------------------------------------------------- letters, IFSC, UPI ids

def _letter(t: _Tok) -> str | None:
    if t.kind != "word":
        return None
    if len(t.raw) == 1 and t.raw.isascii() and t.raw.isalpha():
        return t.raw.upper()
    return LETTER_NAMES.get(t.norm)


def _letter_runs(toks: list[_Tok], claimed: set[int], min_run: int = 3) -> list[tuple[str, list[int]]]:
    """Runs of spelled letters ("N A I R G", "एम आई आर जी"), at least `min_run` in a row."""
    runs, cur, i = [], [], 0
    while i <= len(toks):
        ch = _letter(toks[i]) if i < len(toks) and i not in claimed else None
        if ch:
            cur.append(i)
        elif not (i < len(toks) and toks[i].kind == "sep" and cur):
            if len(cur) >= min_run:
                runs.append(("".join(_letter(toks[k]) for k in cur), cur))
            cur = []
        i += 1
    return runs


def _parse_ifsc_tail(toks: list[_Tok], k: int, claimed: set[int]) -> tuple[str, list[int]]:
    """After the word IFSC: skip a filler word or two, then take letters (single letters, ALL-CAPS chunks,
    Devanagari letter names) and digits until something else appears."""
    idxs: list[int] = []
    chars = ""
    skipped = 0
    while k < len(toks) and k not in claimed:
        t = toks[k]
        hit = _digit_at(toks, k, claimed)
        if hit:
            chars += hit[0]
            idxs.extend(range(k, k + hit[1]))
            k += hit[1]
            continue
        if t.kind == "sep":
            k += 1
            continue
        if t.kind != "word":
            break
        if t.raw.isascii() and t.raw.isalpha() and (t.raw.isupper() or len(t.raw) == 1):
            chars += t.raw.upper()
        elif _letter(t):
            chars += _letter(t)
        elif not chars and t.norm in FILLER_WORDS and skipped < 2:
            skipped += 1
            k += 1
            continue
        else:
            break
        idxs.append(k)
        k += 1
    return chars, idxs


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _suggest_provider(heard: list[str], providers: frozenset[str]) -> str | None:
    """A GUESS at which known handle was meant, e.g. "miraj"+"UPI" -> "mirageupi". Only if it is clearly the
    closest one. Never used as the value."""
    candidates = {"".join(heard[:n]).lower() for n in range(1, len(heard) + 1)}
    best: list[tuple[int, str]] = []
    for target in providers:
        for cand in candidates:
            if not (cand.isascii() and cand.isalpha()):
                continue
            d = _edit_distance(cand, target)
            if d <= (2 if len(target) >= 7 else 1):
                best.append((d, target))
    best.sort()
    if best and (len(best) == 1 or best[0][0] < best[1][0] or best[0][1] == best[1][1]):
        return best[0][1]
    return None


def _vpa_finding(toks, idxs, local: str, provider_raw: list[str], transcripts, synthetic_mode) -> Finding:
    providers = KNOWN_UPI_PROVIDERS | (SYNTHETIC_UPI_PROVIDERS if synthetic_mode else frozenset())
    value = f"{local}@{' '.join(provider_raw)}"
    known = provider_raw[0].lower() in providers and len(provider_raw) == 1
    suggestion = None if known else _suggest_provider(provider_raw, providers)
    valid = is_valid_vpa(value)
    reason = "" if valid else "not a valid UPI id as heard (a UPI id is plain ASCII: name@handle)"
    if valid and not known:
        reason = "handle not in the known list (the list is small and unverified)"
    return _make(
        toks, idxs, transcripts, kind="vpa", value=value, valid=valid, reason=reason, confidence="heard",
        needs_confirmation=not (valid and known),
        suggested=f"{local}@{suggestion}" if suggestion else None,
    )


def _find_written_vpas(toks, claimed, transcripts, synthetic_mode) -> list[tuple[int, Finding]]:
    """A literal "name@handle" in the transcript, e.g. "राजेश.test@miraj UPI"."""
    out = []
    for k, t in enumerate(toks):
        if t.kind != "vpa" or k in claimed:
            continue
        local, _, provider = t.raw.partition("@")
        if not local or not provider:
            continue
        idxs, provider_raw = [k], [provider]
        nxt = toks[k + 1] if k + 1 < len(toks) else None
        if provider.lower() not in KNOWN_UPI_PROVIDERS | SYNTHETIC_UPI_PROVIDERS and nxt and nxt.norm in UPI_WORDS \
                and (k + 1) not in claimed:
            idxs.append(k + 1)           # "miraj UPI": the heard handle may be two words
            provider_raw.append(nxt.raw)
        claimed.update(idxs)
        out.append((min(idxs), _vpa_finding(toks, idxs, local, provider_raw, transcripts, synthetic_mode)))
    return out


def _find_spoken_vpas(toks, claimed, transcripts, synthetic_mode) -> list[tuple[int, Finding]]:
    """"राजेश dot test at miraj UPI": only when a UPI keyword appears just before, so an ordinary "at" is ignored."""
    out = []
    for k, t in enumerate(toks):
        if k in claimed or not (t.kind == "at" or (t.kind == "word" and t.norm in AT_WORDS)):
            continue
        end = k + 1   # skip "the rate" / "द रेट" after "at"
        if end + 1 < len(toks) and toks[end].norm in AT_TAIL[0] and toks[end + 1].norm in AT_TAIL[1]:
            end += 2
        local_parts: list[str] = []
        local_idxs: list[int] = []
        j = k - 1
        while j >= 0 and len(local_idxs) < 8 and j not in claimed:
            u = toks[j]
            hit = _digit_at(toks, j, claimed) if u.kind in ("digit", "numeral") else None
            if hit:
                local_parts.append(hit[0])
            elif u.kind == "word" and u.norm in DOT_WORDS:
                local_parts.append(".")
            elif u.kind == "word" and u.norm in UNDERSCORE_WORDS:
                local_parts.append("_")
            elif u.kind == "word" and u.norm in DASH_WORDS:
                local_parts.append("-")
            elif u.kind == "word" and u.norm not in FILLER_WORDS and u.norm not in UPI_WORDS and u.norm not in KEYWORDS:
                local_parts.append(u.raw)
            else:
                break
            local_idxs.append(j)
            j -= 1
        if not local_idxs:
            continue
        gate, seen = False, 0   # a UPI keyword within 6 words before the name
        for g in range(j, -1, -1):
            if toks[g].kind == "break" or seen >= 6:
                break
            if toks[g].kind != "sep":
                seen += 1
                gate = gate or toks[g].norm in UPI_WORDS
        if not gate or end >= len(toks) or toks[end].kind != "word" or toks[end].norm in FILLER_WORDS or end in claimed:
            continue
        idxs = sorted(local_idxs) + list(range(k, end)) + [end]
        provider_raw = [toks[end].raw]
        known = toks[end].norm in KNOWN_UPI_PROVIDERS | SYNTHETIC_UPI_PROVIDERS
        if not known and end + 1 < len(toks) and toks[end + 1].norm in UPI_WORDS and end + 1 not in claimed:
            idxs.append(end + 1)
            provider_raw.append(toks[end + 1].raw)
        claimed.update(idxs)
        local = "".join(reversed(local_parts))
        out.append((min(idxs), _vpa_finding(toks, idxs, local, provider_raw, transcripts, synthetic_mode)))
    return out


def ends_with_digits(text: str) -> bool:
    """True if the text ends in the middle of a number (its last word is a digit). The FSM uses this to decide
    to wait longer before replying, because the caller may be mid-dictation."""
    toks = [t for t in _tokenize(text, 0) if t.kind != "sep"]
    return bool(toks) and toks[-1].kind in ("digit", "numeral")


# --------------------------------------------------------------------------- the top-level call

def extract_intel(
    transcripts: Sequence[Transcript], *, synthetic_mode: bool = False, expected: str | None = None,
    min_run: int = 3, join_window_s: float = 2.5,
) -> list[Finding]:
    """Findings from a list of consecutive transcripts, in the order they were said.

    synthetic_mode  accept our staged 00-prefixed phone numbers and recognise our invented UPI handle. Off by default.
    expected        what the FSM has just asked for ("phone", "account" or "case"). Only a tie-break when there is
                    no specific keyword, and it lowers the confidence to "expected".
    min_run         shortest digit run counted as a number. 3 keeps "एक parcel" ("a parcel") from being a digit.
    join_window_s   transcripts this close together (see _stream) are read as one dictation.
    """
    if expected is not None and expected not in KINDS_EXPECTED:
        raise ValueError(f"expected must be one of {KINDS_EXPECTED} or None")
    toks = _stream(transcripts, join_window_s)
    claimed: set[int] = set()
    found: list[tuple[int, Finding]] = []

    found += _find_written_vpas(toks, claimed, transcripts, synthetic_mode)
    found += _find_spoken_vpas(toks, claimed, transcripts, synthetic_mode)

    letter_runs = _letter_runs(toks, claimed)
    ifsc_starts = [k for k, t in enumerate(toks) if t.kind == "word" and t.norm in IFSC_WORDS and k not in claimed]
    ifsc_starts += [idxs[-1] for value, idxs in letter_runs if value == "IFSC"]   # spelled "आई एफ एस सी"
    for k in sorted(ifsc_starts):
        chars, idxs = _parse_ifsc_tail(toks, k + 1, claimed)
        if not chars:
            continue
        claimed.update(idxs)
        ok = is_valid_ifsc(chars)
        reason = "" if ok else f"{len(chars)} characters; an IFSC is 4 letters, 0, then 6 letters or digits"
        f = _make(toks, idxs, transcripts, kind="ifsc", value=chars, valid=ok, reason=reason,
                  confidence="heard", needs_confirmation=True)
        found.append((idxs[0], f))

    for value, idxs in letter_runs:
        if value == "IFSC" or any(k in claimed for k in idxs):
            continue
        claimed.update(idxs)
        f = _make(toks, idxs, transcripts, kind="letters", value=value, valid=True,
                  reason="letters are often misheard", confidence="heard", needs_confirmation=True)
        found.append((idxs[0], f))

    for run in _build_runs(toks, claimed):
        unit = _amount_unit(toks, run)
        if unit:
            # "500 rupaye", "Rs 5000", "do lakh": an amount of money, not an identifier. Recorded as its own kind
            # (with quote and time) so the evidence packet can show what was demanded; never read back, never classified.
            idxs = [k for k in range(run.first, run.last + 1) if toks[k].kind in ("digit", "numeral", "mult")]
            f = _make(toks, idxs, transcripts, kind="amount", value=run.digits, valid=True,
                      reason=f"an amount (next to {unit!r})", confidence="heard", needs_confirmation=False)
            found.append((run.first, f))
            continue
        if len(run.digits) < min_run:
            continue
        kind, confidence, candidates = _classify(run, toks, claimed, expected)
        valid, reason = _check(kind, run.digits, synthetic_mode)
        if run.near_other_run:
            reason = "; ".join(filter(None, [reason, "one unrecognised word separates this from another number: a digit may be missing"]))
        idxs = [k for k in range(run.first, run.last + 1) if toks[k].kind in ("digit", "numeral", "mult")]
        f = _make(toks, idxs, transcripts, kind=kind, value=run.digits, valid=valid, reason=reason,
                  confidence=confidence, candidates=candidates,
                  needs_confirmation=confidence != "keyword" or not valid or run.near_other_run)
        found.append((run.first, f))

    found.sort(key=lambda pair: pair[0])   # the order they were said
    return [f for _, f in found]
