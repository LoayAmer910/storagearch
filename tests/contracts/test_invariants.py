"""The eight rules (I1-I8) the whole system keeps, checked through the API on the whole corpus."""
import contextlib
import io
import os
import shutil
import sqlite3
import time
import zipfile

import pytest

from core import api, config, events, procs, router, strategies
from support import corpus, fakes
from support.api import item_id_by_name, leftovers, read, tree


KNOWN_GAPS = {}  # I2 violations that exist today - none


def _archive_and_restore(app, path, category="Contract"):
    result = api.archive_file_result(path, category)
    assert result.get("success") and result["reason"] == "OK", result
    item_id = item_id_by_name(os.path.basename(path), "file")
    out = app / f"out_{item_id}"
    restored = api.checkout(item_id, destination=str(out))
    assert restored is not None, f"archived but not restorable: {path}"
    return result, read(restored)


def _plain_zip_size(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(path, "m")
    with zipfile.ZipFile(buf) as z:
        return z.infolist()[0].compress_size


I2_KINDS = ("text.log", "text.csv", "text.json", "text.py", "text.md", "image.png", "image.gif", "image.tiff",
            "image.bmp", "image.jpg", "doc.pdf", "bundle.zip", "bundle.gz", "program.exe", "random.bin",
            "large_text.txt", "office.docx", "db.sqlite", "video.mp4")


def _text(app, name="payload.txt", lines=3000, seed=0):
    path = app / name
    path.write_bytes(corpus.lines(lines, f"row {{i}} {seed} {{w}} {{n}} payload\n", seed=seed))
    return str(path)


def _route_to(monkeypatch, strategy_list):
    monkeypatch.setattr(router, "route", lambda info: list(strategy_list))


def _clean():
    left = leftovers()
    assert left["temp_entries"] == [] and left["children"] == [], left


FAULTS = [fakes.Raising(name="raises"), fakes.Crashing(name="crashes"), fakes.Hanging(name="hangs"),
          fakes.WrongRestore(name="wrong"), fakes.MissingOutput(name="missing")]


@contextlib.contextmanager
def _db():
    """The catalog, opened directly: commits on success like `with connection`, and is always closed."""
    db = sqlite3.connect(config.db_path())
    try:
        with db:
            yield db
    finally:
        db.close()


class _FakeCloudModule:
    def __init__(self, label, calls, fail=False, hang=0.0, returns=None, on_call=None):
        self.label, self.calls, self.fail, self.hang, self.returns, self.on_call = label, calls, fail, hang, returns or {}, on_call

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)

        def call(*args, **kwargs):
            self.calls.append((self.label, attr, args, kwargs))
            if self.on_call:
                self.on_call(self.label, attr, kwargs)
            if self.hang:
                time.sleep(self.hang)
            if self.fail:
                raise RuntimeError(f"cloud {self.label}.{attr} is down")
            return self.returns.get(attr)
        return call


def _install_cloud(monkeypatch, calls, **behaviour):
    monkeypatch.setattr(events, "CLOUD_AVAILABLE", True)
    monkeypatch.setattr(events, "_sender", None)
    for attr in ("_sender_module", "_installation", "_remote_config", "_retention", "_schemas", "_telemetry", "_updates"):
        monkeypatch.setattr(events, attr, _FakeCloudModule(attr, calls, **behaviour))


def _local_outcome(app):
    """Archive a file and a folder, check out, edit, check in, verify: the local, deterministic results."""
    src = _text(app, "a.txt", seed=1)
    folder = app / "f"
    folder.mkdir()
    _text(folder, "b.txt", 1500, seed=2)
    r1 = api.archive_file_result(src, "Docs")
    r2 = api.archive_folder_result(str(folder), "Docs")
    item_id = item_id_by_name("a.txt")
    ws = api.checkout(item_id)
    with open(ws, "a") as f:
        f.write("edit\n")
    ok = api.checkin(item_id)
    back = read(api.checkout(item_id, destination=str(app / "back")))
    statuses = api.verify_all()
    return ({k: r1[k] for k in ("success", "strategy", "compressed_size")},
            {k: r2[k] for k in ("success", "compressed_size")}, ok, back, sorted(statuses["Docs"].values()))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _strings(v)


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


