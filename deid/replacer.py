"""Multi-pattern, same-length replacement over bytes (several encodings) and str."""
from __future__ import annotations

import ahocorasick_rs as acr

SHORT = 6  # keys shorter than this need word boundaries, and are skipped in opaque binaries


def case_variants(k: str) -> set[str]:
    out = {k, k.lower(), k.upper(), k.title(), k[:1].upper() + k[1:]}
    return {v for v in out if len(v) == len(k)}


def transfer_case(src: str, rep: str) -> str:
    res = []
    for s, r in zip(src, rep):
        if s.isupper():
            u = r.upper()
            res.append(u if len(u) == 1 else r)
        elif s.islower():
            l = r.lower()
            res.append(l if len(l) == 1 else r)
        else:
            res.append(r)
    return "".join(res)


def _alnum_byte(b: int) -> bool:
    return 48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122


class Replacer:
    def __init__(self, mapping: dict[str, str]):
        pairs: dict[str, str] = {}
        for k, r in mapping.items():
            if not k or k == r:
                continue
            for v in case_variants(k):
                rv = transfer_case(v, r)
                if v != rv and len(v.encode("utf-8")) == len(rv.encode("utf-8")):
                    pairs.setdefault(v, rv)
        self.pairs = pairs
        # str automaton (paths, XML/metadata strings)
        self.s_keys = list(pairs)
        self.s_reps = [pairs[k] for k in self.s_keys]
        self.s_ac = acr.AhoCorasick(self.s_keys, matchkind=acr.MATCHKIND_LEFTMOST_LONGEST) if pairs else None
        # bytes automatons: (pattern, replacement, char_width, short)
        pats, reps, meta = [], [], []
        seen = set()
        for v, rv in pairs.items():
            short = len(v) < SHORT
            for enc, w in (("utf-8", 1), ("cp1252", 1), ("utf-16-le", 2)):
                try:
                    pb, rb = v.encode(enc), rv.encode(enc)
                except UnicodeEncodeError:
                    continue
                if len(pb) != len(rb) or pb in seen:
                    continue
                seen.add(pb)
                pats.append(pb); reps.append(rb); meta.append((w, short))
        self.b_pats, self.b_reps, self.b_meta = pats, reps, meta
        self.b_ac = acr.BytesAhoCorasick(pats, matchkind=acr.MATCHKIND_LEFTMOST_LONGEST) if pats else None
        long_idx = [i for i, m in enumerate(meta) if not m[1]]
        self.bl_map = long_idx
        self.bl_ac = acr.BytesAhoCorasick([pats[i] for i in long_idx], matchkind=acr.MATCHKIND_LEFTMOST_LONGEST) if long_idx else None

    # -- str -------------------------------------------------------------
    def sub_str(self, s: str) -> str:
        if not self.s_ac or not s:
            return s
        out, last, changed = [], 0, False
        for idx, st, en in self.s_ac.find_matches_as_indexes(s):
            k = self.s_keys[idx]
            if len(k) < SHORT:
                if (st > 0 and s[st - 1].isalnum()) or (en < len(s) and s[en].isalnum()):
                    continue
            out.append(s[last:st]); out.append(self.s_reps[idx]); last = en; changed = True
        if not changed:
            return s
        out.append(s[last:])
        return "".join(out)

    # -- bytes -----------------------------------------------------------
    def sub_bytes(self, data: bytes, text: bool = True) -> tuple[bytes, int]:
        """Replace in place (same length). text=False skips short keys (opaque binaries)."""
        if text:
            ac, idxmap = self.b_ac, None
        else:
            ac, idxmap = self.bl_ac, self.bl_map
        if ac is None or not data:
            return data, 0
        buf = None
        n = 0
        L = len(data)
        for idx, st, en in ac.find_matches_as_indexes(data):
            j = idxmap[idx] if idxmap is not None else idx
            w, short = self.b_meta[j]
            if short:
                if w == 1:
                    if (st > 0 and _alnum_byte(data[st - 1])) or (en < L and _alnum_byte(data[en])):
                        continue
                else:
                    if (st > 1 and _alnum_byte(data[st - 2]) and data[st - 1] == 0) or \
                       (en + 1 < L and _alnum_byte(data[en]) and data[en + 1] == 0):
                        continue
            if buf is None:
                buf = bytearray(data)
            buf[st:en] = self.b_reps[j]
            n += 1
        return (bytes(buf) if buf is not None else data), n
