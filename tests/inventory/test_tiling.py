"""Tiled processing of large clouds: the grid, ownership, extents, and tiled vs untiled runs."""

import numpy as np
import pytest

from forest_inventory_pipeline import tiling
from forest_inventory_pipeline.errors import ParameterError


def test_grid_covers_extent():
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    assert [(t.row, t.col) for t in tiles] == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    assert tiles[0].core == (0, 0, 100, 100) and tiles[0].buffered == (-10, -10, 110, 110)
    assert tiles[-1].core == (200, 100, 300, 200)
    assert tiles[4].name == "r1c1"


def test_every_point_has_exactly_one_owner():
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    rng = np.random.default_rng(0)
    pts = np.vstack([rng.uniform([0, 0], [250, 120], (2000, 2)),
                     [[100, 50], [200, 100], [250, 120], [0, 0], [100, 100]]])  # edges, corners, max corner
    for x, y in pts:
        assert sum(t.owns(x, y) for t in tiles) == 1, (x, y)


def test_stems_just_outside_the_extent_still_have_an_owner():
    """A fitted stem centre can fall outside the points' extent (a trunk seen from one side)."""
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    for x, y in [(-0.3, 50), (50, -0.2), (-1, -1), (300.5, 199), (120, 200.4), (400, 400)]:
        assert sum(t.owns(x, y) for t in tiles) == 1, (x, y)


@pytest.mark.parametrize("size, overlap", [(10, 2), (100, 4), (30, 15)])
def test_limits(size, overlap):
    with pytest.raises(ParameterError):
        tiling.plan_tiles((0, 0, 100, 100), size=size, overlap=overlap)


# --- extent and crop boxes from file headers ------------------------------------------------

from test_import_export import WKT_SPCS_USFT, write_las  # noqa: E402

US_FT = 1200 / 3937


def test_extent_is_union_of_files(tmp_path):
    write_las(tmp_path / "a.las", np.array([[0.0, 0, 0], [10, 5, 1]]))
    write_las(tmp_path / "b.las", np.array([[20.0, -3, 0], [25, 4, 1]]))
    ext, unit = tiling.cloud_extent([tmp_path])
    assert ext == pytest.approx((0, -3, 25, 5), abs=1e-3) and unit == "m"


def test_extent_of_a_feet_file_is_in_metres(tmp_path):
    write_las(tmp_path / "f.las", np.array([[0.0, 0, 0], [100.0, 50, 1]]), wkt=WKT_SPCS_USFT)
    ext, unit = tiling.cloud_extent([tmp_path / "f.las"])
    assert unit == "us_ft"
    assert ext == pytest.approx((0, 0, 100 * US_FT, 50 * US_FT), abs=1e-3)


def test_crop_boxes_are_in_file_units():
    t = tiling.plan_tiles((0, 0, 60, 60), size=30, overlap=10)[0]
    assert tiling.crop_box(t, "ft") == ",".join(f"{v / 0.3048:.4f}" for v in (-10, -10, 40, 40))
    assert tiling.crop_box(t, "m") == "-10.0000,-10.0000,40.0000,40.0000"


def test_no_cheap_extent_for_other_formats(tmp_path):
    np.save(tmp_path / "p.npy", np.zeros((10, 3)))
    assert tiling.cloud_extent([tmp_path / "p.npy"]) is None


# --- the stages per tile, one result -----------------------------------------------------------

from forest_inventory_pipeline import inventory as inv  # noqa: E402
from forest_inventory_pipeline.synthetic import make_plot  # noqa: E402
from forest_inventory_pipeline.traits.dbh.robust_fit import FitParams  # noqa: E402

GP, SP, FP = inv.GroundParams(), inv.SegmentParams(), FitParams()
EDGE_Y = 25.0  # no synthetic tree within 5 m of (edge, 25) in make_plot(seed=5)


