#!/usr/bin/env python3
"""Download one size-balanced shard of the Machina input.

  python shard.py --manifest manifest.tsv --shard 3 --of 20 --dest input
"""
import argparse, heapq, os, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

ERRS = {}
BASE = os.environ.get("MACHINA_INPUT_BASE", "https://machina-input.joinbuilders.org")


def assign(rows, n):
    heap = [(0, i) for i in range(n)]
    out = [[] for _ in range(n)]
    for r in sorted(rows, key=lambda r: (-r[1], r[0])):
        load, i = heapq.heappop(heap)
        out[i].append(r)
        heapq.heappush(heap, (load + r[1], i))
    return out


def fetch(item, dest):
    path, size, url = item
    dst = os.path.join(dest, path)
    if os.path.exists(dst) and os.path.getsize(dst) == size:
        return 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if size == 0:
        open(dst, "wb").close()
        return 0
    for attempt in range(8):
        try:
            with urllib.request.urlopen(BASE + "/" + urllib.parse.quote(url, safe="/%:@!$&'()*+,;=~-._"), timeout=300) as r, open(dst, "wb") as f:
                while True:
                    b = r.read(1 << 20)
                    if not b:
                        break
                    f.write(b)
            if os.path.getsize(dst) == size:
                return size
        except Exception as e:
            code = getattr(e, "code", None)
            ERRS[str(code or type(e).__name__)] = ERRS.get(str(code or type(e).__name__), 0) + 1
            if code == 404:
                return -1
        time.sleep(2 * (attempt + 1))
    print("failed after retries", file=sys.stderr)
    return -1


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--manifest", default="manifest.tsv")
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--of", type=int, required=True)
    p.add_argument("--dest", default="input")
    p.add_argument("--jobs", type=int, default=16)
    a = p.parse_args()
    rows = []
    with open(a.manifest, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            path, size, _md5, url = line.rstrip("\n").split("\t")
            rows.append((path, int(size), url))
    mine = assign(rows, a.of)[a.shard]
    total = sum(r[1] for r in mine)
    print(f"shard {a.shard}/{a.of}: {len(mine)} files, {total/1e9:.2f} GB", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(a.jobs) as ex:
        res = list(ex.map(lambda r: fetch(r, a.dest), mine))
    bad = sum(1 for x in res if x < 0)
    print(f"downloaded in {time.time()-t0:.0f}s, {bad} failures, errors by kind: {ERRS}", flush=True)
    if bad > len(mine) // 200:
        sys.exit(1)


if __name__ == "__main__":
    main()
