"""Rewrite one file (recursively for containers) with the identity mapping."""
from __future__ import annotations

import io
import os
import re
import tempfile

from . import containers as C
from .harvest import is_text
from .replacer import Replacer


class Ctx:
    def __init__(self, R: Replacer, scratch: str):
        self.R = R
        self.scratch = scratch
        self.notes: list[str] = []  # neutral notes for README (use final paths)

    def path(self, p: str) -> str:
        """Map each segment of a /-separated path, keeping the extension."""
        segs = p.split("/")
        out = []
        for s in segs:
            if not s or s in (".", ".."):
                out.append(s)
                continue
            out.append(self.R.sub_str(s))
        return "/".join(out)


def process_blob(name: str, data: bytes, ctx: Ctx, depth: int = 0) -> tuple[str, bytes]:
    new_name = ctx.path(name)
    if not data:
        return new_name, data
    if depth > 6:
        out, _ = ctx.R.sub_bytes(data, text=False)
        return new_name, out
    kind = C.kind_of(name, data)
    try:
        if kind == "zip":
            def fn(n, payload):
                if payload is None:
                    return ctx.path(n), None
                return process_blob(n, payload, ctx, depth + 1)
            return new_name, C.zip_rebuild(data, fn)
        if kind == "gz":
            inner = C.gz_read(data)
            _, new_inner = process_blob(C.gz_inner_name(name), inner, ctx, depth + 1)
            return new_name, C.gz_write(new_inner)
        if kind == "Z":
            inner = C.z_read(data)
            _, new_inner = process_blob(name[:-2], inner, ctx, depth + 1)
            return new_name, C.z_write(new_inner)
        if kind == "tar":
            return new_name, C.tar_rebuild(data, lambda n, p: process_blob(n, p, ctx, depth + 1), ctx.path)
        if kind == "7z":
            members = [process_blob(n, p, ctx, depth + 1) for n, p in C.seven_members(data, ctx.scratch)]
            return new_name, C.seven_write(members, ctx.scratch)
        if kind == "rar":
            # RAR cannot be rewritten without proprietary tools: repack the cleaned members as ZIP.
            members = [process_blob(n, p, ctx, depth + 1) for n, p in C.rar_members(data, ctx.scratch)]
            buf = io.BytesIO()
            import zipfile
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for n, p in members:
                    z.writestr(n, p)
            ctx.notes.append(f"RAR archive repacked as ZIP content (extension kept): {new_name}")
            return new_name, buf.getvalue()
    except Exception as e:  # unreadable container: fall back to byte-level pass
        ctx.notes.append(f"Container could not be unpacked ({type(e).__name__}); cleaned at byte level only: {new_name}")
        out, _ = ctx.R.sub_bytes(data, text=False)
        return new_name, out

    if data[:5] == b"%PDF-":
        try:
            return new_name, process_pdf(data, ctx, depth)
        except Exception as e:
            ctx.notes.append(f"PDF could not be parsed ({type(e).__name__}); cleaned at byte level only: {new_name}")
            out, _ = ctx.R.sub_bytes(data, text=False)
            return new_name, out
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return new_name, process_ole(data, ctx)
    text = is_text(name, data)
    out, _ = ctx.R.sub_bytes(data, text=text)
    return new_name, out