def _stem_at(x, y, ground_z, r=0.15):
    """A vertical 30 cm stem column, 0-4 m above ground, as a ring of points 1 cm apart."""
    n = int(2 * np.pi * r / 0.01)
    t = np.linspace(0, 2 * np.pi, n, endpoint=False)
    z = np.arange(0.0, 4.0, 0.01)
    tt, zz = np.meshgrid(t, z)
    return np.c_[x + r * np.cos(tt.ravel()), y + r * np.sin(tt.ravel()), ground_z + zz.ravel()]


def _plot(tmp_path, fmt="las"):
    """make_plot(30 trees, 40 m) plus a stem exactly on the first core edge of a 20 m grid."""
    xyz = make_plot(n_trees=30, size=40.0, seed=5).points
    edge_x = float(xyz[:, 0].min()) + 20.0  # the grid is aligned to the extent's minimum
    near = np.hypot(xyz[:, 0] - edge_x, xyz[:, 1] - EDGE_Y) < 1.0
    xyz = np.vstack([xyz, _stem_at(edge_x, EDGE_Y, float(np.percentile(xyz[near, 2], 5)))])
    path = tmp_path / f"plot.{fmt}"
    write_las(path, xyz) if fmt == "las" else np.save(path, xyz)
    return path, edge_x


def _same_trees(a, b):
    fa, fb = [r for r in a if r.fitted], [r for r in b if r.fitted]
    assert len(fa) == len(fb), (len(fa), len(fb))
    xy = np.array([[r.x, r.y] for r in fb])
    for r in fa:
        j = int(np.argmin(np.hypot(*(xy - [r.x, r.y]).T)))
        assert np.hypot(r.x - fb[j].x, r.y - fb[j].y) < 0.05, (r.x, r.y)
        # 1 cm, the README's criterion: each tile has its own ground model (a few cm apart from
        # the whole cloud's, and the multithreaded ground filter varies between runs), which
        # moves a DBH by a few mm; 3 mm failed now and then
        assert abs(r.dbh - fb[j].dbh) < 0.01, (r.x, r.y, r.dbh, fb[j].dbh)


def test_tiled_las_matches_untiled(tmp_path):
    path, edge_x = _plot(tmp_path)
    cloud = inv.load_cloud(path)
    whole = inv.process_cloud(cloud.xyz, cloud.classification, GP, SP, FP)
    tiled, tiles = tiling.run_tiled([path], 20.0, 6.0, GP, SP, FP)
    _same_trees(whole, tiled)
    ext, _ = tiling.cloud_extent([path])
    names = {t.name for t in tiling.plan_tiles(ext, 20.0, 6.0)}
    assert [t["name"] for t in tiles] == [t.name for t in tiling.plan_tiles(ext, 20.0, 6.0)]
    assert sum(t["trees"] for t in tiles) == len(tiled)
    edge = [r for r in tiled if r.fitted and np.hypot(r.x - edge_x, r.y - EDGE_Y) < 0.1]
    assert len(edge) == 1  # the stem on the core edge, once
    assert [r.tree_id for r in tiled] == list(range(1, len(tiled) + 1))
    assert {r.extra["tile"] for r in tiled} <= names
    assert all(isinstance(r.extra["tile_tree_id"], int) for r in tiled)


def test_tiled_npy_matches_untiled(tmp_path):  # no crop on read: loaded once, sliced
    path, _ = _plot(tmp_path, "npy")
    cloud = inv.load_cloud(path)
    _same_trees(inv.process_cloud(cloud.xyz, None, GP, SP, FP),
                tiling.run_tiled([path], 20.0, 6.0, GP, SP, FP)[0])


def test_empty_tiles_are_skipped(tmp_path):
    xyz = make_plot(n_trees=5, size=15.0, seed=1).points
    write_las(tmp_path / "gap.las", np.vstack([xyz, xyz + [60.0, 0, 0]]))  # empty middle tiles
    recs, tiles = tiling.run_tiled([tmp_path / "gap.las"], 20.0, 6.0, GP, SP, FP)
    assert any(t["skipped"] for t in tiles) and sum(r.fitted for r in recs) >= 8


