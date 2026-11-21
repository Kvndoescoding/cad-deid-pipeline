"""Self-check an output corpus: residual identifiers and file validity."""
import collections, io, json, os, sys, zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ahocorasick_rs as acr

out, work = sys.argv[1], sys.argv[2]


def L(p):
    return "\\\\?\\" + os.path.abspath(p) if os.name == "nt" else p


mapping = json.load(open(os.path.join(work, "mapping.json"), encoding="utf-8"))
keys = [k for k in mapping if len(k) >= 6]
pats = []
for k in keys:
    for v in {k, k.lower(), k.upper()}:
        for enc in ("utf-8", "utf-16-le"):
            pats.append((v.encode(enc), k))
ac = acr.BytesAhoCorasick([p for p, _ in pats])
resid = collections.Counter(); resid_files = collections.defaultdict(set)
bad = collections.Counter(); badlist = []
n = 0
for root, _, files in os.walk(L(out)):
    for f in files:
        n += 1
        p = os.path.join(root, f)
        rel = os.path.relpath(p, L(out)).replace(os.sep, "/")
        data = open(p, "rb").read()
        for idx, st, en in ac.find_matches_as_indexes(data):
            k = pats[idx][1]; resid[k] += 1; resid_files[k].add(rel)
        for idx, st, en in ac.find_matches_as_indexes(rel.encode()):
            k = pats[idx][1]; resid["PATH:" + k] += 1
        ext = os.path.splitext(f)[1].lower()
        try:
            if data[:5] == b"%PDF-":
                import pymupdf
                d = pymupdf.open(stream=data, filetype="pdf"); _ = d.page_count; d[0].get_text() if d.page_count else None
            elif data[:4] == b"PK\x03\x04":
                z = zipfile.ZipFile(io.BytesIO(data)); assert z.testzip() is None
            elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                import olefile
                o = olefile.OleFileIO(io.BytesIO(data)); o.listdir(); o.get_metadata()
        except Exception as e:
            bad[ext] += 1; badlist.append((rel, repr(e)[:100]))
print("files", n)
print("residual keys:", len(resid))
for k, c in resid.most_common(40):
    print(f"  {c:6} {k!r}  e.g. {sorted(resid_files.get(k, ['-']))[:1]}")
print("invalid:", dict(bad)); [print("  ", b) for b in badlist[:20]]
