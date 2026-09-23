"""
ACMI format probe — read-only, ASCII-only output.
Usage: python scripts/acmi_probe.py <file.acmi|zip.acmi>
"""
import io
import re
import sys
import zipfile
from collections import Counter, defaultdict

TS_RE = re.compile(r"^#([0-9.]+)")
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^,]*)")


def open_stream(path):
    with open(path, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":
        zf = zipfile.ZipFile(path)
        names = zf.namelist()
        inner = names[0]
        for n in names:
            if n.lower().endswith(".txt"):
                inner = n
                break
        return io.TextIOWrapper(zf.open(inner), encoding="utf-8", errors="replace"), names
    return open(path, "r", encoding="utf-8", errors="replace"), None


def main():
    path = sys.argv[1]
    stream, zipnames = open_stream(path)
    print("container:", "ZIP entries=%s" % zipnames if zipnames else "plain text")

    header = []
    prop_names = Counter()
    obj_types = Counter()
    obj_max_t = defaultdict(float)
    obj_first_line = {}
    event_kinds = Counter()
    event_samples = {}
    t_shapes = Counter()
    alt_by_type = defaultdict(list)
    cs_by_id = {}
    pilot_by_id = {}
    name_by_id = {}
    cur_t = 0.0
    max_t = 0.0
    n_lines = 0
    n_ts = 0

    with stream as fh:
        for raw in fh:
            n_lines += 1
            line = raw.rstrip("\r\n")
            if not line or line.startswith("//"):
                continue
            if line.startswith("#"):
                n_ts += 1
                m = TS_RE.match(line)
                if m:
                    try:
                        cur_t = float(m.group(1))
                        if cur_t > max_t:
                            max_t = cur_t
                    except ValueError:
                        pass
                continue
            if "," not in line:
                if "=" in line and n_lines < 20:
                    header.append(line)
                continue

            oid, rest = line.split(",", 1)
            if not re.fullmatch(r"[0-9A-Fa-f]+", oid):
                continue

            # T= must come first per spec
            tval = None
            body = rest
            if rest.startswith("T="):
                after = rest[2:]
                cut = after.find(",")
                if cut < 0:
                    tval, body = after, ""
                else:
                    tval, body = after[:cut], after[cut + 1:]
            kvs = dict(KV_RE.findall(body))
            for k in kvs:
                prop_names[k] += 1

            if not body:  # pure position update
                if tval is not None:
                    parts = re.split(r"[|,]", tval)
                    t_shapes[len(parts)] += 1
                if oid not in obj_first_line:
                    pass
                cur_t and obj_max_t.__setitem__(oid, max(obj_max_t[oid], cur_t))
                continue

            if oid not in obj_first_line:
                obj_first_line[oid] = line
            if "Type" in kvs:
                obj_types[kvs["Type"]] += 1
            if "CallSign" in kvs:
                cs_by_id[oid] = kvs["CallSign"]
            if "Pilot" in kvs:
                pilot_by_id[oid] = kvs["Pilot"]
            if "Name" in kvs:
                name_by_id[oid] = kvs["Name"]
            if tval is not None:
                parts = re.split(r"[|,]", tval)
                t_shapes[len(parts)] += 1
                if oid in pilot_by_id and len(parts) >= 3 and parts[2]:
                    try:
                        alt_by_type[name_by_id.get(oid, "?")].append(float(parts[2]))
                    except ValueError:
                        pass
            obj_max_t[oid] = max(obj_max_t[oid], cur_t)

            if "Event" in kvs:
                kind = kvs["Event"].split("|")[0]
                event_kinds[kind] += 1
                if kind not in event_samples:
                    event_samples[kind] = line

    print("lines=%d  timestamp_lines=%d  max_t=%.1fs (%.2f h)"
          % (n_lines, n_ts, max_t, max_t / 3600.0))
    print("\nheader:")
    for h in header[:10]:
        print("   ", h)

    print("\nobjects seen: %d" % len(obj_first_line))
    print("\nobject Type counts:")
    for k, c in obj_types.most_common(30):
        print("    %6d  %s" % (c, k))

    print("\nobjects with Pilot= : %d" % len(pilot_by_id))
    for oid, p in pilot_by_id.items():
        print("    id=%-5s Pilot=%-14s CallSign=%-10s Name=%-20s max_t=%.0f"
              % (oid, p, cs_by_id.get(oid, "-"), name_by_id.get(oid, "-"),
                 obj_max_t.get(oid, 0)))

    print("\nall property names:")
    for k, c in prop_names.most_common():
        print("    %8d  %s" % (c, k))

    print("\nT= component-count distribution:")
    for k, c in t_shapes.most_common():
        print("    %8d  %d components" % (c, k))

    if alt_by_type:
        print("\ncomponent[2] stats for PILOTED objects (candidate altitude):")
        for nm, vals in alt_by_type.items():
            vals.sort()
            n = len(vals)
            print("    %-22s n=%-7d min=%.1f p50=%.1f max=%.1f  frac<5m=%.1f%%"
                  % (nm, n, vals[0], vals[n // 2], vals[-1],
                     100.0 * sum(1 for v in vals if v < 5.0) / n))

    print("\nEvent kinds (first token):")
    if not event_kinds:
        print("    NONE FOUND")
    for k, c in event_kinds.most_common(30):
        print("    %8d  %s" % (c, k))
        print("            %s" % event_samples[k][:240])


if __name__ == "__main__":
    main()
