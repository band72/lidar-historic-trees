# Tiled Processing ("quadrants") Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `fip inventory INPUT --tile SIZE` processes a point cloud too large for memory as a grid of overlapping square tiles and writes one result, identical to an untiled run except for the memory it needs.

**Architecture:** A new module `tiling.py` plans a grid of tiles over the cloud's XY extent (each tile = a *core* cell plus an *overlap* buffer), runs the existing per-cloud stages (ground → heights → segmentation → fit) on each buffered tile, and keeps only the trees whose stem centre falls in that tile's core. Every location belongs to exactly one core, so trees are neither lost nor doubled; `dedupe_stems` removes the rare stem fitted twice across a core edge. LAS/LAZ tiles are cropped while reading, so peak memory is that of one buffered tile.

**Tech Stack:** Python 3.10+, numpy, laspy (headers for the extent), typer (CLI), pytest.

**Spec:** this plan's *Design* section below (decisions agreed in conversation on 2026-11-16; there is no separate spec document).

## Design

- **Why tiles:** one 2-minute vehicle segment (45 M points) peaks at 7.4 GB; a whole drive or a large terrestrial/airborne LAS can exceed RAM. Ground filtering and clustering are the memory peaks, and both are local.
- **Grid:** square cores of `SIZE` m (default none = untiled; minimum 20 m), aligned to the extent's minimum corner, covering the extent. Cores are half-open `[xmin, xmax) × [ymin, ymax)`, except that the last column/row includes its max edge, so every point of the extent is in exactly one core.
- **Buffer:** each tile is processed over its core grown by `OVERLAP` m on every side (default 10 m, minimum 5 m, must be < SIZE / 2). 10 m covers a cluster's horizontal extent in the 1-4 m slice, the roof/crown checks' 3 m look-around, and the ground filter's edge effects.
- **Ownership:** a record (measured or failed) belongs to the tile whose core contains its `(x, y)`. Others are dropped from that tile's results. Then `dedupe_stems` runs once over all kept records.
- **Ids:** `tree_id` is renumbered 1..N across the whole result, ordered by tile then by the tile's own id; `extra["tile"]` (e.g. `"r2c3"`) and `extra["tile_tree_id"]` keep the origin.
- **Extent and units:** from LAS/LAZ headers (`laspy.open(...).header.mins/maxs`, union over input files) without reading points; crop boxes are passed to `load_cloud(crop=...)` in the files' own units. Other formats (PLY, NPY, text, ROS bags) have no cheap extent: they are loaded once and sliced in memory, which still bounds the ground/segmentation peak, with a log line saying the memory benefit is partial.
- **Empty tiles** (no points, or no tree clusters) are skipped with an info line, never an error.
- **Everything after fitting is unchanged:** `--exclusions`, metrics, all exporters, `run.json` (plus a `tiles` list: id, core box, points, trees, seconds).
- **Out of scope:** parallel tiles (memory × N; later `--jobs`), the web app (it keeps one cloud in memory), automatic tile size.

## Global Constraints

- Defaults: `--tile` off; `--tile-overlap 10` (m). Limits: SIZE ≥ 20 m; 5 m ≤ OVERLAP < SIZE / 2. Invalid values exit with code 2 and a message naming the limit.
- Untiled runs must produce byte-identical `trees.csv` to before this change (no new columns when `--tile` is off).
- Lengths on the command line are metres, like every other `fip` length option except `--crop`/`--breast-height`, which stay in input units.
- Tests must pass with `LOG_LEVEL=warn` (CI); CLI tests that read log text set `LOG_LEVEL=info` for the subprocess.

## Review Focus

