"""Step 7c - contracts I4-I8 through the API.

I4: failing candidates (raise, crash, hang, wrong bytes, no output) never fail an archive.
I5: a hanging candidate, and a hanging restore at checkout, are killed within their timeout.
I6: after every operation - also failed ones - no temp folder and no worker process is left.
I7: tampered catalog rows (stored paths, display names, workspace paths) never make the core write or
    delete outside where it should; other-drive names are refused cleanly instead of crashing.
I8: a cloud package that fails everywhere, or hangs, changes nothing local; events leave only after the
    commit and carry no names or paths; remote config can only narrow the choice.
"""
import os
import sqlite3
import time

import pytest

import corpus
import fakes
from api_support import item_id_by_name, leftovers, read, tree
from core import api, candidate, config, events, procs, router, strategies


def _text(app, name="payload.txt", lines=3000, seed=0):
    path = app / name
    path.write_bytes(corpus.lines(lines, f"row {{i}} {seed} {{w}} {{n}} payload\n", seed=seed))
    return str(path)


def _route_to(monkeypatch, strategy_list):
    monkeypatch.setattr(router, "route", lambda info: list(strategy_list))


def _clean():
    left = leftovers()
    assert left["temp_entries"] == [] and left["children"] == [], left


# --- I4 -------------------------------------------------------------------------------------------------

FAULTS = [fakes.Raising(name="raises"), fakes.Crashing(name="crashes"), fakes.Hanging(name="hangs"),
          fakes.WrongRestore(name="wrong"), fakes.MissingOutput(name="missing")]


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


# --- I5 -------------------------------------------------------------------------------------------------

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


# --- I6 -------------------------------------------------------------------------------------------------

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


# --- I7 -------------------------------------------------------------------------------------------------

def _db():
    return sqlite3.connect(config.db_path())


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


# --- I8 -------------------------------------------------------------------------------------------------

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


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _strings(v)
