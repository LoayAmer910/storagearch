"""When a verified candidate may replace the baseline: it must be smaller and save at least 1 MiB or 3 % of
the baseline (the thresholds measured on the old version). Files under 4 KiB are not worth trying:
container overhead ate any gain.

The decision depends on sizes only, so the same bytes always get the same decision. The old version also
rejected candidates that ran slower than 2 MB/s; that made the choice depend on how busy the machine was
(the differential test caught one real 2.3 MB JPEG flipping between JPEG XL and plain ZIP from run to run,
with wall time and with CPU time alike) and it threw away work that was already done. Speed is controlled
before a candidate runs instead - by the router's measured size limits for slow strategies - and bounded
while it runs by the executor's timeouts."""
from dataclasses import dataclass

MIB = 1024 * 1024


@dataclass(frozen=True)
class Policy:
    tiny_file_bytes: int = 4096
    min_saved_bytes: int = 1 * MIB
    min_saved_percent: float = 3.0

    def worth_trying(self, size):
        return size >= self.tiny_file_bytes

    def judge(self, original_size, baseline_size, stored_size, compress_s=0.0, restore_s=0.0):
        """compress_s / restore_s are reported in the verdict for the log; they do not decide anything."""
        saved = baseline_size - stored_size
        percent = saved / baseline_size * 100 if baseline_size > 0 else 0.0
        total_s = max(compress_s, 0.0) + max(restore_s, 0.0)
        throughput = (max(original_size, 1) / MIB) / total_s if total_s > 0 else float("inf")
        if stored_size >= baseline_size:
            return Verdict(False, "not smaller than the baseline", saved, percent, throughput)
        if not (saved >= self.min_saved_bytes or percent >= self.min_saved_percent):
            return Verdict(False, "saving too small", saved, percent, throughput)
        return Verdict(True, "accepted", saved, percent, throughput)


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reason: str
    saved_bytes: int
    saved_percent: float
    throughput_mb_s: float


DEFAULT = Policy()
