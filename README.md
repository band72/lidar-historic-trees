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

Results can be explored in an interactive web app and exported as **CSV** (spreadsheets), **DXF** (CAD: DBH circles at true elevation) or **GeoJSON** (GIS).

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

- **Demo plot**: a synthetic plot. Choose the number of trees and a random seed.
- **File / bag path**: type the path to a file on this computer (any [supported format](#5-input-data)) or a ROS2 bag folder. Large files are best opened this way. For ROS bags, a *ROS bag options* panel appears, with topic, target frame, every n-th message and more.
- **Upload**: drag a file into the browser, up to 4 GB.

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
fip inventory INPUT [options]
```

| Option | Description |
|---|---|
| `-o, --output DIR` | Output folder (default `fip_output`). It holds the exports and `fip.log` |
| `-c, --config FILE` | YAML/JSON settings file, see [Configuration](#7-configuration) |
| `-e, --export NAME` | Exporter to run; repeatable. Default: `-e csv -e dxf` |
| `-E, --export-option NAME.KEY=VALUE` | Exporter option, e.g. `-E dxf.z_mode=plan`; repeatable |
| `--reader NAME` | Force a reader (normally picked from the file extension) |
| `-R, --reader-option KEY=VALUE` | Reader option, e.g. `-R topic=/points`; repeatable |
| `--plugin-dir DIR` | Load extra plugins from this folder; repeatable |
| `--breast-height M` | Override the breast height |
| `--confidence P` | Override the confidence level, e.g. `0.9` |
| `--ci-method analytic\|bootstrap` | Override the CI method |
| `--model-sd M` | Override the systematic uncertainty (metres) |
| `--log-level trace\|debug\|info\|warn` | Console verbosity (the log file always has full detail) |

Examples:

```bash
# all three exporters, DXF as a flat 2D plan, GeoJSON tagged with a CRS
fip inventory plot.laz -o out --plugin-dir plugins -e csv -e dxf -e geojson \
    -E dxf.z_mode=plan -E geojson.crs=EPSG:25832

# US breast height, 90 % intervals, bootstrap, calibrated systematic error
fip inventory plot.laz -o out --breast-height 1.37 --confidence 0.9 --ci-method bootstrap --model-sd 0.012

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
fip calibrate out/trees.csv field_measurements.csv [--dbh-unit auto|m|cm] [--max-distance 0.5] [--current-model-sd 0.005]
```

See [section 9](#9-calibrating-the-confidence-interval).

---

## 5. Input data

### Point cloud files

| Format | Extensions | Notes |
|---|---|---|
| PLY / PCD / XYZ | `.ply .pcd .xyz .xyzn .xyzrgb .pts` | Read with Open3D |
| LAS / LAZ | `.las .laz` | Scaled coordinates are applied. LAZ is supported in the installers and with the `las` extra |
| NumPy | `.npy` | An `N × 3` (or wider) array |
| Text | `.txt .csv .asc` | The first three numeric columns are x, y, z. Header lines are skipped and the delimiter is detected automatically |

Requirements:
- **Units must be metres.** Every setting assumes metres. A cloud in millimetres or feet gives nonsense; convert it first.
- The cloud should cover the stems from the ground up to at least 1.5 m. Terrestrial, mobile (backpack/robot/handheld) and dense UAV scans work. Typical airborne LiDAR is too sparse on the stems.
- One plot per file. Very large clouds (more than about 50 million points) are best cropped or thinned first.

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

All lengths are in metres. Failed trees are included, with a reason, unless you pass `-E csv.include_failed=false`.

| Column | Meaning |
|---|---|
| `tree_id` | Tree number |
| `status` | `ok`, `warning` (measured, but check `message`) or `failed` (not measurable; see `message`) |
| `x`, `y` | Stem centre at breast height, in the input coordinate system |
| `ground_z` | Terrain elevation under the stem |
| `breast_height` | Height used (e.g. 1.3) |
| `bh_elevation` | **Absolute elevation of the DBH plane** = `ground_z + breast_height` |
| `dbh`, `dbh_cm` | DBH in m and in cm |
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

### `trees.dxf`: CAD drawing (metres)

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

Each DBH circle also carries extended data (XDATA, application id `FIP`): the tree id, DBH, CI and status. Other options: `include_ci`, `include_labels`, `include_axis`, `text_height`.

### `trees.geojson` (example plugin)

Point features `[x, y, bh_elevation]` with DBH, CI and status properties, for QGIS or ArcGIS. Enable it with `--plugin-dir plugins -e geojson`. Add `-E geojson.crs=EPSG:xxxx` if your coordinates are georeferenced.

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
fip inventory plot.laz --plugin-dir plugins -e geojson
fip app --plugin-dir plugins
```

A plugin is a `.py` file in a folder. You can also set `FIP_PLUGIN_PATH=/path/to/folder`, or install a package that uses the `forest_inventory_pipeline.plugins` entry point. A broken plugin is logged and skipped. See **[plugins/README.md](plugins/README.md)** for the API and examples of each type (reader, exporter, metric). Only load plugin folders you trust: plugins run as normal code.

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| **No trees found** | The height slice must contain breast height. Lower `segment.min_z` for short trees. Check that the cloud is in metres. Look at `fip.log` for the stage that found nothing |
| **Neighbouring trees merged into one** | Keep *Merged trees* on and lower the stem gap; lower `k` or `beta` |
| **One tree split into several** | Raise `k` or `beta` |
| **Many `failed` trees: "only N points in the breast-height band"** | Sparse stems: widen the band (`band_half_width`) or lower `min_band_points` |
| **Many `warning: low arc coverage`** | The stems were scanned from one side, so the intervals are wider by design. Scan from more positions if you need tighter intervals |
| **DBH consistently too small or too large** | Check the ground (`refine_tolerance`), check `breast_height`, and run [calibration](#9-calibrating-the-confidence-interval) |
| **ROS bag: "scans are in sensor frame(s)"** | See the [ROS bag table](#ros-bags-ros1-bag-ros2-folder-db3-mcap): use a registered topic, or `target_frame` |
| **ROS bag: "several PointCloud2 topics"** | Choose one with `-R topic=/name` |
| **"vertical extent is only … m"** | The file has no stems, or isn't in metres |
| **Web app doesn't open** | Open `http://localhost:8501` yourself (the console shows the actual port). Use `fip app --port 8600` if a firewall blocks the default |
| **Windows: SmartScreen warning** | The installer is unsigned: *More info → Run anyway* |
| **Linux: "glibc … is too old"** | Use Ubuntu 22.04+ / Debian 12+, or [install from source](#from-source-for-development-or-the-latest-code) |
| **Anything else** | Run `fip self-test`, and check `fip.log` or the app's **Log** tab. When reporting a problem, include the log |

---

## 12. Development

```bash
pip install -e ".[test,app,las]" rosbags ruff
pytest -q tests                  # 68 tests: fitting, CIs, readers, exports, plugins, end-to-end, web app
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