# --------------------------------------------------------------------- PDF
def process_pdf(data: bytes, ctx: Ctx, depth: int) -> bytes:
    import pymupdf
    R = ctx.R
    doc = pymupdf.open(stream=data, filetype="pdf")
    if doc.needs_pass and not doc.authenticate(""):
        raise ValueError("encrypted")
    md = doc.metadata or {}
    newmd = {}
    for k in ("title", "author", "subject", "keywords", "creator", "producer", "creationDate", "modDate"):
        v = md.get(k) or ""
        newmd[k] = R.sub_str(v) if k not in ("creationDate", "modDate") else v
    if any(newmd[k] != (md.get(k) or "") for k in newmd):
        doc.set_metadata(newmd)
    try:
        x = doc.get_xml_metadata()
        if x:
            nx = R.sub_str(x)
            if nx != x:
                doc.set_xml_metadata(nx)
    except Exception:
        pass
    # attachments
    for i in range(doc.embfile_count()):
        try:
            info = doc.embfile_info(i)
            fname = info.get("filename") or "attachment.bin"
            content = doc.embfile_get(i)
            nn, nc = process_blob(fname, content, ctx, depth + 1)
            doc.embfile_upd(i, nc, filename=nn, ufilename=nn, desc=R.sub_str(info.get("desc") or ""))
        except Exception:
            pass
    # outline
    try:
        toc = doc.get_toc(False)
        ntoc = [[t[0], R.sub_str(t[1]), t[2]] + list(t[3:]) for t in toc]
        if any(a[1] != b[1] for a, b in zip(toc, ntoc)):
            doc.set_toc(ntoc)
    except Exception:
        pass
    # pages
    for page in doc:
        text = page.get_text()
        hits = {}
        if R.s_ac and text:
            for idx, st, en in R.s_ac.find_matches_as_indexes(text):
                k = R.s_keys[idx]
                if len(k) < 6 and ((st > 0 and text[st - 1].isalnum()) or (en < len(text) and text[en].isalnum())):
                    continue
                hits[k] = R.s_reps[idx]
        placed = []
        for k, rep in hits.items():
            try:
                quads = page.search_for(k, quads=True)
            except Exception:
                quads = []
            for q in quads:
                page.add_redact_annot(q.rect, fill=False, cross_out=False)
                placed.append((q, rep))
        if placed:
            try:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                                      graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                      text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            except TypeError:
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)
            import math
            for q, rep in placed:
                try:
                    ul, ur, ll = q.ul, q.ur, q.ll
                    width = abs(ur - ul)
                    height = abs(ll - ul)
                    if width <= 0 or height <= 0:
                        continue
                    ang = math.degrees(math.atan2(ur.y - ul.y, ur.x - ul.x))
                    rot = int((-round(ang / 90.0) * 90) % 360)
                    fs = height * 0.82
                    tl = pymupdf.get_text_length(rep, fontname="helv", fontsize=fs)
                    if tl > width:
                        fs *= width / tl
                    pt = ll + (ul - ll) * 0.22
                    page.insert_text(pt, rep, fontsize=max(1.0, fs), fontname="helv", rotate=rot)
                except Exception:
                    pass
        for link in page.get_links():
            u = link.get("uri")
            if u:
                nu = R.sub_str(u)
                if nu != u:
                    link["uri"] = nu
                    try:
                        page.update_link(link)
                    except Exception:
                        pass
        for annot in page.annots() or []:
            try:
                info = annot.info
                nc = R.sub_str(info.get("content", "") or "")
                nt = R.sub_str(info.get("title", "") or "")
                if nc != info.get("content") or nt != info.get("title"):
                    annot.set_info(content=nc, title=nt)
                    annot.update()
            except Exception:
                pass
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    # catch identifiers in remaining uncompressed objects (same length, offsets unchanged)
    out, _ = R.sub_bytes(out, text=False)
    return out


# --------------------------------------------------------------------- OLE
def process_ole(data: bytes, ctx: Ctx) -> bytes:
    """Property-set streams get the full (short-key) pass; the whole file gets the long-key pass."""
    import olefile
    R = ctx.R
    try:
        fd, tmp = tempfile.mkstemp(dir=ctx.scratch, suffix=".ole")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            o = olefile.OleFileIO(tmp, write_mode=True)
            for entry in o.listdir():
                if not entry[-1].startswith("\x05"):
                    continue
                s = o.openstream(entry).read()
                ns, n = R.sub_bytes(s, text=True)
                if n and len(ns) == len(s):
                    o.write_stream(entry, ns)
            o.close()
            with open(tmp, "rb") as fh:
                data = fh.read()
        finally:
            os.unlink(tmp)
    except Exception:
        pass
    out, _ = R.sub_bytes(data, text=False)
    return out
