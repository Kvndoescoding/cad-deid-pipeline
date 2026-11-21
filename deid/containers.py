"""Read and rebuild archive-like containers in memory."""
from __future__ import annotations

import gzip
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ZIP_EXT = {".zip", ".3mf", ".fcstd", ".f3d", ".f3z", ".odg", ".ods", ".odt", ".docx", ".xlsx",
           ".pptx", ".kmz", ".fcstd1", ".jar", ".sldzip", ".step.zip"}


def kind_of(name: str, data: bytes) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    h = data[:8]
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return "zip"
    if h[:2] == b"\x1f\x8b":
        return "gz"
    if h[:6] == b"7z\xbc\xaf'\x1c":
        return "7z"
    if h[:4] == b"Rar!":
        return "rar"
    if h[:2] == b"\x1f\x9d":
        return "Z"
    if len(data) > 262 and data[257:262] == b"ustar":
        return "tar"
    if ext == ".tar" and len(data) >= 512:
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
                t.getmembers()
            return "tar"
        except Exception:
            return None
    return None


# ---------------------------------------------------------------- zip
def zip_members(data: bytes):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                yield info, None
            else:
                yield info, z.read(info)


def zip_rebuild(data: bytes, fn) -> bytes:
    """fn(name, bytes|None) -> (new_name, new_bytes|None)."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", allowZip64=True) as zout:
        if zin.comment:
            zout.comment = zin.comment
        used = set()
        for info in zin.infolist():
            payload = None if info.is_dir() else zin.read(info)
            new_name, new_payload = fn(info.filename, payload)
            while new_name in used:
                root, ext = os.path.splitext(new_name.rstrip("/"))
                new_name = root + "_dup" + ext + ("/" if info.is_dir() else "")
            used.add(new_name)
            ni = zipfile.ZipInfo(new_name, date_time=info.date_time)
            ni.compress_type = info.compress_type if info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA) else zipfile.ZIP_DEFLATED
            ni.external_attr = info.external_attr
            ni.create_system = info.create_system
            ni.comment = info.comment
            ni.extra = b""
            if info.is_dir():
                zout.writestr(ni, b"")
            else:
                zout.writestr(ni, new_payload)
    return out.getvalue()


# ---------------------------------------------------------------- gz
def gz_inner_name(name: str) -> str:
    base = os.path.basename(name)
    low = base.lower()
    if low.endswith(".tgz"):
        return base[:-4] + ".tar"
    if low.endswith(".gz"):
        return base[:-3]
    return base + ".unpacked"


def gz_read(data: bytes) -> bytes:
    return gzip.decompress(data)


def gz_write(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=6, mtime=0)


# ---------------------------------------------------------------- .Z (LZW)
def z_read(data: bytes) -> bytes:
    p = subprocess.run(["gzip", "-dc"], input=data, capture_output=True)
    if p.returncode not in (0, 2) or not p.stdout:
        raise ValueError("cannot decompress .Z")
    return p.stdout


def z_write(data: bytes) -> bytes:
    exe = shutil.which("compress") or shutil.which("ncompress")
    if not exe:
        raise ValueError("no compress binary")
    p = subprocess.run([exe, "-c"], input=data, capture_output=True)
    if p.returncode not in (0, 2):
        raise ValueError("compress failed")
    return p.stdout


# ---------------------------------------------------------------- tar
def tar_members(data: bytes):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as t:
        for m in t.getmembers():
            if m.isfile():
                f = t.extractfile(m)
                yield m, f.read() if f else b""
            else:
                yield m, None


def tar_rebuild(data: bytes, fn, path_fn) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tin:
        fmt = tin.format
        with tarfile.open(fileobj=out, mode="w:", format=fmt) as tout:
            for m in tin.getmembers():
                nm = m.__class__.frombuf(m.tobuf(fmt, "utf-8", "surrogateescape"), "utf-8", "surrogateescape") if False else m
                if m.isfile():
                    f = tin.extractfile(m)
                    payload = f.read() if f else b""
                    new_name, new_payload = fn(m.name, payload)
                    nm.name = new_name
                    nm.size = len(new_payload)
                    tout.addfile(nm, io.BytesIO(new_payload))
                else:
                    nm.name = path_fn(m.name)
                    if m.linkname:
                        nm.linkname = path_fn(m.linkname)
                    tout.addfile(nm)
    return out.getvalue()


# ---------------------------------------------------------------- 7z / rar via temp dirs
def seven_members(data: bytes, scratch: str):
    import py7zr
    d = tempfile.mkdtemp(dir=scratch)
    try:
        with py7zr.SevenZipFile(io.BytesIO(data)) as z:
            z.extractall(d)
        for root, dirs, files in os.walk(d):
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    yield os.path.relpath(p, d).replace(os.sep, "/"), fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def seven_write(members: list[tuple[str, bytes]], scratch: str) -> bytes:
    import py7zr
    out = io.BytesIO()
    with py7zr.SevenZipFile(out, "w") as z:
        for name, payload in members:
            z.writestr(payload, name)
    return out.getvalue()


def rar_members(data: bytes, scratch: str):
    d = tempfile.mkdtemp(dir=scratch)
    try:
        src = os.path.join(d, "in.rar")
        with open(src, "wb") as fh:
            fh.write(data)
        outd = os.path.join(d, "x")
        os.makedirs(outd)
        exe = shutil.which("unrar") or shutil.which("unar") or shutil.which("7z")
        if not exe:
            raise ValueError("no rar extractor")
        if exe.endswith("unar"):
            cmd = [exe, "-q", "-o", outd, src]
        elif exe.endswith("7z"):
            cmd = [exe, "x", "-y", "-o" + outd, src]
        else:
            cmd = [exe, "x", "-y", "-inul", src, outd + "/"]
        subprocess.run(cmd, check=True, capture_output=True)
        for root, dirs, files in os.walk(outd):
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    yield os.path.relpath(p, outd).replace(os.sep, "/"), fh.read()
    finally:
        shutil.rmtree(d, ignore_errors=True)
