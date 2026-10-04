"""Fake strategies for fault injection. Module level, so a spawned worker can import them by name."""
import os
import shutil
import subprocess
import sys
import time

from core.strategies.base import Strategy, StrategyError, StrategySpec


TIMEOUT_S = 3.0


def spec(name, in_process=False, max_bytes=1 << 40, timeout_s=TIMEOUT_S):
    return StrategySpec(name=name, version=1, weight="light", in_process_safe=in_process,
                        max_in_process_bytes=max_bytes, base_timeout_s=timeout_s, seconds_per_mib=0.0)


class Fake(Strategy):
    """Stores a copy. `marker`: a file that receives the pid of the process the fault ran in."""

    def __init__(self, name="fake_copy", in_process=False, marker=None, max_bytes=1 << 40, timeout_s=TIMEOUT_S):
        self.spec = spec(name, in_process, max_bytes, timeout_s)
        self.marker = marker

    def note(self, text=None):
        if self.marker:
            with open(self.marker, "w") as f:
                f.write(str(text if text is not None else os.getpid()))

    def compress(self, src_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "stored.bin")
        shutil.copyfile(src_path, out)
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "restored.bin")
        shutil.copyfile(stored_path, out)
        return out


class Raising(Fake):
    def compress(self, src_path, work_dir, time_limit_s=None):
        self.note()
        raise StrategyError("injected failure")


class Crashing(Fake):
    def compress(self, src_path, work_dir, time_limit_s=None):
        self.note()
        os._exit(3)


class Hanging(Fake):
    def compress(self, src_path, work_dir, time_limit_s=None):
        self.note()
        time.sleep(600)
        return super().compress(src_path, work_dir)


class HangingWithChild(Fake):
    """Starts a child process (like a worker launching cjxl) and hangs; the marker gets the child's pid."""

    def compress(self, src_path, work_dir, time_limit_s=None):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"],
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.note(child.pid)
        time.sleep(600)
        return super().compress(src_path, work_dir)


class WrongRestore(Fake):
    def restore(self, stored_path, work_dir, time_limit_s=None):
        self.note()
        out = os.path.join(work_dir, "restored.bin")
        with open(out, "wb") as f:
            f.write(b"not the original")
        return out


class MissingOutput(Fake):
    def compress(self, src_path, work_dir, time_limit_s=None):
        self.note()
        return os.path.join(work_dir, "never_written.bin")


class OutsideWorkDir(Fake):
    def compress(self, src_path, work_dir, time_limit_s=None):
        self.note()
        return src_path  # "stored" = the source itself, outside the work dir


class Slow(Fake):
    def __init__(self, seconds, **kwargs):
        super().__init__(**kwargs)
        self.seconds = seconds

    def compress(self, src_path, work_dir, time_limit_s=None):
        time.sleep(self.seconds)
        return super().compress(src_path, work_dir)


class Tagged(Fake):
    """Stores the source followed by its own tag, and strips it on restore - so a mixed-up result is
    visible in the stored bytes."""

    def __init__(self, tag, **kwargs):
        super().__init__(name=f"tag_{tag}", **kwargs)
        self.tag = tag

    def compress(self, src_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "stored.bin")
        with open(src_path, "rb") as s, open(out, "wb") as d:
            d.write(s.read() + f"|{self.tag}".encode())
        time.sleep(0.05 * (hash(self.tag) % 5))
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        out = os.path.join(work_dir, "restored.bin")
        with open(stored_path, "rb") as s, open(out, "wb") as d:
            data = s.read()
            d.write(data[: data.rindex(b"|")])
        return out


class Unavailable(Fake):
    def available(self):
        return False, "tool missing"

    def compress(self, src_path, work_dir, time_limit_s=None):
        raise AssertionError("an unavailable strategy must never run")


class Compressing(Fake):
    """Really compresses (LZMA), so it wins over the baseline. When `trigger` exists, restore hangs (and
    writes its pid to `marker`) - archiving works, a later checkout gets stuck."""

    def __init__(self, name="fake_lzma", trigger=None, **kwargs):
        super().__init__(name=name, **kwargs)
        self.trigger = trigger

    def compress(self, src_path, work_dir, time_limit_s=None):
        import lzma
        out = os.path.join(work_dir, "stored.xz")
        with open(src_path, "rb") as s, open(out, "wb") as d:
            d.write(lzma.compress(s.read(), preset=6))
        return out

    def restore(self, stored_path, work_dir, time_limit_s=None):
        import lzma
        if self.trigger and os.path.exists(self.trigger):
            self.note()
            time.sleep(600)
        out = os.path.join(work_dir, "restored.bin")
        with open(stored_path, "rb") as s, open(out, "wb") as d:
            d.write(lzma.decompress(s.read()))
        return out


def worker_that_dies_at_startup(conn):
    """A worker process entry point that ends before saying it is ready (like a worker whose imports fail)."""
    conn.close()


def worker_that_never_says_ready(conn):
    """A worker process entry point that stays alive but never says it is ready (a worker stuck while starting)."""
    time.sleep(60)
