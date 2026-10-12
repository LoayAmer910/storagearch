"""Differential test: the old engine and the new core archive the same real folders; both must restore every
file byte-identical, and the new core must be at least as small (and not much slower) per file type.

Each engine runs in its own process with its own data folder, so the new core never shares a module with
the old one. The old engine runs the way the packaged app ran it (concurrency overlay active).

    python tests/differential/run_differential.py OUT_DIR FOLDER [FOLDER ...]

Writes OUT_DIR/report.json and prints a summary. Nothing is written into the source folders.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
# The old version is not part of this repository: STORAGEARCH_OLD_ENGINE_DIR points at a checkout of it.
OLD_ENGINE = os.environ.get("STORAGEARCH_OLD_ENGINE_DIR", "")

_ENGINE = r'''
import hashlib, json, os, sys, time
engine, data_dir, folders = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
repo, old_repo = {repo!r}, {old_repo!r}
os.makedirs(data_dir, exist_ok=True)
os.chdir(data_dir)
if engine == "old":
    b9 = os.path.join(old_repo, "beta9_src")
    for p in (os.path.join(b9, "experiments", "images", "src"), os.path.join(b9, "experiments", "smart_selector", "src"),
              os.path.join(b9, "experiments", "video", "src"), os.path.join(b9, "experiments", "text_filters", "src"), b9):
        sys.path.insert(0, p)
    import main as api
    from concurrency_overlay.integration_layer import activate_beta12
    activate_beta12()
else:
    sys.path.insert(0, repo)
    from core import api

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()

api.initialize()
report = []
for i, folder in enumerate(folders):
    start = time.perf_counter()
    result = api.archive_folder_result(folder, "Diff")
    seconds = time.perf_counter() - start
    entry = {{"folder": folder, "success": result.get("success"), "reason": result.get("reason"), "seconds": seconds,
              "original": result.get("original_size"), "stored": result.get("compressed_size"),
              "files": [{{k: r.get(k) for k in ("file_type", "strategy", "original_size", "stored_size", "processing_ms")}}
                        for r in result.get("file_results") or []]}}
    if result.get("success"):
        name = os.path.basename(os.path.normpath(folder))
        item_id = next(r[0] for r in api.search(name) if r[1] == name and r[2] == "folder")
        out = os.path.join(data_dir, f"restored_{{i}}")
        os.makedirs(out)
        start = time.perf_counter()
        restored = api.checkout(item_id, destination=out)
        entry["restore_seconds"] = time.perf_counter() - start
        mismatched, missing, count = [], [], 0
        for base, _dirs, files in os.walk(folder):
            for f in files:
                src = os.path.join(base, f)
                rel = os.path.relpath(src, folder)
                dst = os.path.join(restored, rel) if restored else None
                count += 1
                if not dst or not os.path.isfile(dst):
                    missing.append(rel)
                elif sha(dst) != sha(src):
                    mismatched.append(rel)
        entry.update(checked=count, missing=missing[:20], missing_count=len(missing), mismatched=mismatched[:20],
                     mismatched_count=len(mismatched))
    report.append(entry)
    print(json.dumps({{"engine": engine, "folder": folder, "seconds": round(seconds, 1), "success": entry["success"]}}), flush=True)
json.dump(report, open(os.path.join(data_dir, "engine_report.json"), "w"), indent=1)
'''


def run_engine(engine, out_dir, folders):
    data_dir = os.path.join(out_dir, engine)
    if engine == "old" and not os.path.isdir(os.path.join(OLD_ENGINE, "beta9_src")):
        raise SystemExit("set STORAGEARCH_OLD_ENGINE_DIR to a checkout of the old version")
    script = _ENGINE.format(repo=REPO, old_repo=OLD_ENGINE)
    env = dict(os.environ)
    subprocess.run([sys.executable, "-c", script, engine, data_dir, json.dumps(folders)], check=True, env=env)
    with open(os.path.join(data_dir, "engine_report.json"), encoding="utf-8") as f:
        return json.load(f)


def by_type(entry):
    totals = {}
    for r in entry["files"]:
        t = totals.setdefault(r["file_type"] or "unknown", {"files": 0, "original": 0, "stored": 0})
        t["files"] += 1
        t["original"] += r["original_size"] or 0
        t["stored"] += r["stored_size"] or 0
    return totals


def main():
    out_dir, folders = sys.argv[1], [os.path.abspath(f) for f in sys.argv[2:]]
    os.makedirs(out_dir, exist_ok=True)
    reports = {engine: run_engine(engine, out_dir, folders) for engine in ("old", "new")}
    summary = []
    for old, new in zip(reports["old"], reports["new"]):
        types_old, types_new = by_type(old), by_type(new)
        worse_types = {t: (types_old[t]["stored"], types_new.get(t, {}).get("stored"))
                       for t in types_old if types_new.get(t, {}).get("stored", 0) > types_old[t]["stored"] * 1.005}
        summary.append({
            "folder": os.path.basename(old["folder"]),
            "old": {k: old.get(k) for k in ("success", "original", "stored", "seconds", "restore_seconds", "mismatched_count", "missing_count")},
            "new": {k: new.get(k) for k in ("success", "original", "stored", "seconds", "restore_seconds", "mismatched_count", "missing_count")},
            "types_where_new_is_larger": worse_types,
            "types": {t: {"old": types_old.get(t), "new": types_new.get(t)} for t in sorted(set(types_old) | set(types_new))},
        })
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    for s in summary:
        o, n = s["old"], s["new"]
        print(f"{s['folder']}: size old {o['stored']} new {n['stored']} | time old {o['seconds']:.1f}s new {n['seconds']:.1f}s | "
              f"restore mismatches old {o['mismatched_count']}/{o['missing_count']} new {n['mismatched_count']}/{n['missing_count']} | "
              f"worse types {list(s['types_where_new_is_larger'])}")


if __name__ == "__main__":
    import ctypes
    if os.name == "nt":
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # no sleep while measuring
    main()
