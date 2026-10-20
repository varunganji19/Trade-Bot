"""Recovery may use only agreeing, unmodified snapshot windows."""
import pandas as pd
import pytest

from scripts.recover_eurusd_replay import reconstruct


def snapshots(tmp_path):
    frame = pd.DataFrame({c: [1., 2., 3., 4.] for c in ("open", "high", "low", "close", "volume")},
                         index=pd.date_range("2026-01-04", periods=4, freq="h", tz="UTC"))
    paths = [tmp_path/"a.parquet", tmp_path/"b.parquet"]
    for path in paths:
        frame.to_parquet(path)
    return frame, paths


def test_recovery_slices_exact_window_without_rewriting_sources(tmp_path):
    _, paths = snapshots(tmp_path)
    before = [p.read_bytes() for p in paths]
    result = reconstruct(paths, "2026-01-04T01:00", "2026-01-04T03:00")
    assert list(result.close) == [2., 3.]
    assert result.attrs["reconstructed"] is True
    assert [p.read_bytes() for p in paths] == before


@pytest.mark.parametrize("change", ["price", "missing_bar", "duplicate", "naive", "nonfinite"])
def test_disagreement_or_invalid_input_is_never_silently_recovered(tmp_path, change):
    frame, paths = snapshots(tmp_path)
    if change == "price":
        frame.iloc[1, 0] += 0.001
    elif change == "missing_bar":
        frame = frame.iloc[1:]
    elif change == "duplicate":
        frame = pd.concat([frame, frame.iloc[-1:]])
    elif change == "naive":
        frame.index = frame.index.tz_localize(None)
    else:
        frame.iloc[1, 0] = float("nan")
    frame.to_parquet(paths[1])
    with pytest.raises(ValueError):
        reconstruct(paths)


def test_recovery_requires_corroboration_and_nonempty_window(tmp_path):
    _, paths = snapshots(tmp_path)
    with pytest.raises(ValueError, match="at least two"):
        reconstruct(paths[:1])
    with pytest.raises(ValueError, match="distinct"):
        reconstruct([paths[0], paths[0]])
    with pytest.raises(ValueError, match="empty"):
        reconstruct(paths, "2027-01-01", "2027-01-02")