def test_a_broken_input_still_fails(tmp_path):
    """Tiles skip unusable edges of a cloud, but when no tile is usable the error is raised."""
    from forest_inventory_pipeline.errors import FIPError

    write_las(tmp_path / "flat.las", np.c_[np.random.default_rng(0).uniform(0, 50, (5000, 2)),
                                           np.zeros(5000)])
    with pytest.raises(FIPError):
        tiling.run_tiled([tmp_path / "flat.las"], 20.0, 6.0, GP, SP, FP)


# --- fip inventory --tile ----------------------------------------------------------------------

import csv  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from forest_inventory_pipeline import exclusions as excl  # noqa: E402


def fip(*args):
    return subprocess.run(
        [sys.executable, "-m", "forest_inventory_pipeline.fip", *args],
        capture_output=True, text=True,
        env={**os.environ, "COLUMNS": "200", "LOG_LEVEL": "info"},
    )


def test_cli_tiled_run(tmp_path):
    path, _ = _plot(tmp_path)
    res = fip("inventory", str(path), "-o", str(tmp_path / "out"), "-e", "csv",
              "--tile", "20", "--tile-overlap", "6")
    assert res.returncode == 0, res.stdout + res.stderr
    rows = list(csv.DictReader((tmp_path / "out" / "trees.csv").open()))
    assert {"tile", "tile_tree_id"} <= set(rows[0])
    tiles = json.loads((tmp_path / "out" / "run.json").read_text())["parameters"]["tiles"]
    ext, _ = tiling.cloud_extent([path])
    assert len(tiles) == len(tiling.plan_tiles(ext, 20.0, 6.0))
    assert all({"name", "core", "points", "trees", "seconds", "skipped"} <= set(t) for t in tiles)


def test_cli_untiled_has_no_tile_columns(tmp_path):
    path, _ = _plot(tmp_path)
    assert fip("inventory", str(path), "-o", str(tmp_path / "u"), "-e", "csv").returncode == 0
    assert "tile" not in next(csv.DictReader((tmp_path / "u" / "trees.csv").open()))


def test_cli_rejects_bad_tile_sizes(tmp_path):
    path, _ = _plot(tmp_path)
    res = fip("inventory", str(path), "-o", str(tmp_path / "b"), "--tile", "10")
    assert res.returncode == 2 and "20 m" in res.stdout + res.stderr
    res = fip("inventory", str(path), "-o", str(tmp_path / "c"), "--tile", "20", "--crop", "0,0,10,10")
    assert res.returncode == 2 and "--crop" in res.stdout + res.stderr


def test_cli_tiled_reapplies_a_review(tmp_path):
    path, _ = _plot(tmp_path)
    assert fip("inventory", str(path), "-o", str(tmp_path / "u"), "-e", "csv").returncode == 0
    t = next(r for r in csv.DictReader((tmp_path / "u" / "trees.csv").open()) if r["status"] == "ok")
    excl.save(tmp_path / "review.json",
              [excl.Exclusion(x=float(t["x"]) + 0.05, y=float(t["y"]), reason="shrub")])
    res = fip("inventory", str(path), "-o", str(tmp_path / "t"), "-e", "csv",
              "--tile", "20", "--tile-overlap", "6", "--exclusions", str(tmp_path / "review.json"))
    assert res.returncode == 0, res.stdout + res.stderr
    rows = list(csv.DictReader((tmp_path / "t" / "trees.csv").open()))
    hit = [r for r in rows if np.hypot(float(r["x"]) - float(t["x"]), float(r["y"]) - float(t["y"])) < 0.1]
    assert [r["status"] for r in hit] == ["excluded"] and hit[0]["exclusion_reason"] == "shrub"


# --- final review fixes ------------------------------------------------------------------------

