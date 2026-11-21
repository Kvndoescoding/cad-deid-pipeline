"""Collect identifier candidates from paths and file contents.

Each candidate is (value, kind) where kind is one of:
  id      - replace every token (owner/repo names, people, emails, domains)
  phrase  - replace only non-generic tokens (project / collection titles)
"""
from __future__ import annotations

import io
import os
import re
import zipfile

from . import containers as C

# Software / vendor / standards names: never treated as identities.
NEVER = {s.lower() for s in """
FreeCAD SolidWorks SOLIDWORKS Autodesk Inventor Fusion Fusion360 PTC Creo Siemens NX Solid Edge
Dassault CATIA Onshape KiCad KiCAD Rhino Blender OpenSCAD CadQuery PrusaSlicer Slic3r Cura Inkscape
Adobe Acrobat Microsoft Windows Apple macOS Linux Ubuntu Google GitHub GitLab Open CASCADE OpenCASCADE
STEP IGES ACIS Parasolid Spatial SpaceClaim ANSYS Abaqus MATLAB Python LibreOffice OpenOffice Qt cairo
Ghostscript Word Excel PowerPoint Visio AutoCAD DraftSight QCAD LibreCAD BricsCAD eDrawings Unknown
unknown Author author Administrator administrator admin Admin user User Owner owner Default default none
None anonymous Anonymous root guest Guest test Test UNSPECIFIED unspecified undefined Undefined N/A
NONE USER ADMIN AUTHOR OWNER STEP-Tools steptools ST-Developer Kubotek Solidworks solidworks
""".split()}

STD_DOMAINS = {
    "w3.org", "adobe.com", "purl.org", "wikipedia.org", "creativecommons.org", "inkscape.org",
    "sourceforge.net", "cairographics.org", "gimp.org", "freecadweb.org", "freecad.org",
    "solidworks.com", "autodesk.com", "ptc.com", "spatial.com", "steptools.com", "microsoft.com",
    "apple.com", "iptc.org", "useplus.org", "openxmlformats.org", "xmlsoap.org", "github.com",
    "gitlab.com", "google.com", "youtube.com", "opencascade.com", "opencascade.org", "iso.org",
    "w3c.org", "xml.org", "oasis-open.org", "openoffice.org", "libreoffice.org", "mozilla.org",
    "python.org", "gnu.org", "apache.org", "githubusercontent.com", "schemas.microsoft.com",
    "3ds.com", "siemens.com", "onshape.com", "kicad.org", "openscad.org", "prusa3d.com",
    "ultimaker.com", "sketchup.com", "trimble.com", "example.com", "example.org", "dublincore.org",
    "ns.adobe.com", "openGIS.net", "opengis.net", "x3d.org", "web3d.org", "ietf.org", "unicode.org",
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com", "aol.com", "icloud.com",
    "sodipodi.sourceforge.net", "ilovepdf.com", "smallpdf.com", "pdfforge.org", "ghostscript.com",
}
SLD_2 = {"co", "com", "ac", "org", "net", "gov", "edu", "or", "ne", "go"}

EMAIL_RE = re.compile(rb"(?<![A-Za-z0-9._%+-])([A-Za-z][A-Za-z0-9._%+-]{0,40})@((?:[A-Za-z0-9-]{1,40}\.){1,4}(?:com|org|net|edu|gov|mil|int|info|biz|io|[a-z]{2}))(?![A-Za-z0-9-])", re.I)
URL_RE = re.compile(rb"(?:https?://|www\.)((?:[A-Za-z0-9-]{1,63}\.){1,5}[A-Za-z]{2,10})")
USERPATH_RE = re.compile(rb"(?:[A-Za-z]:[\\/]+(?:Users|Documents and Settings|home)[\\/]+|/(?:home|Users)/)([A-Za-z0-9._ -]{2,40}?)(?=[\\/])", re.I)
WINDOMAIN_RE = re.compile(rb"\b([A-Z][A-Z0-9-]{2,15})\\([A-Za-z][A-Za-z0-9._-]{1,30})\b")

TEXT_EXT = {".step", ".stp", ".igs", ".iges", ".sat", ".dxf", ".svg", ".obj", ".wrl", ".vrml", ".ps",
            ".eps", ".unv", ".x_t", ".xmt_txt", ".txt", ".csv", ".xml", ".json", ".md", ".html", ".htm",
            ".scad", ".gcode", ".mtl", ".off", ".brep", ".ipj", ".model", ".rels", ".config", ".dat",
            ".ini", ".yaml", ".yml", ".py", ".fcmacro", ".nc", ".tex", ".rtf", ".log", ".cfg", ".gltf",
            ".p21", ".ifc", ".neu", ".asc", ".pts", ".mac", ".bas", ".lsp", ".scr", ".plt", ".ngc"}


