"""Deterministic, same-length, case-preserving pseudonyms.

pseudo(s) rewrites s token by token (a token is a run of letters/digits).  A
token's replacement depends only on its lower-cased text, so a phrase and its
parts always agree ("Jane Doe" -> "Xaro Vek", "Doe" -> "Vek").
Each character keeps its class, case and encoded byte length (ASCII letter ->
ASCII letter, Latin-1 letter -> Latin-1 letter, digit -> digit), so replacing
in binary files never shifts offsets.
"""
from __future__ import annotations

import hashlib
import os
import re

SALT = os.environ.get("DEID_SALT", "machina-deid-v1-4letteruni")

CONS = "bcdfghjklmnprstvz"
VOWS = "aeiou"
LAT_CONS = "çñßðþ"  # 2-byte UTF-8, 1-byte cp1252
LAT_VOWS = "áéíóúàèìòùäëïöü"

TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, "words20k.txt"), encoding="utf-8") as fh:
    _WORDS = [w.strip() for w in fh if w.strip()]
COMMON = set(_WORDS)            # lower-case common words
COMMON_TOP = set(_WORDS[:1500])  # very common words, kept even when Capitalised

# Words that are never identifying on their own; kept inside identity phrases.
GENERIC = set("""
university institute institut laboratory laboratories lab labs college school
inc llc ltd gmbh corp corporation company co ag sa srl bv plc limited group
foundation consortium community engineering technologies technology robotics
hardware open source the of and for de la le der die das und en et del von
project projects design designs cad models model parts part files drawings
original native custom main master release rev version v1 v2 v3 public
department dept center centre research
""".split())


def _h(token: str) -> bytes:
    return hashlib.sha256((SALT + "\x00" + token).encode("utf-8")).digest()


_cache: dict[str, str] = {}


def pseudo_token(tok: str) -> str:
    """Same-length replacement for one alnum token, preserving case per char."""
    low = tok.lower()
    base = _cache.get(low)
    if base is None:
        h = _h(low)
        i = 0
        out = []
        for ch in low:
            b = h[i % len(h)]
            i += 1
            if ch.isdigit() and ch.isascii():
                out.append(str(b % 10))
            elif ch.isascii() and ch.isalpha():
                # alternate consonant / vowel by position for pronounceability
                if len(out) % 2 == 0:
                    out.append(CONS[b % len(CONS)])
                else:
                    out.append(VOWS[b % len(VOWS)])
            elif ch.isalpha() and len(ch.encode("utf-8")) == 2 and ch.encode("cp1252", "ignore"):
                pool = LAT_VOWS if len(out) % 2 else LAT_CONS
                out.append(pool[b % len(pool)])
            elif ch.isdigit():
                out.append(ch)
            else:
                out.append(ch)
            if i >= len(h):
                h = hashlib.sha256(h).digest()
                i = 0
        base = "".join(out)
        if base == low and any(c.isalpha() for c in low):
            base = base[::-1]
        _cache[low] = base
    # re-apply case of the original token
    res = []
    for o, p in zip(tok, base):
        if o.isupper():
            up = p.upper()
            res.append(up if len(up) == 1 else p)
        else:
            res.append(p)
    return "".join(res)


def keep_token(tok: str) -> bool:
    low = tok.lower()
    if tok.isdigit():
        return True
    if low in GENERIC:
        return True
    if tok.islower() and low in COMMON:
        return True
    if tok[:1].isupper() and low in COMMON_TOP:
        return True
    return False


def pseudo(s: str, keep_generic: bool = True) -> str:
    """Pseudonymise every non-generic token of s; separators are kept."""
    def rep(m):
        t = m.group(0)
        if keep_generic and keep_token(t):
            return t
        return pseudo_token(t)
    return TOKEN_RE.sub(rep, s)


if __name__ == "__main__":
    for s in ["Jane Doé", "Doé", "JDOE", "jdoe42", "Example-Robotics",
              "University Springfield WidgetLeg original knee parts", "jane@example.edu"]:
        p = pseudo(s)
        assert len(p) == len(s) and len(p.encode()) == len(s.encode()), (s, p)
        print(f"{s!r:60} -> {p!r}")