1. **A stem exactly on a core edge** (x = core xmax): must appear once, not zero or two times. Pinned in Task 1 (ownership) and Task 3 (end-to-end with a stem placed on the edge).
2. **Feet LAS files:** the crop box must be in feet when the file is in feet, or tiles come out empty or wrong. Pinned in Task 2 (`test_crop_boxes_are_in_file_units`).
3. **Several input files (a folder of tiles)** with a tiled run: the extent is the union of all headers. Pinned in Task 2 (`test_extent_is_union_of_files`).
4. **A tree near a core edge** (its cluster crosses into the neighbouring tile): the owning tile has it whole as long as the cluster's reach from the stem is < OVERLAP. Pinned in Task 3 by the 30 cm stem placed on the edge (it must match the untiled fit within 5 cm and 3 mm DBH); clusters reaching farther than the overlap are a documented limit (Task 4 README step) and checked on real data in Task 5.
5. **Input that cannot be cropped on read** (ROS bag, PLY): must still work, with the partial-memory log line. Pinned in Task 3 (`test_tiled_npy_matches_untiled`).

---

### Task 1: Tile grid and ownership

**Files:**
- Create: `python/forest_inventory_pipeline/tiling.py`
- Test: `tests/inventory/test_tiling.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Tile: row: int; col: int; core: tuple[float, float, float, float]; buffered: tuple[float, float, float, float]; last_row: bool; last_col: bool` with property `name -> str` (`f"r{row}c{col}"`) and method `owns(x: float, y: float) -> bool`.
  - `plan_tiles(extent: tuple[float, float, float, float], size: float, overlap: float = 10.0) -> list[Tile]` (row-major; raises `ParameterError` for the limits in Global Constraints).

- [ ] **Step 1: Write the failing tests**

```python
def test_grid_covers_extent():
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    assert [(t.row, t.col) for t in tiles] == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]
    assert tiles[0].core == (0, 0, 100, 100) and tiles[0].buffered == (-10, -10, 110, 110)
    assert tiles[-1].core == (200, 100, 300, 200)

def test_every_point_has_exactly_one_owner():
    tiles = tiling.plan_tiles((0, 0, 250, 120), size=100, overlap=10)
    rng = np.random.default_rng(0)
    pts = np.vstack([rng.uniform([0, 0], [250, 120], (2000, 2)),
                     [[100, 50], [200, 100], [250, 120], [0, 0], [100, 100]]])  # edges, corners, max corner
    for x, y in pts:
        assert sum(t.owns(x, y) for t in tiles) == 1, (x, y)

@pytest.mark.parametrize("size, overlap", [(10, 2), (100, 4), (30, 15)])
def test_limits(size, overlap):
    with pytest.raises(ParameterError):
        tiling.plan_tiles((0, 0, 100, 100), size=size, overlap=overlap)
```

- [ ] **Step 2: Run to verify they fail** — `pytest tests/inventory/test_tiling.py -v` → FAIL (`No module named ...tiling`).
- [ ] **Step 3: Implement `Tile`, `plan_tiles` in `tiling.py`** — number of columns `ceil((xmax - xmin) / size)` (at least 1); `owns` is half-open except on `last_row`/`last_col`.
- [ ] **Step 4: Run to verify they pass** — same command → 3 tests (5 cases) PASS.
- [ ] **Step 5: Commit** — `git commit -m "tiling: tile grid with half-open cores and overlap buffers"`

### Task 2: Extent and crop boxes from file headers

**Files:**
- Modify: `python/forest_inventory_pipeline/tiling.py`
- Test: `tests/inventory/test_tiling.py`

**Interfaces:**
- Consumes: `Tile` (Task 1); `inventory.expand_inputs(paths) -> list[Path]`; `units.normalize_unit`, `units.from_metres`.
- Produces:
  - `cloud_extent(paths, units: str = "auto") -> tuple[tuple[float, float, float, float], str] | None` — the union XY extent **in metres** and the files' unit, from LAS/LAZ headers only; `None` if any input is not LAS/LAZ.
  - `crop_box(tile: Tile, unit: str) -> str` — `"xmin,ymin,xmax,ymax"` of `tile.buffered` converted to `unit`, for `load_cloud(crop=...)`.

- [ ] **Step 1: Write the failing tests** (use the LAS writer helper from `tests/inventory/test_import_export.py`, `write_las`)

