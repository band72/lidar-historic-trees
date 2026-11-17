"""Guards before processing: separately stitched clouds loaded together, and too little memory."""

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.errors import InputDataError
from forest_inventory_pipeline.synthetic import make_plot
from test_import_export import WKT_SPCS_USFT, write_las  # noqa: F401

# load the C++ clustering extension now, outside CliRunner: in an editable install its first
# import runs a rebuild check in a subprocess, which needs a real stdout (CliRunner's has none)
import forest_inventory_pipeline.cluster.quickshiftpp  # noqa: E402, F401


def _slam_run(seed, shift=(0.0, 0.0)):
    """A cloud in a SLAM run's own local frame: the extent contains the origin (first scan)."""
    xyz = make_plot(n_trees=4, size=20.0, seed=seed).points.copy()
    xyz[:, :2] -= 10.0  # around (0, 0), like KISS-ICP output
    xyz[:, :2] += shift
    return xyz


def test_separately_stitched_runs_are_refused(tmp_path):
    """2022-03-08: six KISS-ICP segments loaded as one cloud stacked six drives on top of each other."""
    d = tmp_path / "segments"
    d.mkdir()
    write_las(d / "segment_0-120s.laz", _slam_run(1))
    write_las(d / "segment_120-240s.laz", _slam_run(2, shift=(3.0, -2.0)))
    with pytest.raises(InputDataError, match="fip merge") as e:
        inv.load_cloud(d)
    assert "segment_0-120s.laz" in str(e.value) and "--same-frame" in str(e.value)
    assert len(inv.load_cloud(d, check_frames=False).xyz) > 0  # the escape hatch


def test_adjacent_tiles_and_single_files_are_fine(tmp_path):
    d = tmp_path / "tiles"
    d.mkdir()
    write_las(d / "a.laz", _slam_run(1))  # contains the origin
    write_las(d / "b.laz", _slam_run(2, shift=(25.0, 0.0)))  # next to it: does not
    assert len(inv.load_cloud(d).xyz) > 0
    assert len(inv.load_cloud(d / "a.laz").xyz) > 0


def test_files_with_a_coordinate_system_are_not_questioned(tmp_path):
    d = tmp_path / "georef"
    d.mkdir()
    write_las(d / "a.laz", _slam_run(1), wkt=WKT_SPCS_USFT)
    write_las(d / "b.laz", _slam_run(2), wkt=WKT_SPCS_USFT)
    assert len(inv.load_cloud(d).xyz) > 0


def test_npy_runs_are_checked_too(tmp_path):
    d = tmp_path / "npy"
    d.mkdir()
    np.save(d / "a.npy", _slam_run(1))
    np.save(d / "b.npy", _slam_run(2))
    with pytest.raises(InputDataError, match="own coordinates"):
        inv.load_cloud(d)


# --- memory -----------------------------------------------------------------------------------


def test_too_little_memory_stops_before_processing(monkeypatch):
    monkeypatch.setattr(inv, "available_memory_bytes", lambda: 8 * 10**9)  # 8 GB free
    with pytest.raises(InputDataError) as e:
        inv.check_memory(233_648_390)  # the six-segment load of 2022-03-08
    msg = str(e.value)
    assert "51 GB" in msg and "8 GB" in msg and "--tile" in msg  # it asked for 51 GB when killed
    inv.check_memory(20_000_000)  # 4.4 GB: fits


def test_unknown_memory_is_not_an_error(monkeypatch):
    monkeypatch.setattr(inv, "available_memory_bytes", lambda: None)
    inv.check_memory(10**9)


def test_available_memory_is_read_on_this_system():
    free = inv.available_memory_bytes()
    assert free is None or free > 2**20


def test_cli_memory_check_and_override(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from forest_inventory_pipeline import fip

    np.save(tmp_path / "p.npy", make_plot(n_trees=4, seed=3).points)
    monkeypatch.setattr(inv, "available_memory_bytes", lambda: 2**20)  # 1 MB free
    runner = CliRunner()
    res = runner.invoke(fip.cli, ["inventory", str(tmp_path / "p.npy"), "-o", str(tmp_path / "a"), "-e", "csv"])
    assert res.exit_code == 2
    assert not (tmp_path / "a" / "trees.csv").exists()
    res = runner.invoke(fip.cli, ["inventory", str(tmp_path / "p.npy"), "-o", str(tmp_path / "b"),
                                  "-e", "csv", "--no-memory-check"])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "b" / "trees.csv").exists()


def test_cli_same_frame_override(tmp_path):
    from typer.testing import CliRunner

    from forest_inventory_pipeline import fip

    d = tmp_path / "segments"
    d.mkdir()
    write_las(d / "s1.laz", _slam_run(1))
    write_las(d / "s2.laz", _slam_run(2))
    runner = CliRunner()
    res = runner.invoke(fip.cli, ["inventory", str(d), "-o", str(tmp_path / "a"), "-e", "csv"])
    assert res.exit_code == 2
    res = runner.invoke(fip.cli, ["inventory", str(d), "-o", str(tmp_path / "b"), "-e", "csv", "--same-frame"])
    assert res.exit_code == 0, res.output


def test_las_runs_are_refused_from_the_headers_before_reading(tmp_path, monkeypatch):
    """The six 2022-03-08 segments took 48 s and gigabytes to read before anything else ran."""
    from forest_inventory_pipeline.plugins.builtin.readers import LasReader

    d = tmp_path / "segments"
    d.mkdir()
    write_las(d / "s1.laz", _slam_run(1))
    write_las(d / "s2.laz", _slam_run(2))

    def no_read(*a, **k):
        raise AssertionError("points were read before the check")

    monkeypatch.setattr(LasReader, "read", no_read)
    with pytest.raises(InputDataError, match="fip merge"):
        inv.load_cloud(d)
