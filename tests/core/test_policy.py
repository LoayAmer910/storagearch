"""core.policy - when a smaller candidate is worth keeping over the plain ZIP baseline."""
import pytest

from core.policy import MIB, Policy


P = Policy()


@pytest.mark.parametrize("baseline,stored,seconds,accepted,reason", [
    (1000, 1000, 0.1, False, "not smaller than the baseline"),
    (1000, 1001, 0.1, False, "not smaller than the baseline"),
    (1000, 971, 0.1, False, "saving too small"),             # 2.9 %
    (1000, 970, 0.1, True, "accepted"),                      # exactly 3 %
    (100 * MIB, 99 * MIB, 0.1, True, "accepted"),             # 1 MiB saved is enough on its own
    (100 * MIB, 99 * MIB + 1, 0.1, False, "saving too small"),
    (10 * MIB, 9 * MIB, 100.0, True, "accepted"),            # slow does not change a size-based decision
    (10 * MIB, 9 * MIB, 0.001, True, "accepted"),
    (0, 0, 0.0, False, "not smaller than the baseline"),
])
def test_policy_boundaries(baseline, stored, seconds, accepted, reason):
    verdict = P.judge(max(baseline, 1), baseline, stored, seconds, 0.0)
    assert (verdict.accepted, verdict.reason) == (accepted, reason), verdict


def test_policy_tiny_file_limit():
    assert not P.worth_trying(4095) and P.worth_trying(4096)