```python
def test_extent_is_union_of_files(tmp_path):
    write_las(tmp_path / "a.las", np.array([[0.0, 0, 0], [10, 5, 1]]))
    write_las(tmp_path / "b.las", np.array([[20.0, -3, 0], [25, 4, 1]]))
    ext, unit = tiling.cloud_extent([tmp_path])
    assert ext == pytest.approx((0, -3, 25, 5), abs=1e-3) and unit == "m"

def test_crop_boxes_are_in_file_units():
    t = tiling.plan_tiles((0, 0, 60, 60), size=30, overlap=10)[0]
    assert tiling.crop_box(t, "ft") == ",".join(f"{v / 0.3048:.4f}" for v in (-10, -10, 40, 40))

def test_no_cheap_extent_for_other_formats(tmp_path):
    np.save(tmp_path / "p.npy", np.zeros((10, 3)))
    assert tiling.cloud_extent([tmp_path / "p.npy"]) is None
```

- [ ] **Step 2: Run to verify they fail** → FAIL (`cloud_extent` not defined).
- [ ] **Step 3: Implement both functions** — `laspy.open(path).header.mins/maxs`; the header's unit via the same rule `load_cloud` uses (explicit `units` beats the header; LAS 1.4 WKT/GeoKeys feet → `ft`/`us_ft`), refusing mixed units like `load_cloud` does.
- [ ] **Step 4: Run to verify they pass** → PASS.
- [ ] **Step 5: Commit** — `git commit -m "tiling: extent from LAS headers, crop boxes in file units"`

### Task 3: Run the stages per tile and assemble one result

**Files:**
- Modify: `python/forest_inventory_pipeline/inventory.py` (extract the per-cloud stages from `fip.py` lines that run `segment_ground` … `fit_trees` into one function)
- Modify: `python/forest_inventory_pipeline/fip.py:449-457` (call the extracted function)
- Modify: `python/forest_inventory_pipeline/tiling.py`
- Test: `tests/inventory/test_tiling.py`

**Interfaces:**
- Consumes: `plan_tiles`, `Tile.owns`, `cloud_extent`, `crop_box` (Tasks 1-2); `inventory.load_cloud`, `inventory.dedupe_stems`.
- Produces:
  - `inventory.process_cloud(xyz: np.ndarray, classification: np.ndarray | None, gp: GroundParams, sp: SegmentParams, fp: FitParams, keep_points: bool = False) -> list[TreeRecord]` — the existing stages, including the `OverheadIndex` roof context; raises `StageError("segment", ...)` when no clusters are found (unchanged message).
  - `tiling.run_tiled(inputs, size: float, overlap: float, gp, sp, fp, *, reader=None, units="auto", keep_points=False, keep_noise=False, reader_options=None) -> tuple[list[TreeRecord], list[dict]]` — records with global ids and `extra["tile"]`, `extra["tile_tree_id"]`; and one summary dict per tile (`name`, `core`, `points`, `trees`, `seconds`, `skipped`: reason or `""`).

- [ ] **Step 1: Write the failing tests**