def test_nothing_is_stored_larger_than_a_plain_zip_member(app, samples):
    worse = []
    for sample in samples.values():
        result = api.archive_file_result(sample.path, "Size")
        stored, plain = result["compressed_size"], _plain_zip_size(sample.path)
        if stored > plain or (result["smart_compression_used"] and stored >= plain):
            worse.append((sample.name, result["strategy"], stored, plain))
    assert worse == []


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


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f.name)
def test_a_failing_candidate_falls_back_to_the_next_method(app, monkeypatch, fault):
    _route_to(monkeypatch, [fault, strategies.get("zstd")])
    result = api.archive_file_result(_text(app), "Docs")
    assert result["success"] and result["strategy"] == "zstd", result
    assert read(api.checkout(item_id_by_name("payload.txt"), destination=str(app / "out"))) == read(app / "payload.txt")
    _clean()


def test_when_every_candidate_fails_the_baseline_is_stored(app, monkeypatch):
    _route_to(monkeypatch, FAULTS)
    result = api.archive_file_result(_text(app), "Docs")
    assert result["success"] and not result["smart_compression_used"] and result["strategy"] == api.BASELINE_NAME
    _clean()


def test_a_folder_archive_survives_failing_candidates(app, monkeypatch):
    folder = app / "docs"
    folder.mkdir()
    for i in range(5):
        _text(folder, f"note{i}.txt", 1500, seed=i)
    _route_to(monkeypatch, FAULTS[:2] + [strategies.get("zstd")])
    result = api.archive_folder_result(str(folder), "Docs")
    assert result["success"] and [r["strategy"] for r in result["file_results"]] == ["zstd"] * 5
    assert tree(api.checkout(item_id_by_name("docs", "folder"), destination=str(app / "out"))) == tree(folder)
    _clean()


def test_a_hanging_candidate_is_killed_and_the_archive_completes(app, monkeypatch):
    marker = str(app / "hang.pid")
    _route_to(monkeypatch, [fakes.Hanging(name="hangs", marker=marker), strategies.get("zstd")])
    start = time.monotonic()
    result = api.archive_file_result(_text(app), "Docs")
    assert result["success"] and time.monotonic() - start < fakes.TIMEOUT_S + 10
    assert not procs.alive(int(open(marker).read()))
    _clean()


def test_a_hanging_restore_at_checkout_is_killed_and_reported(app, monkeypatch):
    trigger, marker = str(app / "hang_now"), str(app / "restore.pid")
    fake = fakes.Compressing(name="fake_lzma", trigger=trigger, marker=marker)
    monkeypatch.setitem(strategies.REGISTRY, fake.name, fake)
    _route_to(monkeypatch, [fake])
    result = api.archive_file_result(_text(app), "Docs")
    assert result["success"] and result["strategy"] == "fake_lzma", result
    open(trigger, "w").close()
    start = time.monotonic()
    assert api.checkout(item_id_by_name("payload.txt"), destination=str(app / "out")) is None
    assert time.monotonic() - start < fakes.TIMEOUT_S + 10
    assert not procs.alive(int(open(marker).read()))
    assert not (app / "out" / "payload.txt").exists() and api.get_item(item_id_by_name("payload.txt"))[10] == "ARCHIVED"
    os.remove(trigger)
    assert read(api.checkout(item_id_by_name("payload.txt"), destination=str(app / "out2"))) == read(app / "payload.txt")
    _clean()


def test_archive_checkout_checkin_of_the_corpus_leave_nothing(app):
    folder = app / "corpus"
    corpus.build(str(folder))
    assert api.archive_folder_result(str(folder), "Docs")["success"]
    single = _text(app)
    assert api.archive_file_result(single, "Docs")["success"]
    folder_id, file_id = item_id_by_name("corpus", "folder"), item_id_by_name("payload.txt")
    _clean()
    ws_folder, ws_file = api.checkout(folder_id), api.checkout(file_id)
    assert ws_folder and ws_file
    _clean()
    with open(ws_file, "a") as f:
        f.write("an edit\n")
    assert api.checkin(file_id) and api.checkin(folder_id)
    _clean()
    assert not os.path.exists(ws_file) and not os.path.exists(ws_folder)


def test_failed_operations_leave_nothing(app, monkeypatch):
    _route_to(monkeypatch, FAULTS)
    api.archive_file_result(_text(app), "Docs")
    moving = _text(app, "moving.txt", seed=3)
    real_decide = api.Pipeline.decide

    def decide_then_change(self, path, scanned=None):
        decision = real_decide(self, path, scanned)
        with open(path, "ab") as f:
            f.write(b"changed after the decision\n")
        return decision
    monkeypatch.setattr(api.Pipeline, "decide", decide_then_change)
    result = api.archive_file_result(moving, "Docs")
    assert result["reason"] == "SOURCE_CHANGED" and not result["success"]
    assert api.search("moving.txt") == []
    _clean()


