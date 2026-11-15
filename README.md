<div align="center">
  <h1>LiDAR Tree Inventory</h1>
  <p><b>Find trees in LiDAR point clouds, fit each stem, and measure DBH with a confidence interval.</b></p>
  <p>
    <a href="#1-install"><img src="https://img.shields.io/badge/Windows-0078D6?logo=windows&logoColor=white" /></a>
    <a href="#1-install"><img src="https://img.shields.io/badge/Linux-FCC624?logo=linux&logoColor=black" /></a>
    <a href="https://github.com/band72/lidar-historic-trees/actions/workflows/ci.yml"><img src="https://github.com/band72/lidar-historic-trees/actions/workflows/ci.yml/badge.svg" /></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" /></a>
  </p>
  <img src="doc/motivation.png" width="400"/>
</div>

LiDAR Tree Inventory turns a point cloud of a forest plot into a tree list. For every tree it reports:

- position (x, y) and the **real-world elevation** of the ground and of the breast-height plane
- **DBH** (diameter at breast height) with a **confidence interval** (95 % by default) and a standard error
- quality flags that say when to distrust a measurement: one-sided scans, clutter, wide intervals
- basal area, plus any extra measurements added through plugins

Results can be explored in an interactive web app and exported as **CSV** (spreadsheets), **DXF** (CAD: DBH circles at true elevation), **GeoJSON** (GIS points or DBH circles) and a **segmented LAS/LAZ** point cloud. It reads PLY, PCD, LAS/LAZ (with units, CRS and ground classes from the header), text, NumPy and ROS bags.

