"""core.api - an installation that never used the cloud leaves no cloud queue files on the computer."""
import os

from core import api, events
from support.api import item_id_by_name
from support.files import write_lines_file


def test_working_without_the_cloud_creates_no_queue_files(app):
    assert not api.cloud_is_registered() and not api.cloud_telemetry_enabled()
    src = write_lines_file(app, "notes.txt")
    assert api.archive_file_result(src, "Docs")["success"]
    item_id = item_id_by_name("notes.txt")
    assert api.checkout(item_id) and api.checkin(item_id)
    api.verify_all()
    events.flush(5)
    home = os.environ["STORAGEARCH_CLOUD_HOME"]
    files = sorted(os.listdir(home)) if os.path.isdir(home) else []
    assert [f for f in files if "queue" in f] == [], files