@pytest.mark.parametrize("bad", ["../../escape.txt", "C:/escape.txt", "Y:escape.txt", "a/../../../escape.txt", "CON"])
def test_tampered_stored_paths_are_refused_and_nothing_is_written_outside(app, bad):
    src = _text(app)
    api.archive_file_result(src, "Docs")
    item_id = item_id_by_name("payload.txt")
    with _db() as db:
        db.execute("UPDATE members SET relative_path = ? WHERE item_id = ?", (bad, item_id))
    dest = app / "dest" / "inner"
    dest.mkdir(parents=True)
    assert api.checkout(item_id, destination=str(dest)) is None
    assert not any("escape" in f for _d, _s, fs in os.walk(app) for f in fs)
    _clean()


def test_a_tampered_display_name_does_not_steer_the_checkout(app):
    api.archive_file_result(_text(app), "Docs")
    item_id = item_id_by_name("payload.txt")
    with _db() as db:
        db.execute("UPDATE items SET file_name = ? WHERE id = ?", ("../../escape_item.txt", item_id))
    out = app / "dest" / "inner"
    restored = api.checkout(item_id, destination=str(out))
    assert restored and os.path.dirname(restored) == str(out)
    assert not any("escape_item" in f for _d, _s, fs in os.walk(app) for f in fs)


def test_a_tampered_workspace_path_is_never_deleted_by_checkin(app):
    api.archive_file_result(_text(app), "Docs")
    item_id = item_id_by_name("payload.txt")
    api.checkout(item_id)
    precious = app / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("user data")
    with _db() as db:
        db.execute("UPDATE items SET workspace_path = ? WHERE id = ?", (str(precious).replace("\\", "/"), item_id))
    api.checkin(item_id)
    assert (precious / "keep.txt").read_text() == "user data"


def test_a_failing_cloud_changes_no_local_result(tmp_path, monkeypatch):
    results = {}
    for label in ("no_cloud", "cloud_down"):
        home = tmp_path / label
        home.mkdir()
        monkeypatch.chdir(home)
        calls = []
        if label == "cloud_down":
            _install_cloud(monkeypatch, calls, fail=True)
        api.initialize()
        results[label] = _local_outcome(home)
        events.flush(5)
        if label == "cloud_down":
            assert calls, "no cloud call was made - the test would prove nothing"
            monkeypatch.setattr(events, "CLOUD_AVAILABLE", False)
    assert results["cloud_down"] == results["no_cloud"]


def test_a_hanging_cloud_does_not_delay_a_local_result(app, monkeypatch):
    calls = []
    _install_cloud(monkeypatch, calls, hang=3.0)
    start = time.monotonic()
    assert api.archive_file_result(_text(app), "Docs")["success"]
    assert time.monotonic() - start < 2.5, "the archive waited for the cloud"


def test_events_leave_after_the_commit_and_carry_no_names_or_paths(app, monkeypatch):
    seen, calls = [], []

    def on_call(label, attr, kwargs):
        if attr == "record_operation_event" and kwargs.get("operation_type") == "archive":
            seen.append(len(api.search("secret_name.txt")))
    _install_cloud(monkeypatch, calls, on_call=on_call)
    src = _text(app, "secret_name.txt")
    assert api.archive_file_result(src, "Private Category")["success"]
    events.flush(5)
    assert seen and all(n == 1 for n in seen), f"an archive event left before the item was committed: {seen}"
    private = ("secret_name", str(app), "Private Category", "payload")
    leaked = [(l, a, v) for l, a, args, kw in calls for v in _strings((args, kw)) if any(p.lower() in v.lower() for p in private)]
    assert leaked == [], leaked


def test_remote_config_can_only_narrow_the_choice(app, monkeypatch):
    calls = []
    _install_cloud(monkeypatch, calls, returns={"disabled_strategies_set": set(strategies.REGISTRY) | {"not_a_strategy"}})
    events.disabled_strategies()
    events.flush(5)  # the config arrives on the background lane; the next archive uses it
    result = api.archive_file_result(_text(app), "Docs")
    assert result["success"] and result["strategy"] == api.BASELINE_NAME
    monkeypatch.setattr(events, "_remote_config", _FakeCloudModule("rc", calls, returns={"disabled_strategies_set": 42}))
    events.disabled_strategies()
    events.flush(5)
    assert api.archive_file_result(_text(app, "b.txt", seed=5), "Docs")["success"]  # garbage config is ignored