```python
def _plot(tmp_path, fmt="las"):
    plot = make_plot(n_trees=30, size=40.0, seed=5)
    xyz = plot.points.copy()
    xyz = np.vstack([xyz, _stem_at(x=20.0, y=13.0)])  # a stem exactly on a core edge (size 20 grid)
    path = tmp_path / f"plot.{fmt}"
    write_las(path, xyz) if fmt == "las" else np.save(path, xyz)
    return path

def _same_trees(a, b):
    fa, fb = [r for r in a if r.fitted], [r for r in b if r.fitted]
    assert len(fa) == len(fb)
    xy = np.array([[r.x, r.y] for r in fb])
    for r in fa:
        j = int(np.argmin(np.hypot(*(xy - [r.x, r.y]).T)))
        assert np.hypot(r.x - fb[j].x, r.y - fb[j].y) < 0.05 and abs(r.dbh - fb[j].dbh) < 0.003

def test_tiled_las_matches_untiled(tmp_path):
    path = _plot(tmp_path)
    cloud = inv.load_cloud(path)
    whole = inv.process_cloud(cloud.xyz, cloud.classification, GP, SP, FP)
    tiled, tiles = tiling.run_tiled([path], 20.0, 6.0, GP, SP, FP)
    _same_trees(whole, tiled)
    assert len(tiles) == 4 and sum(t["trees"] for t in tiles) == len(tiled)
    edge = [r for r in tiled if r.fitted and abs(r.x - 20.0) < 0.1 and abs(r.y - 13.0) < 0.1]
    assert len(edge) == 1  # the stem on the core edge, once
    assert [r.tree_id for r in tiled] == list(range(1, len(tiled) + 1))
    assert {r.extra["tile"] for r in tiled} <= {"r0c0", "r0c1", "r1c0", "r1c1"}

def test_tiled_npy_matches_untiled(tmp_path):  # no crop on read: loaded once, sliced
    path = _plot(tmp_path, "npy")
    cloud = inv.load_cloud(path)
    _same_trees(inv.process_cloud(cloud.xyz, None, GP, SP, FP),
                tiling.run_tiled([path], 20.0, 6.0, GP, SP, FP)[0])

def test_empty_tiles_are_skipped(tmp_path):
    xyz = make_plot(n_trees=5, size=15.0, seed=1).points
    write_las(tmp_path / "gap.las", np.vstack([xyz, xyz + [60.0, 0, 0]]))  # empty middle tiles
    recs, tiles = tiling.run_tiled([tmp_path / "gap.las"], 20.0, 6.0, GP, SP, FP)
    assert any(t["skipped"] for t in tiles) and sum(r.fitted for r in recs) >= 8
```

`_stem_at(x, y)` builds one vertical 30 cm stem column (0-4 m, ring of points, 1 cm spacing) on the plot's ground; `GP, SP, FP` are the default parameter objects (module constants in the test file).

- [ ] **Step 2: Run to verify they fail** → FAIL (`process_cloud` / `run_tiled` not defined).
- [ ] **Step 3: Implement `inventory.process_cloud`** by moving the stage calls out of `fip.inventory`; `fip.inventory` calls it (no behaviour change: the untiled `trees.csv` must not change — Step 4 checks it).
- [ ] **Step 4: Run the untiled regression first** — `pytest tests/inventory/test_end_to_end.py tests/inventory/test_real_shed_row.py tests/inventory/test_import_export.py -q` → all PASS.
- [ ] **Step 5: Implement `tiling.run_tiled`** — extent from `cloud_extent`, else load once and slice the array per buffered box (log: "this format cannot be cropped while reading: the whole cloud is loaded once"); per tile: `load_cloud(crop=crop_box(...))` (LAS) or slice, `process_cloud`, keep `owns(r.x, r.y)` records (catch `StageError("segment")` and `InputDataError("no points ...")` as `skipped`), tag `extra`, then `dedupe_stems` over all, then renumber ids.
- [ ] **Step 6: Run the tiling tests** — `pytest tests/inventory/test_tiling.py -v` → all PASS.
- [ ] **Step 7: Commit** — `git commit -m "tiling: run the stages per tile and keep each tree once"`

### Task 4: `fip inventory --tile` and docs

**Files:**
- Modify: `python/forest_inventory_pipeline/fip.py` (options `--tile`, `--tile-overlap`; branch to `run_tiled`; `tiles` in `context_for`)
- Modify: `README.md` (CLI options table; a "Large clouds: tiles" subsection under `fip inventory`; the README to-do list)
- Test: `tests/inventory/test_tiling.py`

**Interfaces:**
- Consumes: `tiling.run_tiled` (Task 3); existing `inventory.context_for(..., **params)`, `export_records`, exclusions flow.
- Produces: CLI `fip inventory INPUT --tile SIZE [--tile-overlap OVERLAP]`; `run.json` → `parameters.tiles` (the list from `run_tiled`); `trees.csv` gains columns `tile`, `tile_tree_id` only in tiled runs.

