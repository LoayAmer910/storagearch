"""Step 7e - folders of many small files. A file that was just written costs Windows ~14 ms the first time it
is opened again (it is scanned), so small files are tried and restored without temporary files: a pure
codec's candidates run in memory (and must give exactly the bytes the file form gives), the winner's bytes
go straight into the archive, and a restore decodes straight from the archive into the file - in parallel.
"""
import os
import random
import tempfile
import zipfile

import pytest

import corpus
from api_support import item_id_by_name, leftovers, read, tree
from core import api, executor as executor_module, store, strategies
from core.executor import Executor
from core.pipeline import Pipeline
from core.scope import OperationScope
from core.strategies.base import CHUNK, StrategyError, StreamingStrategy

STREAMING = [s for s in strategies.REGISTRY.values() if isinstance(s, StreamingStrategy)]
TEXT_CODECS = [strategies.get(n) for n in ("brotli", "zstd", "lzma")]


@pytest.fixture
def scope(tmp_path):
    with OperationScope(temp_root=str(tmp_path / "scopes")) as s:
        yield s


def _text(size, seed=1):
    data = corpus.lines(size // 20 + 10, f"row {{i}} {seed} {{w}} {{n}}\n", seed=seed)
    return data[:size]


def _files_under(root):
    return [os.path.join(d, f) for d, _s, fs in os.walk(root) for f in fs]


def _code_folder(root, count=60, seed=0):
    rnd = random.Random(seed)
    for i in range(count):
        path = os.path.join(root, f"pkg{i % 4}", f"mod{i}.py")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(_text(rnd.randint(5_000, 120_000), seed=seed * 1000 + i))
    return root


@pytest.mark.parametrize("strategy", STREAMING, ids=lambda s: s.name)
@pytest.mark.parametrize("size", [0, 5, 4096, 300_000, CHUNK])
def test_a_codec_gives_the_same_stored_bytes_from_memory_as_from_a_file(strategy, size, tmp_path):
    data = _text(size)
    src = tmp_path / "src"
    src.write_bytes(data)
    work = tmp_path / "w"
    work.mkdir()
    from_file = read(strategy.compress(str(src), str(work)))
    from_memory = b"".join(strategy.encode([data] if data else [], len(data)))
    assert from_memory == from_file
    assert b"".join(strategy.decode([from_memory])) == data


@pytest.mark.parametrize("strategy", STREAMING, ids=lambda s: s.name)
def test_a_codec_refuses_a_cut_or_padded_stream_in_memory(strategy):
    stored = b"".join(strategy.encode([_text(50_000)], 50_000))
    for bad in (stored[:-7], stored + b"extra bytes"):
        with pytest.raises(StrategyError):
            b"".join(strategy.decode([bad]))


def test_a_small_file_is_decided_in_memory_without_a_temporary_file(scope, tmp_path):
    src = tmp_path / "small.txt"
    src.write_bytes(_text(200_000))
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src))
    assert decision.smart and decision.stored_path is None
    assert len(decision.stored_bytes) == decision.stored_size
    assert {o.where for o in decision.candidates} == {"memory"}
    assert all(o.stored_bytes is None for o in decision.candidates)  # only the winner's bytes are kept
    assert _files_under(scope.root) == []
    winner = strategies.get(decision.method)
    assert b"".join(winner.decode([decision.stored_bytes])) == src.read_bytes()


def test_a_file_larger_than_one_chunk_is_still_tried_through_files(scope, tmp_path):
    src = tmp_path / "big.txt"
    src.write_bytes(_text(CHUNK + 50_000))
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src))
    assert decision.smart and decision.stored_bytes is None
    assert os.path.getsize(decision.stored_path) == decision.stored_size
    assert "memory" not in {o.where for o in decision.candidates}


def test_past_the_memory_budget_a_winner_goes_to_a_file(scope, tmp_path, monkeypatch):
    monkeypatch.setattr(executor_module, "MEMORY_BUDGET_BYTES", 10)
    src = tmp_path / "small.txt"
    src.write_bytes(_text(200_000))
    decision = Pipeline(Executor(scope), lambda info: TEXT_CODECS).decide(str(src))
    assert decision.smart and decision.stored_bytes is None
    assert os.path.getsize(decision.stored_path) == decision.stored_size
    assert b"".join(strategies.get(decision.method).decode([read(decision.stored_path)])) == src.read_bytes()


def test_a_folder_of_small_files_round_trips_and_stores_what_the_file_form_gives(app, tmp_path):
    folder = _code_folder(str(app / "proj"))
    assert api.archive_folder_result(folder, "Docs")["success"]
    item_id = item_id_by_name("proj", "folder")
    members = [m for m in api._cat().members_of_item(item_id) if m["kind"] == "file"]
    smart = [m for m in members if m["method"] not in store.BASELINE_METHODS]
    assert len(smart) > len(members) // 2
    zip_path = api.list_archives()[0][3]
    with zipfile.ZipFile(zip_path) as z:
        for m in smart:
            src = os.path.join(app, *m["relative_path"].split("/"))
            work = tempfile.mkdtemp(dir=tmp_path)
            assert z.read(m["path_in_archive"]) == read(strategies.get(m["method"]).compress(src, work)), m["relative_path"]
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == tree(app / "proj")
    assert set(api.verify_category("Docs").values()) == {"VERIFIED"}
    assert leftovers()["temp_files"] == []


def test_restoring_small_files_makes_no_temporary_files(app, monkeypatch):
    folder = _code_folder(str(app / "proj"), count=30, seed=4)
    api.archive_folder_result(folder, "Docs")
    item_id = item_id_by_name("proj", "folder")
    made = []
    real = store.tempfile.mkdtemp
    monkeypatch.setattr(store.tempfile, "mkdtemp",  # the module is shared: count only a per-file restore folder
                        lambda *a, **k: made.append(k.get("prefix")) or real(*a, **k))
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == tree(app / "proj")
    assert "restore_" not in made


def test_a_member_that_fails_to_decode_fails_the_restore_and_leaves_nothing_behind(app, monkeypatch):
    folder = _code_folder(str(app / "proj"), count=40, seed=6)
    api.archive_folder_result(folder, "Docs")
    item_id = item_id_by_name("proj", "folder")
    smart = [m for m in api._cat().members_of_item(item_id) if m["kind"] == "file" and m["method"] not in store.BASELINE_METHODS]
    victim = strategies.get(smart[len(smart) // 2]["method"])

    def broken(chunks):
        yield b"half of a file"
        raise StrategyError("damaged stream")
    monkeypatch.setattr(victim, "decode", broken)
    assert api.checkout(item_id) is None
    workspace = api.get_storage_locations()["workspace_root"] or os.path.join(api.data_root(), "workspace")
    assert not [p for p in _files_under(app) if p.endswith(".restoring")]
    assert api.get_item(item_id)[10] == "ARCHIVED"  # not marked as checked out
    assert not os.path.exists(os.path.join(workspace, api._cat().item_uid(item_id), "proj"))


def test_parallel_restore_keeps_every_file_and_folder(app):
    folder = app / "deep"
    for i in range(200):
        path = folder / f"a{i % 5}" / f"b{i % 7}" / f"f{i}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_text(random.Random(i).randint(0, 30_000), seed=i))
    (folder / "empty" / "inner").mkdir(parents=True)
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    item_id = item_id_by_name("deep", "folder")
    assert tree(api.checkout(item_id, destination=str(app / "back"))) == tree(folder)
