"""Is a payment or code keyword being warned against? A small, windowed check, not a blanket "any 'nahin' cancels".

In the friend session (staged role-play), the caller said "Payment karne ko bol sakte hain, aap aise mat kariyega" and the call went to
S3 (payment instructions) on the word "Payment". A keyword is negated when a prohibition word sits just before it, or a little after it
and is followed by a request verb. The words live in content/stage_rules.json ("negation"), so they can be edited without code.
Nothing here touches the network.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Negation:
    before: int
    after: int
    prohibitions: tuple[tuple[str, ...], ...]     # always a "do not": mat, never, don't ...
    soft: frozenset[str]                           # nahin / not: a "do not" only next to a request verb or after "need"
    need_words: frozenset[str]
    request_verbs: frozenset[str]
    bare_verbs: frozenset[str]                     # "kar", "karo": enough after a prohibition word, too loose after a soft one


NO_NEGATION = Negation(0, 0, (), frozenset(), frozenset(), frozenset(), frozenset())
GUARD_SPAN = 2        # a request verb must follow the negation word within this many words


def load_negation(raw: dict, words_of) -> Negation:
    def toks(items):
        return [tuple(words_of(i)) for i in items]
    return Negation(
        int(raw["before"]), int(raw["after"]),
        tuple(dict.fromkeys(toks(raw["prohibitions"]))),
        frozenset(t for p in toks(raw["soft"]) for t in p),
        frozenset(t for p in toks(raw["need_words"]) for t in p),
        frozenset(t for p in toks(raw["request_verbs"]) for t in p),
        frozenset(t for p in toks(raw["bare_verbs"]) for t in p),
    )


def occurrences(tokens: list[str], phrase: tuple[str, ...]) -> list[tuple[int, int]]:
    """(start, end) of every place the phrase occurs; end is one past the last word."""
    n = len(phrase)
    return [(i, i + n) for i in range(len(tokens) - n + 1) if tuple(tokens[i:i + n]) == phrase] if n else []


def negated_by(neg: Negation, tokens: list[str], start: int, end: int) -> str | None:
    """The negation word that cancels the keyword at tokens[start:end], or None."""
    if neg.after <= 0 and neg.before <= 0:
        return None
    lo, hi = max(0, start - neg.before), min(len(tokens), end + neg.after)

    def followed_by(i: int, span: int, verbs: frozenset[str]) -> bool:
        return any(t in verbs for t in tokens[i + 1:i + 1 + span])

    for i in range(lo, hi):
        if start <= i < end:
            continue
        before_it = i < start
        for p in neg.prohibitions:
            if tuple(tokens[i:i + len(p)]) == p and not (start <= i + len(p) - 1 < end):
                last = i + len(p) - 1
                if before_it or followed_by(last, GUARD_SPAN, neg.request_verbs | neg.bare_verbs):
                    return " ".join(p)
        if tokens[i] in neg.soft and not before_it:       # "nahin" follows what it negates in Hindi: only look after the keyword
            if followed_by(i, GUARD_SPAN, neg.request_verbs) or (i > 0 and tokens[i - 1] in neg.need_words):
                return tokens[i]
    return None


def counts(neg: Negation, tokens: list[str], phrase: tuple[str, ...]) -> tuple[bool, str | None]:
    """(does the keyword count, the negation word if it was ignored). It counts if ANY occurrence is not negated."""
    spots = occurrences(tokens, phrase)
    if not spots:
        return False, None
    why = None
    for start, end in spots:
        why = negated_by(neg, tokens, start, end)
        if why is None:
            return True, None
    return False, why
