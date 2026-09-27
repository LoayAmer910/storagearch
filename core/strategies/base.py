"""The strategy contract.

A strategy turns a file into a stored representation and back. It never receives a file name: the
same bytes always give the same stored bytes, and the restore never depends on what the file was
called (the defect behind the old "archived but not restorable" bug). It writes only inside the work
directory it is given and returns paths inside it. It raises StrategyError (or any exception) when it
cannot handle an input; the caller treats that as "this candidate failed", never as a hard error.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass

MIB = 1024 * 1024


class StrategyError(Exception):
    """The strategy cannot store or restore this input."""


@dataclass(frozen=True)
class StrategySpec:
    name: str
    version: int                 # format version of the stored bytes; recorded with every stored member
    weight: str                  # "light" or "heavy" (heavy = large memory or CPU per byte)
    in_process_safe: bool        # may run on a thread: a library call with a known bound, or an external
                                 # tool that is killed by its own timeout
    max_in_process_bytes: int    # above this size the executor runs it in a killable worker process
    base_timeout_s: float
    seconds_per_mib: float

    def timeout_s(self, size_bytes):
        return self.base_timeout_s + self.seconds_per_mib * size_bytes / MIB


class Strategy(ABC):
    spec: StrategySpec

    @property
    def name(self):
        return self.spec.name

    def available(self):
        """(True, "") or (False, reason). Must be cheap and must not touch the network."""
        return True, ""

    @abstractmethod
    def compress(self, src_path, work_dir, time_limit_s=None):
        """Stores the bytes of src_path. Returns the path of the stored file (inside work_dir)."""

    @abstractmethod
    def restore(self, stored_path, work_dir, time_limit_s=None):
        """Rebuilds the original bytes. Returns the path of the restored file (inside work_dir)."""
