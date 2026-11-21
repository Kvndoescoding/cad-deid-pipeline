#!/usr/bin/env python3
"""Machina CAD De-ID pipeline.

  python run.py harvest   --input INPUT --work WORK          # collect identifier candidates
  python run.py mapping   --work WORK                         # build private identity mapping
  python run.py transform --input INPUT --work WORK --out OUT  # write OUT/corpus + notes
  python run.py all       --input INPUT --work WORK --out OUT

WORK holds private state (candidates, mapping, logs) and must stay out of the submission.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deid import harvest as H  # noqa: E402
from deid.pseudo import pseudo, COMMON, COMMON_TOP, GENERIC, TOKEN_RE, keep_token  # noqa: E402


def L(p: str) -> str:
    if os.name == "nt":
        p = os.path.abspath(p)
        return p if p.startswith("\\\\?\\") else "\\\\?\\" + p
    return p


def list_input(inp: str) -> list[tuple[str, int]]:
    base = L(inp)
    out = []
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".get-input")]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            out.append((rel, os.path.getsize(full)))
    out.sort(key=lambda x: -x[1])
    return out


# ------------------------------------------------------------------ harvest
def _harvest_one(args):
    inp, rel, scratch = args
    sink = H.Sink()
    H.harvest_path(rel, sink)
    try:
        with open(os.path.join(L(inp), *rel.split("/")), "rb") as fh:
            data = fh.read()
        H.harvest_blob(rel, data, sink, rel, 0, scratch)
    except Exception as e:
        return rel, sink.items, sink.where, f"{type(e).__name__}: {e}"
    return rel, sink.items, sink.where, None


def cmd_harvest(a):
    os.makedirs(a.work, exist_ok=True)
    files = list_input(a.input)
    print(f"harvest: {len(files)} files", flush=True)
    agg = collections.Counter()
    nfiles = collections.Counter()
    where = {}
    t0 = time.time()
    with ProcessPoolExecutor(a.jobs) as ex:
        futs = [ex.submit(_harvest_one, (a.input, rel, a.scratch)) for rel, _ in files]
        for i, f in enumerate(as_completed(futs)):
            rel, items, wh, err = f.result()
            for k, v in items.items():
                agg[k] += v
                nfiles[k] += 1
                where.setdefault(k, wh.get(k, rel))
            if (i + 1) % 2000 == 0:
                print(f"  {i+1}/{len(files)} {time.time()-t0:.0f}s, {len(agg)} candidates", flush=True)
    with open(os.path.join(a.work, "candidates.tsv"), "w", encoding="utf-8") as fh:
        for (v, k), c in sorted(agg.items(), key=lambda x: -nfiles[x[0]]):
            fh.write(f"{v}\t{k}\t{c}\t{nfiles[(v, k)]}\t{where[(v, k)]}\n")
    print(f"harvest done: {len(agg)} candidates in {time.time()-t0:.0f}s")


# ------------------------------------------------------------------ mapping
PLACEHOLDER_RE = re.compile(r"^(ID_\d+|PERSON_[A-Z_ ]+|ORGANIZATION_[A-Z_]+|UNSPECIFIED[ A-Z]*|NONE|N/?A)$", re.I)
SOFTWARE = {s.lower() for s in [
    "Open CASCADE", "OpenCASCADE", "OCCT", "SPATIAL CORP", "Spatial Corp", "kicad StepUp", "ksu MCAD",
    "ZWCAD Software Co.", "ZWCAD", "Ultimaker", "ezdxf", "mbed", "SDRC", "jedec", "youtu", "STEP Tools",
    "ST-Developer", "Datakit", "CoreTechnologie", "TransMagic", "Elysium", "PDF Publisher", "Theorem",
    "Open CASCADE STEP processor", "HOOPS", "Tech Soft", "Siemens PLM", "Dassault Systemes"]}


ENGWORDS = set("""
arrow block grow applied bureau barefoot astro steel aluminum aluminium brass copper bronze plastic nylon
abs pla petg delrin acetal titanium stainless rubber wood glass carbon fiber fibre acrylic polycarbonate
bolt nut screw washer bearing shaft gear spring bracket plate housing cover base frame mount assembly
part drawing sheet scale title rev revision date material finish weight mass volume density thread
metric imperial inch inches millimeter millimeters tolerance angle radius diameter length width height
depth thickness hole slot chamfer fillet extrude revolve sketch body solid surface mesh model default
front back left right top bottom side view section detail isometric checked drawn approved
""".split())


def _ok_key(v: str, kind: str) -> bool:
    if len(v) < 4 or len(v) > 100:
        return False
    if v.lower() in H.NEVER or v.lower() in SOFTWARE or PLACEHOLDER_RE.match(v):
        return False
    toks = TOKEN_RE.findall(v)
    if not toks:
        return False
    if len(toks) == 1 and len(toks[0]) == len(v):
        low1 = toks[0].lower()
        if low1 in ENGWORDS or low1 in COMMON_TOP:
            return False
        if kind != "id" and low1 in COMMON:
            return False
    if any(t.lower() in SOFTWARE for t in toks) and kind != "phrase" and "@" not in v:
        return False
    if kind == "weak":
        if len(toks) < 2 or len(toks) > 3 or len(v) < 6:
            return False
        for t in toks:
            if not (t[:1].isupper() and (t[1:].islower() or t.isupper())) or len(t) < 3:
                return False
            if t.lower() in COMMON or t.lower() in GENERIC:
                return False
    if all(t.isdigit() for t in toks):
        return False
    low = v.lower()
    if kind == "id":
        if len(toks) == 1 and (low in COMMON_TOP or low in GENERIC):
            return False
        if re.fullmatch(r"[0-9a-f]{8,}", low):  # hashes
            return False
        if re.search(r"\.(step|stp|sldprt|sldasm|pdf|dwg|igs|iges|ipt|iam)$", low):
            return False
    if kind == "weak":
        if any(t.lower() in COMMON or t.lower() in GENERIC for t in toks):
            return False
    return True


def cmd_mapping(a):
    rows = []
    with open(os.path.join(a.work, "candidates.tsv"), encoding="utf-8") as fh:
        for line in fh:
            v, k, c, nf, w = line.rstrip("\n").split("\t", 4)
            rows.append((v, k, int(c), int(nf)))
    extra = set(l.strip() for l in open(a.extra, encoding="utf-8")) if a.extra and os.path.exists(a.extra) else set()
    mapping: dict[str, str] = {}
    kinds: dict[str, str] = {}

    def put(v, kind):
        if not _ok_key(v, kind):
            return
        m = re.search(r"(\.[A-Za-z]{2,6})$", v)
        if m and ("@" in v or kind == "domain"):
            r = pseudo(v[:m.start()], keep_generic=False) + m.group(1)
        else:
            r = pseudo(v, keep_generic=(kind == "phrase"))
        if r == v:
            return
        if v not in mapping:
            mapping[v] = r
            kinds[v] = kind
        # standalone distinctive tokens so the identity is caught everywhere
        for t in TOKEN_RE.findall(v):
            if len(t) >= 5 and not t.isdigit() and not keep_token(t) and t.lower() not in COMMON and t.lower() not in H.NEVER:
                if t not in mapping:
                    mapping[t] = pseudo(t, keep_generic=False)
                    kinds[t] = "token"

    for v, k, c, nf in rows:
        put(v, k)
    for v in extra:
        put(v, "id")
    with open(os.path.join(a.work, "mapping.json"), "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, ensure_ascii=False, indent=0, sort_keys=True)
    with open(os.path.join(a.work, "mapping_kinds.tsv"), "w", encoding="utf-8") as fh:
        for v in sorted(mapping):
            fh.write(f"{v}\t{mapping[v]}\t{kinds[v]}\n")
    print(f"mapping: {len(mapping)} keys")


# ------------------------------------------------------------------ transform
_R = None


def _init_worker(work):
    global _R
    from deid.replacer import Replacer
    with open(os.path.join(work, "mapping.json"), encoding="utf-8") as fh:
        _R = Replacer(json.load(fh))


def _transform_one(args):
    inp, outc, rel, scratch = args
    from deid.transform import Ctx, process_blob
    ctx = Ctx(_R, scratch)
    src = os.path.join(L(inp), *rel.split("/"))
    try:
        with open(src, "rb") as fh:
            data = fh.read()
        new_rel, new_data = process_blob(rel, data, ctx)
        err = None
    except Exception as e:
        new_rel = ctx.path(rel)
        new_data, _ = _R.sub_bytes(data, text=False)
        err = f"{type(e).__name__}: {e}"
        ctx.notes.append(f"Processing error ({type(e).__name__}); cleaned at byte level only: {new_rel}")
    dst = os.path.join(L(outc), *new_rel.split("/"))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst):
        root, ext = os.path.splitext(dst)
        dst = root + "_dup" + ext
        new_rel = os.path.relpath(dst, L(outc)).replace(os.sep, "/")
    with open(dst, "wb") as fh:
        fh.write(new_data)
    return rel, new_rel, len(data), ctx.notes, err, (err and traceback.format_exc())


def cmd_transform(a):
    outc = os.path.join(a.out, "corpus")
    os.makedirs(outc, exist_ok=True)
    files = list_input(a.input)
    if a.limit:
        files = files[-a.limit:]
    print(f"transform: {len(files)} files", flush=True)
    t0 = time.time()
    notes, errors, empty = [], [], []
    done_log = open(os.path.join(a.work, "transform_log.tsv"), "w", encoding="utf-8")
    with ProcessPoolExecutor(a.jobs, initializer=_init_worker, initargs=(a.work,)) as ex:
        futs = [ex.submit(_transform_one, (a.input, outc, rel, a.scratch)) for rel, _ in files]
        for i, f in enumerate(as_completed(futs)):
            rel, new_rel, size, nts, err, tb = f.result()
            done_log.write(f"{rel}\t{new_rel}\t{size}\t{err or ''}\n")
            notes.extend(nts)
            if err:
                errors.append((new_rel, err))
            if size == 0:
                empty.append(new_rel)
            if (i + 1) % 2000 == 0:
                print(f"  {i+1}/{len(files)} {time.time()-t0:.0f}s", flush=True)
    done_log.close()
    with open(os.path.join(a.work, "notes.json"), "w", encoding="utf-8") as fh:
        json.dump({"notes": notes, "errors": errors, "empty": empty}, fh, ensure_ascii=False, indent=1)
    print(f"transform done in {time.time()-t0:.0f}s; {len(errors)} errors, {len(empty)} empty, {len(notes)} notes")


README_HEAD = """Machina CAD De-ID - submission notes (team 4letteruni)

