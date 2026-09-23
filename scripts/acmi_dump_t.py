"""Dump T= component statistics for piloted objects, to fix index mapping."""
import io
import re
import sys
import zipfile
from collections import defaultdict
from statistics import median

TAIL_RE = re.compile(r",(?=[A-Z][A-Za-z0-9_]*=)")
TS_RE = re.compile(r"^#([0-9.]+)")


def open_stream(path):
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":
        zf = zipfile.ZipFile(path)
        nm = zf.namelist()[0]
        return io.TextIOWrapper(zf.open(nm), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def main():
    path = sys.argv[1]
    samples = defaultdict(lambda: defaultdict(list))
    names = {}
    ts_vals = []
    with open_stream(path) as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line or line.startswith("//"):
                continue
            if line[0] == "#":
                m = TS_RE.match(line)
                if m:
                    try:
                        ts_vals.append(float(m.group(1)))
                    except ValueError:
                        pass
                continue
            if "," not in line:
                continue
            oid, rest = line.split(",", 1)
            if not re.fullmatch(r"[0-9A-Fa-f]+", oid):
                continue
            if not rest.startswith("T="):
                continue
            after = rest[2:]
            ms = list(TAIL_RE.finditer(after))
            if ms:
                c = ms[-1].start()
                tstr, body = after[:c], after[c + 1:]
            else:
                tstr, body = after, ""
            parts = [p.strip() for p in (tstr.split("|") if "|" in tstr else tstr.split(","))]
            m = re.search(r"Name=([^,]+)", body)
            if m:
                names[oid] = m.group(1).strip()
            m = re.search(r"Pilot=([^,]+)", body)
            if m:
                names.setdefault(oid, "?" + m.group(1).strip())
            nm = names.get(oid, oid)
            key = "%s [%d parts]" % (nm, len(parts))
            for i, p in enumerate(parts):
                if p:
                    try:
                        f = float(p)
                    except ValueError:
                        continue
                    bucket = samples[key][i]
                    if len(bucket) < 4000:
                        bucket.append(f)

    if ts_vals:
        ts_vals.sort()
        print("timestamps: n=%d min=%.1f p50=%.1f max=%.1f (max=%.2f h)"
              % (len(ts_vals), ts_vals[0], median(ts_vals), ts_vals[-1], ts_vals[-1] / 3600))
    print()
    for key in sorted(samples):
        print("=== %s ===" % key)
        for i in sorted(samples[key]):
            v = sorted(samples[key][i])
            n = len(v)
            print("   [%d] n=%-6d min=%-12.4f p50=%-12.4f max=%-12.4f"
                  % (i, n, v[0], v[n // 2], v[-1]))
        print()


if __name__ == "__main__":
    main()