It builds on the ICRA 2024 research code [PRBonn/forest_inventory_pipeline](https://github.com/PRBonn/forest_inventory_pipeline) (see [Citation](#citation)).

---

## Contents

1. [Install](#1-install): Windows, Linux, from source, Docker
2. [Quick start](#2-quick-start)
3. [Using the web app](#3-using-the-web-app)
4. [Using the command line](#4-using-the-command-line)
5. [Input data](#5-input-data): point clouds and ROS bags
6. [Outputs](#6-outputs): CSV, DXF, GeoJSON
7. [Configuration](#7-configuration)
8. [How the measurement works](#8-how-the-measurement-works)
9. [Calibrating the confidence interval](#9-calibrating-the-confidence-interval)
10. [Plugins](#10-plugins)
11. [Troubleshooting](#11-troubleshooting)
12. [Development](#12-development)
13. [Citation and license](#citation)

---

## 1. Install

### Windows 10 / 11 (64-bit)

1. Download **[LiDAR-Tree-Inventory-0.1.0-windows-x64-setup.exe](https://github.com/band72/lidar-historic-trees/releases/download/v0.1.0/LiDAR-Tree-Inventory-0.1.0-windows-x64-setup.exe)** from the [latest release](https://github.com/band72/lidar-historic-trees/releases/latest).
2. Run it. No administrator rights are needed: it installs for your user by default. On the first screen you can choose *Install for all users* instead, which needs admin.
3. Optional tasks during setup:
   - **Create a desktop shortcut**
   - **Add the `fip` command to PATH**, so you can use `fip` from any terminal
4. Start **LiDAR Tree Inventory** from the Start menu. A console window opens (it shows the log) and the app opens in your browser. Close the console window to stop the app.

Uninstall from *Settings → Apps*, or with *Uninstall LiDAR Tree Inventory* in the Start menu.

> Windows SmartScreen may warn that the installer is from an unknown publisher, because it isn't code-signed. Choose *More info → Run anyway*. Each release lists a `.sha256` checksum if you want to verify the download.

### Linux (x86_64)

Requires glibc 2.35 or newer: Ubuntu 22.04+, Debian 12+, Fedora 36+, Linux Mint 21+ and similar. Everything else is bundled, so you don't need Python.

```bash
# download (or use the link on the release page)
wget https://github.com/band72/lidar-historic-trees/releases/download/v0.1.0/LiDAR-Tree-Inventory-0.1.0-linux-x86_64.tar.gz
tar xzf LiDAR-Tree-Inventory-0.1.0-linux-x86_64.tar.gz
cd LiDAR-Tree-Inventory-0.1.0-linux-x86_64
./install.sh                 # for your user, no root needed
# or: sudo ./install.sh --system    (all users, installs to /opt)
```

The installer:
- copies the program to `~/.local/share/lidar-tree-inventory`
- adds the `fip` command to `~/.local/bin`
- adds **LiDAR Tree Inventory** to your applications menu
- checks the installation with `fip self-test`

If it says `~/.local/bin` is not on your PATH, add this to `~/.bashrc`:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

To uninstall, run `~/.local/share/lidar-tree-inventory/uninstall.sh` (or `/opt/lidar-tree-inventory/uninstall.sh` for a system install). Your own data and outputs are not touched.

### From source (for development or the latest code)

Tested on Linux and Windows (macOS is untested). You need Python 3.10–3.12, a C++20 compiler (GCC 11+, Clang 14+ or Visual Studio 2022) and CMake ≥ 3.28. On Linux, also install the Open3D system libraries:

```bash
sudo apt install build-essential libegl1 libgl1 libgomp1     # Debian/Ubuntu
git clone https://github.com/band72/lidar-historic-trees.git
cd lidar-historic-trees
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[app,las]" rosbags                     # builds the C++ clustering extension
fip self-test
```

Optional extras: `app` (web interface), `las` (LAS/LAZ input), `dxf` (DXF export only), `test` (pytest). `rosbags` adds ROS bag input.

### Docker

The upstream Docker setup is still available in `docker/`. See the [upstream instructions](https://github.com/PRBonn/forest_inventory_pipeline#docker). It runs the original `fip instance` and `fip dbh` commands.

### Check the installation

```bash
fip self-test
```

This runs four checks and prints `PASS` or `FAIL` for each:

1. the plugins load
2. the full pipeline runs on a synthetic plot, with an accuracy check
3. the exports are written
4. the web app runs headlessly

---

## 2. Quick start

**No data yet? Use the demo plot.** It is a synthetic forest with known true DBH, so you can see how accurate the results and intervals are.

*Web app:* start **LiDAR Tree Inventory** (or run `fip`), keep **Demo plot** selected, and press **Run segmentation**.

*Command line:*

```bash
fip demo-data demo/plot.ply              # writes demo/plot.ply and demo/plot_truth.csv
fip inventory demo/plot.ply -o results   # writes results/trees.csv, results/trees.dxf, results/fip.log
```

**Your own data:**

```bash
fip inventory /data/plot.laz -o results
fip inventory /data/run.bag -o results -R topic=/cloud_registered      # ROS bag, see section 5
```

Open `results/trees.csv` in a spreadsheet, or `results/trees.dxf` in a CAD program (AutoCAD, BricsCAD, LibreCAD, QGIS).

---

## 3. Using the web app

Start it from the menu, or with `fip` or `fip app`. It runs locally at `http://localhost:8501` and opens in your browser. Your data never leaves your computer.

The sidebar has three steps.

### Step 1 · Point cloud

Choose **Display & export units** at the top: *same as cloud*, *m / cm*, *ft / in* or *US ft / in*. This sets the units of the map, the table, the charts and every download. In feet mode, the breast-height slider is in feet (default 4.5 ft).

- **Demo plot**: a synthetic plot. Choose the number of trees and a random seed.
- **File / bag path**: type the path to a file on this computer (any [supported format](#5-input-data)) or a ROS2 bag folder. Large files are best opened this way. For ROS bags, a *ROS bag options* panel appears, with topic, target frame, every n-th message and more.
- **Upload**: drag a file into the browser, up to 4 GB.
- **Point cloud units** (for files): metres, feet or US survey feet.

### Step 2 · Find trees

These are the slow steps. Adjust the settings, then press **Run segmentation**.

| Setting | What it does | When to change it |
|---|---|---|
| Cloth resolution | Grid of the ground filter (CSF) | Larger on rough terrain with gaps; smaller for fine micro-relief |
| Ground class threshold | How close to the cloth a point must be to count as ground | Rarely |
| Ground refinement tolerance | Points this far above the local terrain are not ground. This removes stem bases from the terrain model | Raise it on very rough ground |
| Rigidness | 1 = steep terrain, 3 = flat | Steep slopes → 1 |
| Neighbours k | Clustering neighbourhood | Trees merged → lower; trees split into pieces → higher |
| Density β | Cluster merging | Higher → fewer, larger clusters |
| Voxel size | Downsampling for clustering only; fitting always uses all points | Very dense clouds → larger |
| Height slice | Height range above ground used to find trees; must contain breast height | Dense undergrowth → raise the lower bound |
| Min points per tree | Smaller clusters are ignored | Sparse scans → lower |

### Step 3 · Fit stems (live)

These sliders update every result instantly.

| Setting | Default | Meaning |
|---|---|---|
| Breast height | 1.3 m | Height of the DBH measurement (1.37 m = 4.5 ft in the US) |
| Band half-width | 0.25 m | Points within breast height ± this are used for the fit |
| Confidence level | 0.95 | Coverage of the reported interval |
| CI method | analytic | *analytic* is instant; *bootstrap* is slower and makes fewer assumptions ([details](#8-how-the-measurement-works)) |
| Systematic uncertainty | 0.5 cm | Error the scan itself can't reveal. **Calibrate it** with field data ([section 9](#9-calibrating-the-confidence-interval)) |
| Fitting details | | Inlier tolerance, robust kernel scale, max DBH, max stem tilt, RANSAC iterations, bootstrap resamples |
| Quality flags | | Thresholds that turn a tree's status into *warning* |
| Merged trees | on, 15 cm | Splits clusters that contain several stems, which recovers trees the segmentation merged |

### The main area

- **Summary**: trees fitted, mean DBH, basal area per hectare, and the median CI width. For the demo plot it also shows the RMSE and CI coverage against the true values.
- **Map**: tree positions sized by DBH. Status is shown by color, shape and label together (✔ ok, ⚠ warning, ✖ failed). Hover for details, and click a tree to select it.
- Below the map is a **DBH chart**: every tree's DBH with its confidence interval.
- **Tree detail**: the breast-height slice with the fitted circle and the CI circles, and a 3D view of the tree with the fitted cylinder.
- **Table & export**: a filterable table with download buttons for every exporter (CSV, DXF and any plugins). Each exporter's options are shown above its button.
- **Calibration**: upload field measurements to estimate the systematic uncertainty, then apply it with one click.
- **Log**: the application log, which you can filter to warnings or errors.

---

## 4. Using the command line

Run `fip --help` for the command list, and `fip <command> --help` for a command's options.

| Command | Purpose |
|---|---|
| `fip` (no arguments) or `fip app` | Start the web app |
| `fip inventory INPUT` | Full inventory: CSV + DXF (+ other exports) |
| `fip calibrate TREES_CSV FIELD_CSV` | Estimate the systematic uncertainty from field data |
| `fip plugins` | List readers, exporters and metrics |
| `fip demo-data OUTPUT` | Write a synthetic plot with true DBH values |
| `fip self-test` | Check the installation |
| `fip instance`, `fip dbh` | The original research pipeline (unchanged) |

### `fip inventory`

```bash
fip inventory INPUT... [options]     # one or more files, or a folder of tiles
```

| Option | Description |
|---|---|
| `-o, --output DIR` | Output folder (default `fip_output`). It holds the exports and `fip.log` |
| `-c, --config FILE` | YAML/JSON settings file, see [Configuration](#7-configuration) |
| `-e, --export NAME` | Exporter to run; repeatable: `csv`, `dxf`, `geojson`, `points` or a plugin. Default: `-e csv -e dxf` |
| `-E, --export-option NAME.KEY=VALUE` | Exporter option, e.g. `-E dxf.z_mode=plan`; repeatable |
| `--reader NAME` | Force a reader (normally picked from the file extension) |
| `-R, --reader-option KEY=VALUE` | Reader option, e.g. `-R topic=/points`; repeatable |
| `--plugin-dir DIR` | Load extra plugins from this folder; repeatable |
| `-u, --units auto\|m\|ft\|us_ft` | Length unit of the input (`auto` = from the LAS header, else metres), and of `--breast-height`, `--model-sd` and `--crop` (see [Metres and feet](#metres-and-feet)) |
| `--crop XMIN,YMIN,XMAX,YMAX` | Process only this box (input coordinates); LAS is cropped while reading |
| `--ground-source auto\|csf\|classification` | Ground from the file's classification if present (`auto`), always CSF, or require the classification |
| `--crs EPSG:xxxx` | Coordinate system to record in the outputs (default: from the file header) |
| `--keep-noise` | Keep points classified as noise (ASPRS 7/18) |
| `--output-units m\|ft\|us_ft` | Length unit of the exports (default: same as `--units`) |
| `--breast-height H` | Override the breast height (in `--units`; default 1.3 m, or 4.5 ft for feet) |
| `--confidence P` | Override the confidence level, e.g. `0.9` |
| `--ci-method analytic\|bootstrap` | Override the CI method |
| `--model-sd S` | Override the systematic uncertainty (in `--units`) |
| `--log-level trace\|debug\|info\|warn` | Console verbosity (the log file always has full detail) |

Examples:

```bash
# every format: table, CAD, GIS circles, and the segmented cloud for checking in CloudCompare
fip inventory plot.laz -o out -e csv -e dxf -e geojson -e points -E geojson.geometry=circle

# a 50 m plot cut from a large State Plane tile; units and CRS come from the header
fip inventory big_tile.laz -o out --crop 656100,1312300,656264,1312464

# a folder of tiles, European Excel CSV, DXF as a flat 2D plan
fip inventory tiles/ -o out -E csv.delimiter=semicolon -E csv.decimal=comma -E dxf.z_mode=plan

# a US plot in feet: 4.5 ft breast height, 90 % intervals, bootstrap; outputs in feet and inches
fip inventory plot.laz -o out --units ft --confidence 0.9 --ci-method bootstrap

# everything from a settings file
fip inventory plot.laz -o out -c config/inventory.yaml
```

Exit codes: `0` success, `2` invalid input or settings (with a clear message), `1` unexpected error (full traceback in `fip.log`).

### `fip app`

| Option | Description |
|---|---|
| `--port N` | Port (default 8501; the next free port is used if it's taken) |
| `--no-browser` | Don't open a browser window |
| `--address A` | `127.0.0.1` (default, this computer only), or `0.0.0.0` to share on your network |
| `--plugin-dir DIR` | Extra plugins |

### `fip calibrate`

```bash
fip calibrate out/trees.csv field_measurements.csv [--dbh-unit auto|m|cm|in|ft] [--xy-unit m|ft|us_ft] [--max-distance 0.5] [--current-model-sd 0.005]
```

See [section 9](#9-calibrating-the-confidence-interval).

---

## 5. Input data

### Point cloud files

| Format | Extensions | Notes |
|---|---|---|
| PLY / PCD / XYZ | `.ply .pcd .xyz .xyzn .xyzrgb .pts` | Read with Open3D |
| LAS / LAZ | `.las .laz` | Reads **units and coordinate system from the header**, keeps the **ground classification**, drops noise points (classes 7/18), and can **crop while reading** to save memory. LAZ is supported in the installers and with the `las` extra |
| NumPy | `.npy` | An `N × 3` (or wider) array |
| Text | `.txt .csv .asc` | The first three numeric columns are x, y, z. Header lines are skipped and the delimiter is detected automatically |

**Several files / tiles:** pass several files (`fip inventory a.laz b.laz`) or a folder (`fip inventory tiles/`). They are merged into one plot, and must share the same units and coordinate system.

**Cropping:** `--crop xmin,ymin,xmax,ymax` (in the file's coordinates) processes only that box. For LAS/LAZ it's applied while reading, so a plot can be cut from a multi-gigabyte tile without loading the whole tile. In the app, use the *Crop* field.

**Existing ground classification:** if a LAS file has ground points (ASPRS class 2), they are used directly instead of re-running the ground filter (`--ground-source auto`, the default). Use `--ground-source csf` to always run CSF, or `classification` to insist on the file's classes.

**Coordinate system:** the CRS in the LAS header (WKT or GeoTIFF keys) is detected and passed to every output: `trees.prj`, GeoJSON, the segmented LAS and `run.json`. Override it, or set it for files without one, with `--crs EPSG:xxxx`. Files in longitude/latitude are refused with a message, because they need reprojecting to metres or feet first.

Requirements:
- **Units: metres or feet.** Tell the program which one your cloud uses (see [Metres and feet](#metres-and-feet) below). Clouds in millimetres must be converted first.
- The cloud should cover the stems from the ground up to at least 1.5 m. Terrestrial, mobile (backpack/robot/handheld) and dense UAV scans work. Typical airborne LiDAR is too sparse on the stems.
- One plot per file. Very large clouds (more than about 50 million points) are best cropped or thinned first.

### Metres and feet

| Unit | Name in the program | Exactly |
|---|---|---|
| metre | `m` (default) | |
| international foot | `ft` | 0.3048 m |
| US survey foot | `us_ft` | 1200/3937 m. Many US State Plane systems use it; over large coordinate values it differs from `ft` |

- **Input:** detected automatically from LAS headers (`--units auto`, the default; files without units are assumed to be metres). Set it with `--units ft` / `us_ft` / `m`, or *Point cloud units* in the app. The cloud is converted to metres on reading, and all calculations run in metres.
- **Output:** exports use the same unit as the input by default, so a feet cloud gives feet CSV, DXF and GeoJSON with your **original coordinates**. Choose differently with `--output-units m`, per exporter with `-E dxf.units=m`, or with *Display & export units* in the app.
- **DBH:** shown in cm for metric output and in **inches** for feet. The CSV always has both `dbh_cm` and `dbh_in`, and a `length_unit` column says which unit the other lengths are in.
- **US breast height:** with `--units ft` or `us_ft`, breast height defaults to **4.5 ft** (1.37 m). `--breast-height` and `--model-sd` are then given in feet too, e.g. `--breast-height 4.5`.
- **Config files** (`-c`) always use metres.
- If a cloud's size suggests it's in feet but you didn't say so, the log warns you.

```bash
fip inventory stateplane.laz --units us_ft -o out            # outputs in US survey feet, DBH in inches
fip inventory plot_ft.las --units ft --output-units m -o out # read feet, write metres
```

### ROS bags (ROS1 `.bag`, ROS2 folder, `.db3`, `.mcap`)

The `rosbag` reader collects `sensor_msgs/PointCloud2` messages from one topic. You don't need a ROS installation. What matters is **which frame the clouds are in**:

| Your bag contains | What to do |
|---|---|
| A registered/mapped cloud topic (e.g. `/cloud_registered` from FAST-LIO, a `/map` topic) | `-R topic=/cloud_registered` |
| Raw scans in the sensor frame, **plus** `/tf` from SLAM/odometry | `-R topic=/os_cloud_node/points -R target_frame=map`. Each scan is transformed with the pose at its timestamp |
| Raw scans with no poses | Run SLAM first (e.g. FAST-LIO, KISS-ICP) and record the registered cloud. Stacking raw scans without poses smears the trees, so the reader refuses with an explanation |
| A static scanner | `-R allow_sensor_frame=true` |

Other rosbag options:

| Option | Meaning |
|---|---|
| `every_nth` | Use only every n-th message |
| `max_messages` | Stop after this many messages |
| `min_range` / `max_range` | Distance filter in the sensor frame |
| `voxel_size` | De-duplication grid for the accumulated cloud (default 1 cm) |
| `tf_tolerance` | Seconds a scan may lie outside the `/tf` time range |

If the bag has a single PointCloud2 topic, the reader uses it automatically. Otherwise the error message lists the topics available.

---

## 6. Outputs

### `trees.csv`: one row per tree

Lengths are in the output unit named in the `length_unit` column: metres by default, or feet ([details](#metres-and-feet)). Failed trees are included, with a reason, unless you pass `-E csv.include_failed=false`.

| Column | Meaning |
|---|---|
| `tree_id` | Tree number |
| `status` | `ok`, `warning` (measured, but check `message`) or `failed` (not measurable; see `message`) |
| `length_unit` | `m`, `ft` or `us_ft`: the unit of every length column below |
| `x`, `y` | Stem centre at breast height, in the input coordinate system |
| `ground_z` | Terrain elevation under the stem |
| `breast_height` | Height used (e.g. 1.3) |
| `bh_elevation` | **Absolute elevation of the DBH plane** = `ground_z + breast_height` |
| `dbh`, `dbh_cm`, `dbh_in` | DBH in the length unit, in cm, and in inches |
| `dbh_se` | Standard error of the DBH |
| `ci_low`, `ci_high` | Confidence interval bounds |
| `confidence`, `ci_method` | Interval level and method |
| `n_points`, `n_band_points` | Points in the tree, and in the breast-height band |
| `inlier_ratio` | Share of band points on the fitted stem surface |
| `rmse` | Fit residual (m) |
| `arc_coverage`, `max_gap_deg` | How much of the stem circumference was scanned, and the largest unscanned gap |
| `tilt_deg`, `axis_x/y/z` | Stem lean |
| `message` | Warnings or the reason a fit failed |
| `cluster_id` | Segmentation cluster (differs from `tree_id` when merged trees were split apart) |
| `basal_area_m2`, `basal_area_ci_low_m2`, `basal_area_ci_high_m2` | Basal area and its interval |

### `trees.dxf`: CAD drawing (metres or feet)

Each tree's DBH is drawn as a **circle with diameter = DBH, lying flat in a horizontal plane**. `-E dxf.z_mode=...` sets where that plane is:

| `z_mode` | The circle's z |
|---|---|
| `elevation` (default) | The actual elevation of breast height (`bh_elevation`), so circles float at true height above the terrain |
| `height` | Breast height above ground (e.g. 1.3) for every tree |
| `plan` | 0, a flat 2D plan |

Layers:

| Layer | Content |
|---|---|
| `FIP_DBH` | DBH circles of `ok` trees (green) |
| `FIP_DBH_WARN` | DBH circles of `warning` trees (yellow) |
| `FIP_CI` | Dashed circles at the lower and upper CI bounds |
| `FIP_CENTER` | Stem centre points |
| `FIP_LABEL` | Text such as `T12 34.2cm [33.1-35.3]` |
| `FIP_AXIS` | Line from the ground to breast height (elevation mode only) |

Each DBH circle also carries extended data (XDATA, application id `FIP`): the tree id, DBH, CI, status and units. In feet the drawing's units are set to feet (or US survey feet for `us_ft`), and labels show inches. Other options: `include_ci`, `include_labels`, `include_axis`, `text_height`, `units`.

### `trees.geojson`: for GIS (`-e geojson`)

One feature per tree, with DBH, CI, status and metric properties, for QGIS, ArcGIS or web maps:
- `-E geojson.geometry=point` (default): a point at `[x, y, bh_elevation]`
- `-E geojson.geometry=circle`: the DBH cross-section as a polygon, so stems draw to scale on a map

The input's CRS is recorded (override with `-E geojson.crs=EPSG:xxxx`). Projected coordinates are written as they are, which QGIS and GDAL read correctly.

### `trees.laz`: segmented point cloud (`-e points`)

Every point of every tree, at its **real elevation**, with extra dimensions `tree_id` and `height_above_ground`. Open it in CloudCompare or QGIS and colour by `tree_id` to check the segmentation against the table. Format: `-E points.format=laz|las|ply`. The input CRS is written into LAS/LAZ files. It contains the height slice that was segmented (`segment.min_z`..`max_z`), not the whole input cloud.

### `run.json` and `trees.prj`

Every run also writes:
- **`run.json`**: the software version, time, input files, input and output units, CRS, all parameters, the result summary, and every output file with its size and SHA-256 checksum, so a result can be traced and verified later
- **`trees.prj`**: the coordinate system as WKT (when known), which lets GIS software place `trees.csv` and the DXF correctly

### CSV options

`-E csv.delimiter=comma|semicolon|tab`, `-E csv.decimal=point|comma` (use `semicolon` + `comma` for Excel in German, French and similar locales), `-E csv.precision=4`, `-E csv.include_failed=true|false`, `-E csv.units=auto|m|ft|us_ft`.

### `fip.log`

A detailed log of the run: every stage, its timing, warnings, and for each failed tree the reason. Unexpected errors include a full traceback. The web app logs to `fip_output/app.log` when run from source. Installed copies log to `%LOCALAPPDATA%\LiDARTreeInventory\logs` on Windows and `~/.local/state/lidar-tree-inventory/logs` on Linux.

---

## 7. Configuration

Every setting has a sensible default. To change them for the command line, copy [`config/inventory.yaml`](config/inventory.yaml), edit it, and pass it with `-c`. That file documents every key. The main ones:

```yaml
ground:
  cloth_resolution: 0.5     # CSF grid (m)
  rigidness: 3              # 1 steep ... 3 flat
  refine_tolerance: 0.15    # remove "ground" points this far above the terrain (stem bases)
segment:
  k: 150                    # clustering neighbours
  beta: 0.5                 # cluster merging
  min_z: 1.0                # height slice used to find trees
  max_z: 4.0
fit:
  breast_height: 1.3
  band_half_width: 0.25
  confidence: 0.95
  ci_method: analytic       # or bootstrap
  model_sd: 0.005           # systematic uncertainty (m); calibrate it
  split_stems: true         # recover trees merged by segmentation
```

Invalid values are rejected with a message naming the setting and its valid range.

---

## 8. How the measurement works

1. **Ground.** The Cloth Simulation Filter separates ground points. A refinement step then removes stem bases that CSF labelled as ground. Without it, the terrain under each tree is lifted (about 0.4 m in tests), which moves the "1.3 m" band up the stem and biases DBH low on tapered stems.
2. **Height normalization.** Each point's height above the interpolated terrain is computed (inverse-distance weighting). The x and y coordinates are not changed.
3. **Tree segmentation.** Quickshift++ clusters a height slice into tree instances, as in the original paper. If a cluster contains several stems at breast height (merged trees), they are split apart, and duplicate estimates of one stem are removed.
4. **Stem fit at breast height.** Only points in the band (breast height ± half-width) are used. A 3-point circle RANSAC gives a robust first guess. A robust Levenberg–Marquardt fit (Geman–McClure kernel, so branches and clutter are down-weighted) then refines a slightly tilted cylinder: centre, lean in two directions, and radius. DBH = 2 × radius.
5. **Confidence interval.**
   - **analytic**: a *cluster-robust* covariance of the fit, using angular sectors of the stem as clusters, with a Student-t interval. Neighbouring LiDAR points aren't independent (bark texture, non-round stems, scan lines). Treating them as independent would make the interval far too narrow, and sector clustering accounts for that. It's instant.
   - **bootstrap**: refits the stem many times on resampled angular sectors and takes the percentile interval. Slower, with fewer assumptions.
   - Both methods add the **systematic uncertainty** `model_sd` in quadrature. This covers error the scan can't reveal: non-circular stems, taper, segmentation mistakes.
6. **Quality flags.** A tree becomes a *warning* when its arc coverage is low (seen from one side), its CI is wide, or its inlier ratio is low (clutter). It becomes *failed* when it can't be measured (too few band points, no valid circle, implausible lean or size). One tree failing never stops the run.

On synthetic plots with known truth (5 plots × 30 trees): all 150 trees found, no false detections, DBH RMSE about 0.25 cm, and the 95 % interval contained the true DBH for every tree. **Real scans have larger errors, so calibrate before relying on the intervals.**

---

## 9. Calibrating the confidence interval

The interval is only honest if the systematic uncertainty matches your data: your scanner, forest type and bark. Measure some trees in the field and let the software estimate it.

1. Measure DBH with a tape on 15–30 trees in the plot, and record their positions in **the same coordinate system as the point cloud**.
2. Save them as a CSV with columns `x`, `y` and `dbh` (in cm or m; the unit is detected automatically).
3. Run the inventory, then:
   ```bash
   fip calibrate out/trees.csv field.csv
   ```
   It reports the matched trees, bias, RMSE, the current CI coverage, and a **suggested `model_sd`**.
4. Re-run with `--model-sd <value>`, or set `fit.model_sd` in your config. In the web app, use the **Calibration** tab and press **Apply suggested value**.

If calibration reports a significant **bias**, check the breast height and the ground segmentation before trusting any intervals.

---

## 10. Plugins

New input formats, export formats and per-tree measurements can be added as plugins, without changing the program.

```bash
fip plugins --plugin-dir plugins                 # list what is available
fip inventory plot.laz --plugin-dir plugins           # adds girth columns to every export
fip app --plugin-dir plugins
```

The `plugins/` folder includes an example metric (`girth_metric.py`: stem circumference). A plugin is a `.py` file in a folder. You can also set `FIP_PLUGIN_PATH=/path/to/folder`, or install a package that uses the `forest_inventory_pipeline.plugins` entry point. A broken plugin is logged and skipped. See **[plugins/README.md](plugins/README.md)** for the API and examples of each type (reader, exporter, metric). Only load plugin folders you trust: plugins run as normal code.

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| **No trees found** | The height slice must contain breast height. Lower `segment.min_z` for short trees. Check that the cloud is in metres. Look at `fip.log` for the stage that found nothing |
| **Neighbouring trees merged into one** | Keep *Merged trees* on and lower the stem gap; lower `k` or `beta` |
| **One tree split into several** | Raise `k` or `beta` |
| **Many `failed` trees: "only N points in the breast-height band"** | Sparse stems: widen the band (`band_half_width`) or lower `min_band_points` |
| **Many `warning: low arc coverage`** | The stems were scanned from one side, so the intervals are wider by design. Scan from more positions if you need tighter intervals |
| **Results are off by about 3.3×** | The cloud is in feet: use `--units ft` (or `us_ft`), or set *Point cloud units* in the app |
| **DBH consistently too small or too large** | Check the ground (`refine_tolerance`), check `breast_height`, and run [calibration](#9-calibrating-the-confidence-interval) |
| **ROS bag: "scans are in sensor frame(s)"** | See the [ROS bag table](#ros-bags-ros1-bag-ros2-folder-db3-mcap): use a registered topic, or `target_frame` |
| **ROS bag: "several PointCloud2 topics"** | Choose one with `-R topic=/name` |
| **"vertical extent is only … m"** | The file has no stems, or isn't in metres |
| **Web app doesn't open** | Open `http://localhost:8501` yourself (the console shows the actual port). Use `fip app --port 8600` if a firewall blocks the default |
| **Windows: SmartScreen warning** | The installer is unsigned: *More info → Run anyway* |
| **Linux: "glibc … is too old"** | Use Ubuntu 22.04+ / Debian 12+, or [install from source](#from-source-for-development-or-the-latest-code) |
| **"coordinates are longitude/latitude"** | Reproject the file to a projected CRS (e.g. UTM) with PDAL or LAStools first |
| **Large file, out of memory** | Cut out the plot with `--crop` (LAS/LAZ are cropped while reading) |
| **Tiles: "different coordinate systems / units"** | Tiles from different projects can't be merged; process them separately |
| **Anything else** | Run `fip self-test`, and check `fip.log` or the app's **Log** tab. When reporting a problem, include the log |

---

## 12. Development

```bash
pip install -e ".[test,app,las]" rosbags ruff
pytest -q tests                  # 90 tests: fitting, CIs, readers, exports, plugins, end-to-end, web app
ruff check python tests
```

| Folder | Contents |
|---|---|
| `python/forest_inventory_pipeline/` | The package: `inventory.py` (pipeline stages), `traits/dbh/robust_fit.py` (stem fit and CI), `app/` (web app), `plugins/` (plugin API and built-ins), `fip.py` (CLI) |
| `cpp/` | The quickshift++ C++ extension |
| `plugins/` | Example external plugins |
| `config/` | Settings files |
| `packaging/` | Installer build: PyInstaller spec, Inno Setup script, Linux installer. See [packaging/README.md](packaging/README.md) |
| `tests/` | pytest suite |
| `.claude/agents/` | Claude Code subagents for testing, measurement review, plugin building and data inspection |

**CI**: every push runs the tests on Python 3.10 and 3.12. **Releases**: pushing a `vX.Y.Z` tag builds both installers on GitHub Actions, tests them (test suite, frozen self-test, a silent install and uninstall), and publishes a GitHub Release with checksums.

---

## Citation

This software extends the code of the following paper. If you use it in academic work, please cite:

```bibtex
@inproceedings{malladi2024icra,
  author = {M.V.R. Malladi and T. Guadagnino and L. Lobefaro and M. Mattamala and H. Griess and J. Schweier and N. Chebrolu and M. Fallon and J. Behley and C. Stachniss},
  title = {{Tree Instance Segmentation and Traits Estimation for Forestry Environments Exploiting LiDAR Data Collected by Mobile Robots}},
  booktitle = icra,
  year = 2024,
  codeurl = {https://github.com/PRBonn/forest_inventory_pipeline}
}
```

Original research code: [PRBonn/forest_inventory_pipeline](https://github.com/PRBonn/forest_inventory_pipeline) · paper: [PDF](https://www.ipb.uni-bonn.de/pdfs/malladi2024icra.pdf) · test data: [DigiForests dataset](https://github.com/PRBonn/digiforests).

## License

MIT. See [LICENSE](LICENSE).
