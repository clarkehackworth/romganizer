"""Find content-identical files that carry different names.

Three passes, each only touching what the one before could not rule out:
size, then the first 4 MB, then a full md5. Two distinct dumps of equal length
practically always diverge inside the first few MB, so the full read is reserved
for the handful that really are copies.
"""
import collections, hashlib, os, pickle, sys, time
from pathlib import Path

PREFIX = 4 << 20
ARCH = ('.7z', '.zip', '.rar')
# MAME device sets are byte-identical on purpose and are looked up by zip name;
# reporting them as duplicates would invite someone to break their MAME install.
INTENTIONAL = ('MAME (bios-devices)',)

def digest(path, limit=None):
    h = hashlib.md5()
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
                if limit and f.tell() >= limit:
                    break
    except OSError:
        return None
    return h.hexdigest()

def narrow(groups, limit, label):
    """Split each group by digest; keep only the sub-groups still colliding."""
    out, done, t0 = [], 0, time.monotonic()
    for files in groups:
        by = collections.defaultdict(list)
        for f in files:
            by[digest(f, limit)].append(f)
            done += 1
            if done % 200 == 0:
                print(f"  {label}: {done} files, {time.monotonic()-t0:.0f}s", file=sys.stderr)
        out += [v for k, v in by.items() if k and len(v) > 1]
    return out

def main(cand_pkl=None, out_pkl=None):
    if cand_pkl is None:
        try:
            cand_pkl, out_pkl = sys.argv[1], sys.argv[2]
        except IndexError:
            print("usage: find_dups.py <candidates.pkl> <duplicates_out.pkl>",
                  file=sys.stderr)
            sys.exit(1)
    cand = pickle.load(open(cand_pkl, 'rb'))
    groups = [[f for f in v if Path(f).suffix.lower() not in ARCH
               and not any(m in f for m in INTENTIONAL)] for v in cand.values()]
    groups = [g for g in groups if len(g) > 1]
    print(f"same-size groups: {len(groups)}  files: {sum(map(len, groups))}")
    groups = narrow(groups, PREFIX, "prefix")
    print(f"survived first {PREFIX >> 20} MB: {len(groups)} groups, {sum(map(len, groups))} files")
    groups = narrow(groups, None, "full")
    dups = [g for g in groups if len({Path(f).name for f in g}) > 1]
    pickle.dump(dups, open(out_pkl, 'wb'))
    print(f"\nidentical content, differing names: {len(dups)} groups")
    for g in sorted(dups, key=lambda g: -os.stat(g[0]).st_size * (len(g) - 1)):
        sz = os.stat(g[0]).st_size
        print(f"\n  {sz/2**20:9.1f} MB x{len(g)}  ({sz*(len(g)-1)/2**30:.2f} GB reclaimable)")
        for f in sorted(g):
            print(f"      {f.replace('/mnt/nfs/Downloads/Software/games/rom_dumps/','')}")

if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])