def is_text(name: str, data: bytes) -> bool:
    ext = os.path.splitext(name)[1].lower()
    head = data[:65536]
    if ext in TEXT_EXT and b"\x00" not in head[:4096]:
        return True
    if head[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return True
    if not head or b"\x00" in head:
        return False
    if ext == ".stl" and head[:5].lower() == b"solid":
        return True
    printable = sum(1 for b in head[:4096] if 32 <= b < 127 or b in (9, 10, 13))
    return printable / max(1, len(head[:4096])) > 0.97


def registrable(host: str) -> str | None:
    host = host.lower().strip(".")
    parts = host.split(".")
    if len(parts) < 2:
        return None
    if len(parts) >= 3 and parts[-2] in SLD_2 and len(parts[-1]) == 2:
        reg = ".".join(parts[-3:])
    else:
        reg = ".".join(parts[-2:])
    for s in STD_DOMAINS:
        if host == s or host.endswith("." + s) or reg == s:
            return None
    return reg


class Sink:
    def __init__(self):
        self.items: dict[tuple[str, str], int] = {}
        self.where: dict[tuple[str, str], str] = {}

    def add(self, value, kind: str, src: str = ""):
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        value = value.strip().strip("'\"()[]{}<>,;:").strip()
        if not value or len(value) < 3 or len(value) > 120:
            return
        if "�" in value or any(ord(c) < 32 for c in value):
            return
        if value.lower() in NEVER:
            return
        if not re.search(r"[A-Za-zÀ-ɏ]", value):
            return
        k = (value, kind)
        self.items[k] = self.items.get(k, 0) + 1
        self.where.setdefault(k, src)


# ------------------------------------------------------------------ paths
def harvest_path(path: str, sink: Sink):
    parts = path.split("/")
    if parts[0] == "archives" and len(parts) > 1:
        stem = re.sub(r"\.(zip|tar|gz|tgz|7z)$", "", parts[1], flags=re.I)
        _owner_repo(stem, sink, path)
    if parts[0] != "sources" or len(parts) < 3:
        return
    src, top = parts[1], parts[2]
    if src == "github":
        _owner_repo(top, sink, path)
    elif src == "gitlab":
        bits = [b for b in top.split("__") if b and b.lower() != "gitlab.com"]
        for b in bits:
            if b.lower() not in ("project", "projects", "group", "groups"):
                sink.add(b, "id", path)
    elif src == "catalog":
        sink.add(top, "phrase", path)
    elif src == "web":
        reg = registrable(top)
        if reg:
            sink.add(reg.split(".")[0], "id", path)
    elif src == "existing":
        for seg in parts[4:6]:
            if "." in seg and seg.lower().endswith((".pdf", ".step", ".stp", ".sldprt")):
                continue
            seg = re.sub(r"\.extracted$", "", seg)
            sink.add(seg, "phrase", path)


def _owner_repo(top: str, sink: Sink, path: str):
    bits = top.split("__")
    if len(bits) >= 2:
        sink.add(bits[0], "id", path)
        sink.add(bits[1], "phrase", path)
    else:
        sink.add(top, "phrase", path)


# ------------------------------------------------------------------ content
def _step(data: bytes, sink: Sink, src: str):
    head = data[:20000]
    m = re.search(rb"FILE_NAME\s*\((.*?)\)\s*;", head, re.S)
    if m:
        body = re.sub(rb"/\*.*?\*/", b"", m.group(1), flags=re.S)
        strs = re.findall(rb"'((?:[^']|'')*)'", body)
        # name, time_stamp, author(s)..., organization(s)..., preproc, origsys, auth
        if len(strs) >= 7:
            for s in strs[2:-3]:
                sink.add(s, "id", src)
            sink.add(strs[-1], "id", src)
    for m in re.finditer(rb"\bPERSON\s*\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'", data[:5_000_000]):
        for g in m.groups():
            sink.add(g, "id", src)
        if m.group(2) and m.group(3):
            sink.add(m.group(3) + b" " + m.group(2), "id", src)
    for m in re.finditer(rb"\bORGANIZATION\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", data[:5_000_000]):
        sink.add(m.group(2), "id", src)


def _iges(data: bytes, sink: Sink, src: str):
    lines = data[:200000].split(b"\n")
    g = b"".join(l[:72] for l in lines if len(l) >= 73 and l[72:73] == b"G")
    if not g:
        return
    vals = []
    i = 0
    pd, rd = b",", b";"
    # Hollerith-aware split
    while i < len(g):
        m = re.match(rb"\s*(\d+)H", g[i:])
        if m:
            n = int(m.group(1))
            st = i + m.end()
            vals.append(g[st:st + n])
            i = st + n
        else:
            j = i
            while j < len(g) and g[j:j + 1] not in (pd, rd):
                j += 1
            vals.append(g[i:j].strip())
            i = j
        if i < len(g) and g[i:i + 1] in (pd, rd):
            i += 1
        if len(vals) == 1 and vals[0]:
            pd = vals[0][:1]
        if len(vals) == 2 and vals[1]:
            rd = vals[1][:1]
    for idx in (20, 21):  # author, organization (0-based)
        if idx < len(vals):
            sink.add(vals[idx], "id", src)


def _dxf(data: bytes, sink: Sink, src: str):
    m = re.search(rb"\$LASTSAVEDBY\s*\r?\n\s*1\r?\n([^\r\n]+)", data[:200000])
    if m:
        sink.add(m.group(1), "id", src)


def _svg(data: bytes, sink: Sink, src: str):
    for tag in (rb"dc:creator", rb"dc:rights", rb"dc:publisher", rb"dc:contributor"):
        for m in re.finditer(tag + rb">(.*?)</" + tag, data[:500000], re.S):
            for t in re.findall(rb"<dc:title>([^<]+)</dc:title>", m.group(1)):
                sink.add(t, "id", src)


def _xml_kv(data: bytes, sink: Sink, src: str):
    # FreeCAD Document.xml
    for m in re.finditer(rb'<Property name="(CreatedBy|LastModifiedBy|Company)"[^>]*>\s*<String value="([^"]*)"', data):
        sink.add(m.group(2), "id", src)
    # 3MF metadata
    for m in re.finditer(rb'<metadata name="(Designer|Copyright|Author|Company|Owner)"[^>]*>([^<]*)<', data, re.I):
        sink.add(m.group(2), "id", src)
    for m in re.finditer(rb"<(dc:creator|cp:lastModifiedBy|Company|Manager)>([^<]*)<", data):
        sink.add(m.group(2), "id", src)


def _generic(data: bytes, sink: Sink, src: str):
    for m in EMAIL_RE.finditer(data):
        local, dom = m.group(1), m.group(2).decode("ascii", "replace")
        if dom.split(".")[-1].lower() in ("png", "jpg", "gif", "tlb", "dll", "radius", "angle", "x", "y"):
            continue
        reg = registrable(dom)
        sink.add(local + b"@" + m.group(2), "id", src)
        if len(local) >= 3 and not local.lower() in (b"info", b"support", b"contact", b"sales", b"admin", b"webmaster", b"you", b"user", b"name", b"email", b"mail", b"noreply"):
            sink.add(local, "id", src)
        if reg:
            sink.add(reg.split(".")[0], "id", src)
    for m in URL_RE.finditer(data):
        reg = registrable(m.group(1).decode("ascii", "replace"))
        if reg:
            sink.add(reg.split(".")[0], "id", src)
    for m in USERPATH_RE.finditer(data):
        sink.add(m.group(1), "id", src)


def _ole(data: bytes, sink: Sink, src: str):
    import olefile
    try:
        o = olefile.OleFileIO(io.BytesIO(data))
    except Exception:
        return
    try:
        md = o.get_metadata()
        for k in ("author", "last_saved_by", "company", "manager"):
            v = getattr(md, k, None)
            if v:
                v = v.split(b"\x00")[0] if isinstance(v, bytes) else v
                sink.add(v.decode("cp1252", "replace") if isinstance(v, bytes) else v, "id", src)
        # Inventor / other property sets: grab person/company-like props by name heuristics
        for entry in o.listdir():
            if entry[-1].startswith("\x05"):
                try:
                    props = o.getproperties(entry, convert_time=True, no_conversion=[])
                except Exception:
                    continue
                for pid, v in props.items():
                    if pid in (4, 8, 14, 15) and isinstance(v, (str, bytes)):
                        sink.add(v.decode("cp1252", "replace") if isinstance(v, bytes) else v, "id", src)
    finally:
        o.close()


PDF_LABEL_RE = re.compile(
    r"(?:DRAWN|DRN|DRAFTER|DESIGNED|DESIGNER|DSGN|CHECKED|CHK|APPROVED|APPR|ENGINEER|ENG|AUTHOR|CREATED)"
    r"(?:\s*BY)?\s*[:.]?\s*\n?\s*([A-Z][A-Za-z'\-]+(?:[ .]+[A-Z][A-Za-z'\-]+){0,2})", re.I)
PROP_RE = re.compile(r"(?:PROPERTY OF|COPYRIGHT|©|\(c\))\s*(?:\d{4}\s*)?([A-Z][A-Za-z0-9&.,' \-]{2,60}?)(?:\.|,|\n| IT | AND | ALL )", re.I)


def _pdf(data: bytes, sink: Sink, src: str):
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception:
        return
    try:
        md = doc.metadata or {}
        a = md.get("author") or ""
        for part in re.split(r"[\\;,/]| \(|\)", a):
            sink.add(part, "id", src)
        x = doc.get_xml_metadata() or ""
        for m in re.finditer(r"<dc:creator>.*?</dc:creator>", x, re.S):
            for li in re.findall(r"<rdf:li[^>]*>([^<]+)</rdf:li>", m.group(0)):
                sink.add(li, "id", src)
        for i, page in enumerate(doc):
            if i >= 30:
                break
            t = page.get_text()
            for m in PDF_LABEL_RE.finditer(t):
                v = m.group(1).strip()
                if v.upper() in ("DATE", "BY", "SCALE", "TITLE", "SIZE", "SHEET", "NAME", "REV", "DWG", "NO", "QA", "MFG"):
                    continue
                sink.add(v, "weak", src)
            for m in PROP_RE.finditer(t):
                sink.add(m.group(1).strip(), "phrase", src)
            _generic(t.encode("utf-8", "replace"), sink, src)
        for i in range(doc.embfile_count()):
            try:
                info = doc.embfile_info(i)
                harvest_blob(info.get("filename") or "attach.bin", doc.embfile_get(i), sink, src + "#emb", 1)
            except Exception:
                pass
    finally:
        doc.close()


def _image(data: bytes, sink: Sink, src: str):
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        ex = im.getexif()
        for tag in (315, 33432, 0x9C9D):
            v = ex.get(tag)
            if v:
                if isinstance(v, bytes):
                    v = v.decode("utf-16-le", "ignore") if tag == 0x9C9D else v.decode("latin1")
                sink.add(v, "id", src)
        for k, v in (im.info or {}).items():
            if isinstance(v, str) and k.lower() in ("author", "copyright", "artist"):
                sink.add(v, "id", src)
    except Exception:
        pass


def harvest_blob(name: str, data: bytes, sink: Sink, src: str, depth: int = 0, scratch: str = "/tmp"):
    if depth > 6 or not data:
        return
    ext = os.path.splitext(name)[1].lower()
    kind = C.kind_of(name, data)
    try:
        if kind == "zip":
            for info, payload in C.zip_members(data):
                harvest_path_inner(info.filename, sink, src)
                if payload is not None:
                    harvest_blob(info.filename, payload, sink, src + "!" + info.filename, depth + 1, scratch)
            return
        if kind == "gz":
            harvest_blob(C.gz_inner_name(name), C.gz_read(data), sink, src, depth + 1, scratch)
            return
        if kind == "Z":
            harvest_blob(name[:-2], C.z_read(data), sink, src, depth + 1, scratch)
            return
        if kind == "tar":
            for m, payload in C.tar_members(data):
                harvest_path_inner(m.name, sink, src)
                if payload is not None:
                    harvest_blob(m.name, payload, sink, src + "!" + m.name, depth + 1, scratch)
            return
        if kind == "7z":
            for n, payload in C.seven_members(data, scratch):
                harvest_path_inner(n, sink, src)
                harvest_blob(n, payload, sink, src + "!" + n, depth + 1, scratch)
            return
        if kind == "rar":
            for n, payload in C.rar_members(data, scratch):
                harvest_path_inner(n, sink, src)
                harvest_blob(n, payload, sink, src + "!" + n, depth + 1, scratch)
            return
    except Exception:
        pass
    if data[:5] == b"%PDF-" or ext == ".pdf":
        _pdf(data, sink, src)
        return
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        _ole(data, sink, src)
    if ext in (".step", ".stp", ".p21") or data[:13] == b"ISO-10303-21;":
        _step(data, sink, src)
    elif ext in (".igs", ".iges"):
        _iges(data, sink, src)
    elif ext == ".dxf":
        _dxf(data, sink, src)
    elif ext == ".svg":
        _svg(data, sink, src)
    if ext in (".xml", ".model", ".svg") or name.endswith("Document.xml"):
        _xml_kv(data, sink, src)
    if ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
        _image(data, sink, src)
    _generic(data if len(data) < 50_000_000 else data[:50_000_000], sink, src)


def harvest_path_inner(member: str, sink: Sink, src: str):
    # archive member paths: only user-dir style names are harvested here
    for m in USERPATH_RE.finditer(member.encode("utf-8", "replace")):
        sink.add(m.group(1), "id", src)