HOW TO RUN
  Requirements: Python 3.10+, packages in code/requirements.txt, and the system tools
  gzip, ncompress (for .Z), unar or unrar (for .rar). code/vm_setup.sh installs them on Ubuntu.
    python3 code/run.py harvest   --input INPUT --work WORK --scratch SCRATCH
    python3 code/run.py mapping   --work WORK
    python3 code/run.py transform --input INPUT --work WORK --out OUT --scratch SCRATCH
    python3 code/run.py readme    --work WORK --out OUT
  WORK holds the private identity mapping and logs; it is never part of the submission.
  Pseudonyms are deterministic (salted SHA-256 per token), so re-runs give identical output.

METHOD
  1. Harvest identities from folder and file names (repository owners and projects, catalog
     collections, web domains), CAD metadata (STEP FILE_NAME author/organization, PERSON and
     ORGANIZATION entities, IGES global section, OLE SummaryInformation/DocumentSummaryInformation
     of SolidWorks/Inventor/Solid Edge files, FreeCAD Document.xml, 3MF metadata, DXF
     $LASTSAVEDBY), PDF metadata/XMP/title blocks, image EXIF, e-mail addresses, URLs and user
     home-directory paths, recursively inside every archive and PDF attachment.
  2. Each identifier token gets one pseudonym of exactly the same length, character classes and
     case pattern, so the same identity is renamed identically everywhere (paths, text, binary
     properties, archive member names) and different identities stay distinct. Generic engineering
     words inside project titles are kept.
  3. Files are rewritten in place-equivalent form: same-length byte replacement (UTF-8, cp1252,
     UTF-16LE) keeps binary CAD files, offsets, geometry and references valid; PDFs are edited with
     PyMuPDF (metadata, XMP, outlines, links, annotations, attachments, page text redacted and
     re-written with the pseudonym; vector graphics and images untouched); ZIP-family files
     (zip, 3mf, FCStd, f3d, odg), tar, gzip, .Z and 7z are unpacked, cleaned recursively and
     repacked in the same format with renamed members, so references between drawings, models
     and assemblies keep resolving. File extensions are never changed.

