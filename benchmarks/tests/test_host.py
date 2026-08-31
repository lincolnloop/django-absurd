import pytest

from benchmarks import host


@pytest.mark.internal
def test_refuses_phase_when_wall_clock_outruns_monotonic() -> None:
    with pytest.raises(host.SuspendedPhaseError):
        host.check_phase_uninterrupted(elapsed_s=1.0, wall_s=30.0)


@pytest.mark.internal
def test_tolerates_clock_read_jitter_and_a_backwards_wall_clock() -> None:
    host.check_phase_uninterrupted(elapsed_s=10.0, wall_s=10.4)
    host.check_phase_uninterrupted(elapsed_s=30.0, wall_s=1.0)


@pytest.mark.functional
@pytest.mark.django_db
def test_collects_host_context_fields() -> None:
    context = host.collect_host_context()

    assert set(context) == {
        "absurd_sdk",
        "captured_at",
        "cpu_count",
        "django",
        "git_sha",
        "load_avg_1m",
        "postgres",
        "postgres_uptime_s",
        "python",
    }
    assert context["postgres_uptime_s"] >= 0.0
    assert context["cpu_count"] >= 1
