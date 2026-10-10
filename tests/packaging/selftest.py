"""Self-test of a packaged build. PyInstaller freezes this file the same way the app is frozen, and the release
workflow runs the result: it archives sample files and a folder, checks every one out and compares it with the
original byte for byte, edits one and checks it back in, and verifies every archive. Exit code 0 means all passed.

    selftest.exe WORK_DIR
"""
import hashlib
import multiprocessing
import os
import random
import struct
import sys


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _bmp(width, height):
    """A 24-bit bitmap built by hand, so the JPEG XL strategy has something to work on."""
    row = (width * 3 + 3) & ~3
    pixels = bytearray()
    for y in range(height):
        line = bytearray()
        for x in range(width):
            line += bytes(((x * 3) % 256, (y * 5) % 256, ((x + y) * 2) % 256))
        pixels += line + bytes(row - len(line))
    header = struct.pack("<2sIHHI", b"BM", 54 + len(pixels), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return header + info + bytes(pixels)


def _samples(root):
    rng = random.Random(1)
    words = ["alpha", "beta", "gamma", "archive", "value", "error", "info", "request"]
    os.makedirs(os.path.join(root, "folder"))
    files = {
        "log.txt": "\n".join(" ".join(rng.choice(words) for _ in range(12)) for _ in range(30000)).encode(),
        "data.json": ("[" + ",".join('{"id":%d,"name":"%s"}' % (i, rng.choice(words)) for i in range(20000)) + "]").encode(),
        "picture.bmp": _bmp(320, 240),
        "random.bin": rng.randbytes(200_000),
        "empty.txt": b"",
    }
    with open(sys.executable, "rb") as f:  # a real program, for the executable strategy
        files["program.exe"] = f.read(3_000_000)
    for name, data in files.items():
        with open(os.path.join(root, name), "wb") as f:
            f.write(data)
    for i in range(4):
        with open(os.path.join(root, "folder", f"module{i}.py"), "w") as f:
            f.write(f"def function_{i}(x):\n    return x * {i}\n" * 400)
    return sorted(files), os.path.join(root, "folder")


def main():
    multiprocessing.freeze_support()
    work = os.path.abspath(sys.argv[1])
    os.makedirs(work, exist_ok=True)
    os.chdir(work)  # the app keeps its data in the folder it is started in
    from core import api, tools

    print("frozen:", getattr(sys, "frozen", False), "| JPEG XL tools:", tools.available("cjxl", "djxl")[0])
    source = os.path.join(work, "source")
    names, folder = _samples(source)
    api.initialize()
    failures = []

    def check(label, ok, detail=""):
        print(("ok   " if ok else "FAIL ") + label, detail)
        if not ok:
            failures.append(label)

    check("create a category", api.create_category("Self test") is not None)
    for name in names:
        result = api.archive_file_result(os.path.join(source, name), "Self test")
        check(f"archive {name}", result["success"],
              f"{result.get('strategy')} {result.get('original_size')} -> {result.get('compressed_size')}")
    check("archive a folder", api.archive_folder_result(folder, "Self test")["success"])
    again = api.archive_file_result(os.path.join(source, "log.txt"), "Self test")
    check("the same content is not stored twice", again.get("reason") == "DUPLICATE", again.get("reason"))

    for row in api.search(""):
        item_id, name, kind = row[0], row[1], row[2]
        restored = api.checkout(item_id, destination=os.path.join(work, "out", str(item_id)))
        if not restored:
            check(f"check out {name}", False)
        elif kind == "file":
            check(f"check out {name}", _sha(restored) == _sha(os.path.join(source, name)))
        else:
            same = all(_sha(os.path.join(restored, f)) == _sha(os.path.join(folder, f)) for f in os.listdir(folder))
            check(f"check out {name}", same and sorted(os.listdir(restored)) == sorted(os.listdir(folder)))

    log_id = next(row[0] for row in api.search("log.txt"))
    edited_path = os.path.join(work, "out", str(log_id), "log.txt")
    with open(edited_path, "a") as f:
        f.write("one more line\n")
    edited = _sha(edited_path)
    check("check in an edited file", api.checkin_result(log_id)["success"])
    back = api.checkout(log_id, destination=os.path.join(work, "out_again"))
    check("the edited version comes back", bool(back) and _sha(back) == edited)

    statuses = [status for category in api.verify_all().values() for status in category.values()]
    check("every archive verifies", bool(statuses) and set(statuses) == {"VERIFIED"}, str(statuses))
    api.shutdown_cloud()
    print("RESULT:", "PASS" if not failures else f"FAIL {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