LIMITATIONS AND EXCEPTIONS
  - Text that exists only as pixels (logos, scanned title blocks, raster previews) is not OCRed.
  - Compressed proprietary streams (newer SolidWorks/Inventor internal blocks) are not
    decompressed; their uncompressed property and name records are cleaned.
  - Duplicate input files are kept as duplicates (each copy cleaned identically).
"""


def cmd_readme(a):
    with open(os.path.join(a.work, "notes.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    lines = [README_HEAD, "SOURCE DEFECTS AND SPECIAL CASES (final paths)"]
    if d["empty"]:
        lines.append(f"  Empty (0-byte) files in the source, kept as empty files ({len(d['empty'])}):")
        lines += [f"    {p}" for p in sorted(d["empty"])]
    if d["notes"]:
        lines.append(f"  Files handled with a fallback ({len(d['notes'])}):")
        lines += [f"    {n}" for n in sorted(set(d["notes"]))]
    if d["errors"]:
        lines.append(f"  Files with processing errors, cleaned at byte level ({len(d['errors'])}):")
        lines += [f"    {p}" for p, _ in sorted(d["errors"])]
    with open(os.path.join(a.out, "README.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("README written")


def cmd_merge(a):
    """Merge candidates.tsv files from shards (paths in --parts) into WORK/candidates.tsv."""
    agg, nf, where = collections.Counter(), collections.Counter(), {}
    for part in a.parts:
        with open(part, encoding="utf-8") as fh:
            for line in fh:
                v, k, c, n, w = line.rstrip("\n").split("\t", 4)
                agg[(v, k)] += int(c); nf[(v, k)] += int(n); where.setdefault((v, k), w)
    os.makedirs(a.work, exist_ok=True)
    with open(os.path.join(a.work, "candidates.tsv"), "w", encoding="utf-8") as fh:
        for key, c in sorted(agg.items(), key=lambda x: -nf[x[0]]):
            fh.write(f"{key[0]}\t{key[1]}\t{c}\t{nf[key]}\t{where[key]}\n")
    print(f"merged {len(a.parts)} parts: {len(agg)} candidates")


def cmd_mergenotes(a):
    d = {"notes": [], "errors": [], "empty": []}
    for part in a.parts:
        with open(part, encoding="utf-8") as fh:
            x = json.load(fh)
        for k in d:
            d[k].extend(x.get(k, []))
    os.makedirs(a.work, exist_ok=True)
    with open(os.path.join(a.work, "notes.json"), "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    print(f"merged notes from {len(a.parts)} parts")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["harvest", "mapping", "transform", "readme", "all", "merge", "mergenotes"])
    p.add_argument("--input", default="input")
    p.add_argument("--work", default="work")
    p.add_argument("--out", default="out")
    p.add_argument("--scratch", default=os.environ.get("TMPDIR", "/tmp"))
    p.add_argument("--jobs", type=int, default=os.cpu_count())
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--extra", default="", help="file with extra identifiers, one per line")
    p.add_argument("--parts", nargs="*", default=[])
    a = p.parse_args()
    if a.cmd == "merge":
        return cmd_merge(a)
    if a.cmd == "mergenotes":
        return cmd_mergenotes(a)
    os.makedirs(a.scratch, exist_ok=True)
    if a.cmd in ("harvest", "all"):
        cmd_harvest(a)
    if a.cmd in ("mapping", "all"):
        cmd_mapping(a)
    if a.cmd in ("transform", "all"):
        cmd_transform(a)
    if a.cmd in ("readme", "all"):
        cmd_readme(a)


if __name__ == "__main__":
    main()
