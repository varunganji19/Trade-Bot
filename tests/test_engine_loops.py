"""The dashboard's engine loops (bot/engines.py), driven for real with a fake
engine: a RUNNING engine must pick up a new interval on its next cycle, for
both books. The interval used to be captured by the loop thread at start,
so the cadence could only change through a stop and a restart."""
import time

import pytest


class FakeEngine:
    def __init__(self, *args, **kwargs):
        self.cycles = 0
        self.book_token = None
        self.last_error = None

    def run_cycle(self):
        self.cycles += 1

    def shutdown(self):
        pass


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize("book", ["standard", "fast"])
def test_a_running_loop_picks_up_a_new_interval(book, tmp_path, monkeypatch):
    import bot.dashboard as dash
    import bot.engines as engines
    import bot.hft as hft
    monkeypatch.setattr(dash, "journal", dash.Journal(str(tmp_path / "t.db")))
    monkeypatch.setattr(dash, "_write_engine_state", lambda *a: True)
    monkeypatch.setattr(dash, "_write_hft_state", lambda *a: True)
    if book == "standard":
        monkeypatch.setattr(engines, "TradingEngine", FakeEngine)
        for name, value in (("_engine", None), ("_engine_thread", None),
                            ("_engine_starting", False), ("_engine_interval", 60)):
            monkeypatch.setattr(dash, name, value)
        spawn, get, interval_attr = engines._spawn_engine, engines._get_engine, "_engine_interval"
        handle, thread_attr = "_engine", "_engine_thread"
    else:
        monkeypatch.setattr(hft, "build_hft_engine", lambda **kw: FakeEngine())
        for name, value in (("_hft_engine", None), ("_hft_thread", None),
                            ("_hft_starting", False), ("_hft_interval", 10)):
            monkeypatch.setattr(dash, name, value)
        spawn, get, interval_attr = engines._spawn_hft_engine, engines._get_hft_engine, "_hft_interval"
        handle, thread_attr = "_hft_engine", "_hft_thread"

    assert spawn(0)["status"] == "started"          # interval 0: cycles back to back
    eng = get()
    assert _wait(lambda: eng.cycles > 20)
    setattr(dash, interval_attr, 3600)               # what /api/.../interval does
    time.sleep(0.2)                                  # let the in-flight cycle finish
    settled = eng.cycles
    time.sleep(0.4)
    assert eng.cycles == settled                     # now sleeping the new interval
    # stop: clearing the handle wakes the sleeping loop within its poll step
    setattr(dash, handle, None)
    getattr(dash, thread_attr).join(timeout=3)
    assert not getattr(dash, thread_attr).is_alive()
