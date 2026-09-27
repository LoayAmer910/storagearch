"""Step 7b - contracts I1-I3 through the API, on the whole corpus.

I1: every file and every folder comes back byte-identical (or the archive is refused - never a success
    followed by different bytes); verification passes for every archive.
I2: the same bytes under another name get the same stored method and size. No known gaps: the list the
    old engine needed (20 cases) is empty here.
I3: nothing is stored larger than a plain ZIP member of the same file; a smart method only when smaller.
"""
import io
import os
import shutil
import zipfile

import pytest

import corpus
from api_support import item_id_by_name, read, tree
from core import api

KNOWN_GAPS = {}  # I2 violations that exist today - none


@pytest.fixture(scope="module")
def samples(tmp_path_factory):
    return corpus.build(str(tmp_path_factory.mktemp("corpus")))


def _archive_and_restore(app, path, category="Contract"):
    result = api.archive_file_result(path, category)
    assert result.get("success") and result["reason"] == "OK", result
    item_id = item_id_by_name(os.path.basename(path), "file")
    out = app / f"out_{item_id}"
    restored = api.checkout(item_id, destination=str(out))
    assert restored is not None, f"archived but not restorable: {path}"
    return result, read(restored)


def test_every_corpus_file_restores_identically(app, samples):
    wrong = []
    for sample in samples.values():
        result, restored = _archive_and_restore(app, sample.path)
        if restored != read(sample.path):
            wrong.append((sample.name, result["strategy"]))
    assert wrong == []
    assert all(s == "VERIFIED" for s in api.verify_category("Contract").values())


def test_the_whole_corpus_as_a_folder_restores_identically(app, samples):
    folder = app / "corpus_copy"
    shutil.copytree(os.path.dirname(next(iter(samples.values())).path), folder)
    (folder / "nested" / "deeper").mkdir(parents=True)
    (folder / "nested" / "empty").mkdir()
    shutil.copyfile(samples["text.log"].path, folder / "nested" / "deeper" / "copy.log")
    result = api.archive_folder_result(str(folder), "Contract")
    assert result["success"] and result["reason"] == "OK", result
    out = app / "restored"
    restored = api.checkout(item_id_by_name("corpus_copy", "folder"), destination=str(out))
    assert tree(restored) == tree(folder)
    assert api.verify_all() == {"Contract": {os.path.basename(api.list_archives()[0][3]): "VERIFIED"}}


def _plain_zip_size(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, "m")
    with zipfile.ZipFile(buf) as z:
        return z.infolist()[0].compress_size


def test_nothing_is_stored_larger_than_a_plain_zip_member(app, samples):
    worse = []
    for sample in samples.values():
        result = api.archive_file_result(sample.path, "Size")
        stored, plain = result["compressed_size"], _plain_zip_size(sample.path)
        if stored > plain or (result["smart_compression_used"] and stored >= plain):
            worse.append((sample.name, result["strategy"], stored, plain))
    assert worse == []


I2_KINDS = ("text.log", "text.csv", "text.json", "text.py", "text.md", "image.png", "image.gif", "image.tiff",
            "image.bmp", "image.jpg", "doc.pdf", "bundle.zip", "bundle.gz", "program.exe", "random.bin",
            "large_text.txt", "office.docx", "db.sqlite", "video.mp4")


@pytest.mark.parametrize("kind", I2_KINDS)
def test_same_bytes_under_another_name_are_stored_the_same_way(kind, samples, tmp_path, monkeypatch):
    if kind in KNOWN_GAPS:
        pytest.xfail(KNOWN_GAPS[kind])
    outcomes = {}
    for ext in ("own", "txt", "jpg", "dat", "noext"):
        home = tmp_path / ext
        home.mkdir()
        monkeypatch.chdir(home)
        api.initialize()
        own = os.path.splitext(kind)[1]
        name = "sample" + (own if ext == "own" else "" if ext == "noext" else f".{ext}")
        shutil.copyfile(samples[kind].path, home / name)
        result, restored = _archive_and_restore(home, str(home / name))
        assert restored == read(samples[kind].path)
        outcomes[ext] = (result["strategy"], result["compressed_size"])
    assert len(set(outcomes.values())) == 1, f"{kind}: stored differently by name: {outcomes}"
