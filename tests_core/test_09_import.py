"""Step 9 - importing the old version's data. The old engine (run in its own process, as the packaged app
ran it) archives the corpus; the importer rebuilds every item without the old code, checks it against the
old catalog's hash and archives it with the new core. Every item then restores from the new core exactly
as the original; a damaged old item is reported and skipped while the rest import; the old data is never
modified."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile

import pytest

import corpus
from api_support import read, tree
from core import api, config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import import_old_data  # noqa: E402

# The old version is not part of this repository: point STORAGEARCH_OLD_ENGINE_DIR at a checkout of it (the
# folder that holds its beta9_src) to run these tests; without it they are skipped.
OLD_ENGINE = os.environ.get("STORAGEARCH_OLD_ENGINE_DIR")
pytestmark = pytest.mark.skipif(not (OLD_ENGINE and os.path.isdir(os.path.join(OLD_ENGINE, "beta9_src"))),
                                reason="set STORAGEARCH_OLD_ENGINE_DIR to a checkout of the old version")

_OLD = r'''
import os, sys, json
repo, data_dir, files, folder = sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4]
b9 = os.path.join(repo, "beta9_src")
for p in (os.path.join(b9, "experiments", "images", "src"), os.path.join(b9, "experiments", "smart_selector", "src"),
          os.path.join(b9, "experiments", "video", "src"), os.path.join(b9, "experiments", "text_filters", "src"), b9):
    sys.path.insert(0, p)
os.makedirs(data_dir, exist_ok=True)
os.chdir(data_dir)
import main
from concurrency_overlay.integration_layer import activate_beta12
activate_beta12()
main.initialize()
main.create_category("Old Docs", "from the old version", "#123456", 50_000_000)
used = {}
for path in files:
    r = main.archive_file_result(path, "Old Docs")
    used[os.path.basename(path)] = r.get("strategy")
r = main.archive_folder_result(folder, "Old Docs")
print(json.dumps({"files": used, "folder": r.get("success")}))
'''


def _snapshot(root):
    out = {}
    for base, _d, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, root)] = hashlib.sha256(read(p)).hexdigest()
    return out


@pytest.fixture(scope="module")
def old_installation(tmp_path_factory):
    root = tmp_path_factory.mktemp("old")
    samples = corpus.build(str(root / "sources"))
    files = [s.path for s in samples.values() if s.name not in ("empty.txt",)]
    folder = root / "sources_folder"
    shutil.copytree(root / "sources", folder)
    done = subprocess.run([sys.executable, "-c", _OLD, OLD_ENGINE, str(root / "oldhome"), json.dumps(files), str(folder)],
                          capture_output=True, text=True, env=dict(os.environ))
    assert done.returncode == 0, done.stderr[-3000:]
    used = json.loads(done.stdout.strip().splitlines()[-1])
    return {"root": root, "data": root / "oldhome" / "SmartArchiveData", "files": files, "folder": str(folder),
            "strategies": set(used["files"].values())}


def test_everything_the_old_version_stored_comes_back_from_the_new_core(old_installation, tmp_path, monkeypatch):
    before = _snapshot(old_installation["data"])
    assert len(old_installation["strategies"] - {"baseline_storagearch_zip", None}) >= 3, old_installation["strategies"]
    report = import_old_data.import_old(str(old_installation["data"]), str(tmp_path / "newhome"))
    assert report["damaged"] == [], report["damaged"]
    assert _snapshot(old_installation["data"]) == before, "the importer modified the old data"
    assert {c[1] for c in api.list_categories()} == {"Old Docs"}
    assert api.list_categories()[0][2:5] == ("from the old version", "#123456", 50_000_000)
    for i, path in enumerate(old_installation["files"]):
        name = os.path.basename(path)
        item = next(r for r in api.search(name) if r[1] == name and r[2] == "file")
        restored = api.checkout(item[0], destination=str(tmp_path / f"o{i}"))
        assert read(restored) == read(path), name
    folder_name = os.path.basename(old_installation["folder"])
    item = next(r for r in api.search(folder_name) if r[2] == "folder")
    assert tree(api.checkout(item[0], destination=str(tmp_path / "of"))) == tree(old_installation["folder"])


def test_a_damaged_old_item_is_reported_and_the_rest_import(old_installation, tmp_path):
    copy = tmp_path / "old_copy"
    shutil.copytree(old_installation["data"], copy)
    zip_path = next(os.path.join(d, f) for d, _s, fs in os.walk(copy / "archives") for f in fs if f.endswith(".zip"))
    with zipfile.ZipFile(zip_path) as z:
        victim = max((i for i in z.infolist() if not i.is_dir()), key=lambda i: i.compress_size)
    data = bytearray(read(zip_path))
    at = victim.header_offset + 30 + len(victim.filename.encode()) + len(victim.extra) + victim.compress_size // 2
    data[at] ^= 0xFF
    open(zip_path, "wb").write(bytes(data))
    report = import_old_data.import_old(str(copy), str(tmp_path / "newhome"))
    assert len(report["damaged"]) >= 1
    assert len(report["imported"]) + len(report["duplicates"]) >= len(old_installation["files"]) - 1
    assert set(api.verify_all()["Old Docs"].values()) == {"VERIFIED"}


def test_the_new_data_may_not_live_inside_the_old(old_installation, tmp_path):
    copy = tmp_path / "old_copy"
    shutil.copytree(old_installation["data"], copy)
    target = copy / "inside"
    target.mkdir()
    (target / config.CONFIG_FILE).write_text(json.dumps({"data_root": str(copy / "nested_new")}), encoding="utf-8")
    with pytest.raises(SystemExit):
        import_old_data.import_old(str(copy), str(target))
