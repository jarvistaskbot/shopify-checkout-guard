"""Tests for the v3 CUSUM slow-bleed detector (docs/12-V3-DIRECTION.md §3).

The accumulation math is a pure function (_cusum_update), so the calibration
claims are verified numerically here: ~30% under expectation sustained for ~6
hours fires; normal jitter never does; recovery decays and resolves.
Wiring (sweep hook, migration, alerter, incident type) is asserted on source.
"""

from pathlib import Path

from services.detector import (
    _cusum_update,
    _SB_ALERT_S,
    _SB_RESOLVE_S,
    _SB_SLACK,
    _SB_STAT_CAP,
)

ROOT = Path(__file__).parent


def _run_hours(ratios, s=0.0):
    for r in ratios:
        s = _cusum_update(s, r)
    return s


# ---- calibration: the headline claim ----

def test_30pct_under_for_6h_fires():
    s = _run_hours([0.70] * 6)
    assert s >= _SB_ALERT_S, f"6h at 30% under must alert (S={s})"


def test_30pct_under_for_4h_does_not_fire():
    s = _run_hours([0.70] * 4)
    assert s < _SB_ALERT_S, f"4h at 30% under must NOT yet alert (S={s})"


def test_severe_collapse_fires_fast():
    # 70% under: (0.85 - 0.30) = 0.55/h -> fires in 2 hours.
    s = _run_hours([0.30] * 2)
    assert s >= _SB_ALERT_S


def test_normal_jitter_never_accumulates():
    # 10% under is within slack; a whole day of it stays at zero.
    s = _run_hours([0.90, 0.95, 1.05, 0.92, 1.0, 0.88] * 4)
    assert s == 0.0


def test_alternating_good_bad_does_not_creep():
    # 30% under one hour, fully normal the next: decay (0.25) outpaces
    # accumulation (0.15) so no slow false-positive creep.
    s = _run_hours([0.70, 1.0] * 12)
    assert s < _SB_ALERT_S


def test_recovery_decays_to_resolve():
    s = _run_hours([0.70] * 6)          # alerting
    assert s >= _SB_ALERT_S
    s = _run_hours([1.0] * 3, s=s)      # three normal hours
    assert s <= _SB_RESOLVE_S, f"3 normal hours must reach resolve level (S={s})"


def test_stat_is_capped():
    s = _run_hours([0.0] * 50)          # total outage for 50h
    assert s == _SB_STAT_CAP
    # capped stat still recovers in bounded time
    s = _run_hours([1.0] * 12, s=s)
    assert s == 0.0


def test_slack_boundary_exact():
    # exactly at the slack edge: no accumulation
    assert _cusum_update(0.0, 1.0 - _SB_SLACK) == 0.0
    # just under: accumulates the (tiny) shortfall
    assert _cusum_update(0.0, 1.0 - _SB_SLACK - 0.01) > 0.0


# ---- wiring: source-shape assertions ----

def test_wiring():
    det = (ROOT / "services" / "detector.py").read_text()
    al = (ROOT / "services" / "alerter.py").read_text()
    mig = (ROOT / "migrations" / "008_cusum_slow_bleed.sql").read_text()

    # sweep hooked into the proactive loop
    assert "asyncio.create_task(run_slow_bleed_sweep())" in det
    # incident lifecycle reused with its own type
    assert "'slow_bleed'" in det and "_get_active_incident(conn, shop_domain, \"slow_bleed\")" in det
    # each completed hour processed exactly once
    assert "if last is not None and last >= hour_start:" in det
    # sparse-store guard
    assert "_SB_MIN_EXPECTED" in det
    # Postgres DOW (0=Sunday) vs Python weekday (0=Monday) conversion present
    assert "(hour_dt.weekday() + 1) % 7" in det
    # migration adds both state columns idempotently
    assert "ADD COLUMN IF NOT EXISTS cusum_stat" in mig
    assert "ADD COLUMN IF NOT EXISTS cusum_updated_at" in mig
    # alerter registered with label + sender
    assert '"slow_bleed": "Slow Checkout Bleed"' in al
    assert "async def send_slow_bleed_alert(" in al