from forest_inventory_pipeline.errors import FIPError, StageError  # noqa: E402
from forest_inventory_pipeline.records import TreeRecord  # noqa: E402


def test_folder_of_files_tiled_matches_untiled(tmp_path):
    """Review finding 1: a tile that misses one of several input files must not be dropped."""
    xyz = make_plot(n_trees=12, size=30.0, seed=2).points
    folder = tmp_path / "tiles"
    folder.mkdir()
    write_las(folder / "a.las", xyz)
    write_las(folder / "b.las", xyz + [45.0, 0.0, 0.0])
    cloud = inv.load_cloud(folder)
    whole = inv.process_cloud(cloud.xyz, cloud.classification, GP, SP, FP)
    tiled, tiles = tiling.run_tiled([folder], 20.0, 6.0, GP, SP, FP)
    _same_trees(whole, tiled)


def _rec(tid, x, status="ok", dbh=0.3, support=500):
    fitted = status != "failed"
    return TreeRecord(tree_id=tid, status=status, x=x, y=10.0, ground_z=0.0, breast_height=1.3,
                      bh_elevation=1.3, n_points=1000, n_band_points=support,
                      dbh=dbh if fitted else float("nan"), inlier_ratio=1.0 if fitted else float("nan"),
                      ci_low=dbh * 0.9 if fitted else float("nan"), ci_high=dbh * 1.1 if fitted else float("nan"))


def test_edge_stem_placed_in_the_other_core_by_both_tiles_is_kept_once():
    """Review finding 2: each tile's fit can put the stem just inside the other tile's core."""
    a, b = tiling.plan_tiles((0, 0, 40, 20), 20.0, 6.0)  # cores split at x = 20
    out = tiling.assemble([(a, [_rec(0, 20.01)]), (b, [_rec(0, 19.99)])])
    assert len(out) == 1 and out[0].tree_id == 1


def test_fitted_and_failed_records_of_one_stem_are_kept_once():
    a, b = tiling.plan_tiles((0, 0, 40, 20), 20.0, 6.0)
    out = tiling.assemble([(a, [_rec(0, 19.97)]), (b, [_rec(3, 20.05, status="failed")])])
    assert [r.status for r in out] == ["ok"] and out[0].extra["tile"] == "r0c0"


def test_neighbouring_stems_across_an_edge_are_both_kept():
    a, b = tiling.plan_tiles((0, 0, 40, 20), 20.0, 6.0)
    out = tiling.assemble([(a, [_rec(0, 19.5), _rec(1, 5.0)]), (b, [_rec(0, 20.5), _rec(1, 35.0)])])
    assert sorted(round(r.x, 1) for r in out) == [5.0, 19.5, 20.5, 35.0]
    assert [r.tree_id for r in out] == [1, 2, 3, 4]


def test_out_of_memory_in_a_tile_stops_the_run(tmp_path, monkeypatch):
    """Review finding 3: an unexpected error in a tile is raised, not skipped as an empty tile."""
    path, _ = _plot(tmp_path)

    def boom(*a, **k):
        raise StageError("segment", "quickshift++ failed: MemoryError: cannot allocate")

    monkeypatch.setattr(inv, "process_cloud", boom)
    with pytest.raises(FIPError, match="--tile"):
        tiling.run_tiled([path], 20.0, 6.0, GP, SP, FP)


def test_read_errors_in_a_tile_stop_the_run(tmp_path, monkeypatch):
    path, _ = _plot(tmp_path)
    from forest_inventory_pipeline.errors import InputDataError

    real, calls = inv.load_cloud, []

    def bad(*a, **k):  # only the second tile's read fails
        calls.append(1)
        if len(calls) == 2:
            raise InputDataError("plot.las: cannot read LAS/LAZ: corrupt chunk")
        return real(*a, **k)

    monkeypatch.setattr(inv, "load_cloud", bad)
    with pytest.raises(InputDataError, match="corrupt chunk"):
        tiling.run_tiled([path], 20.0, 6.0, GP, SP, FP)