- [ ] **Step 1: Write the failing tests**

```python
def test_cli_tiled_run(tmp_path):
    path = _plot(tmp_path)
    res = fip("inventory", str(path), "-o", str(tmp_path / "out"), "-e", "csv", "--tile", "20", "--tile-overlap", "6")
    assert res.returncode == 0, res.stdout + res.stderr
    rows = list(csv.DictReader((tmp_path / "out" / "trees.csv").open()))
    assert {"tile", "tile_tree_id"} <= set(rows[0])
    tiles = json.loads((tmp_path / "out" / "run.json").read_text())["parameters"]["tiles"]
    assert len(tiles) == 4 and all({"name", "core", "points", "trees", "seconds", "skipped"} <= set(t) for t in tiles)

def test_cli_untiled_has_no_tile_columns(tmp_path):
    path = _plot(tmp_path)
    assert fip("inventory", str(path), "-o", str(tmp_path / "u"), "-e", "csv").returncode == 0
    assert "tile" not in next(csv.DictReader((tmp_path / "u" / "trees.csv").open()))

def test_cli_rejects_bad_tile_sizes(tmp_path):
    path = _plot(tmp_path)
    res = fip("inventory", str(path), "-o", str(tmp_path / "b"), "--tile", "10")
    assert res.returncode == 2 and "20 m" in res.stdout + res.stderr

def test_cli_tiled_reapplies_a_review(tmp_path):
    path = _plot(tmp_path)
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
```

`fip(...)` is the subprocess helper used in `tests/inventory/test_merge.py`, with `LOG_LEVEL=info` added to its environment.

- [ ] **Step 2: Run to verify they fail** → FAIL (no such option `--tile`).
- [ ] **Step 3: Implement the options and the branch in `fip.inventory`** — when `--tile` is given, units/CRS/breast height come from `cloud_extent` (or the once-loaded cloud), and the rest of the command (exclusions, `context_for`, export) runs on the `run_tiled` records unchanged; log one line per tile (`r0c1: 3.2 M points, 41 trees, 58 s`) and the peak tile.
- [ ] **Step 4: Run to verify they pass** — `LOG_LEVEL=warn pytest tests/inventory/test_tiling.py -v` → all PASS; then the whole suite `LOG_LEVEL=warn pytest -q` → all PASS.
- [ ] **Step 5: Document in `README.md`** — the two options in the CLI table; a short "Large clouds: tiles" subsection (what a tile/overlap is, default 10 m, that LAS/LAZ are cropped while reading and other formats are loaded once, the limit "trees whose cluster reaches farther than the overlap from the stem may be measured differently near a tile edge", and that segments of a drive are still best processed per segment and merged with `fip merge`); tick nothing in the to-do list, add "check `--tile` on a whole-drive cloud" to it.
- [ ] **Step 6: Commit** — `git commit -m "fip inventory --tile: process large clouds tile by tile"`

### Task 5: Real-data check (no code; results recorded)

**Files:**
- Modify: `README.md` ("Large clouds: tiles": add the measured numbers)

- [ ] **Step 1: Run segment 360-480 s untiled and tiled** — `fip inventory segment_360-480s.laz -o untiled --output-units ft -e csv` and the same with `--tile 60` (record peak memory with `/usr/bin/time -v`, "Maximum resident set size").
- [ ] **Step 2: Compare** — every accepted tree of one run has a match in the other within 0.3 ft and 1 cm DBH; list the exceptions with their distance to the nearest core edge; render side views of each exception (`render.py`-style: breast-height slice and two side views) and say what changed.
- [ ] **Step 3: Record** — peak memory untiled vs tiled, run time, and the comparison, in the README subsection.
- [ ] **Step 4: Commit** — `git commit -m "README: tiled vs untiled on real data"`
