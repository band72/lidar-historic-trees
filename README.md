<div align="center">
  <h1>Tree Instance Segmentation and Traits Estimation for Forestry Environment Exploiting LiDAR Data Collected by Mobile Robots</h1>
    <a href="#setup"><img src="https://img.shields.io/badge/Linux-FCC624?logo=linux&logoColor=black" /></a>
    <a href="#usage"><img src="https://img.shields.io/badge/python-3670A0?style=flat-square&logo=python&logoColor=ffdd54" /></a>
    <a href="https://www.ipb.uni-bonn.de/pdfs/malladi2024icra.pdf"><img src="https://img.shields.io/badge/Paper-pdf-<COLOR>.svg?style=flat-square" /></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square" /></a>

<p>
  <img src="doc/motivation.png" width="400"/>
</p>

</div>

## Setup

A docker image is provided and details are described in the [Docker](#docker) section below.

### Dependencies

This code has been developed and tested on Ubuntu 22.04 LTS using Python 3.10.

System dependencies:

- libegl1 libgl1 libgomp1 (required by Open3D)

```bash
sudo apt install libegl1 libgl1 libgomp1
```

- build-essential (Gives g++ make libc among others)

```bash
sudo apt install build-essential
```

CMake is not needed to build the package. This is handled by the python build system.

### forest_inventory_pipeline

After you clone the repository, installing it is a simple

```bash
pip install -v .
```

or

```bash
make [install] # install is the default target
```

### Docker

Please check the `docker/` folder.
A simple Dockerfile is provided that sets up the project in a base Ubuntu 22.04 image.
A `compose.yaml` file is provided for convenience as well.
This also has services to run some different experiments on data.

You can run a container with `forest_inventory_pipeline` setup by running the following from inside `docker/`

```bash
docker compose run fip
```

## Usage

The CLI entrypoint `fip` covers most of the relevant information needed to use the package.
Additionally, parameters are described and their default values are given in corresponding files under `config/`.

```bash
fip --help
fip instance --help
fip dbh --help
```

Example runs:

```bash
# relevant output of the following is an instance_segmented_cloud.ply
fip instance /data/input_cloud.ply --output /output -c config/instance.yaml
```

In case some stages have been run earlier, corresponding skip flags can be used.

```bash
# relevant output of the following is cylinders.pickle and tree_dbh.csv
fip dbh /data/input_cloud.ply --output output -c config/dbh.yaml --skip_gs --skip_norm --skip_cluster
```

If the `--output` flag is specified, only then do results get written to disk.

### Example Experiments

To test and use the approach, you can use the provided Docker image and some simple experiment scripts in `scripts/evaluation`.
These scripts need the data in a slightly specific format detailed further in their documentation (use `--help` when running the cli).

Please see the [DigiForests dataset](https://github.com/PRBonn/digiforests) for some forestry data you can run the approach on.

To run an instance segmentation experiment, `cd` into the `docker/` folder, modify the `compose.yaml` file to have the appropriate data mount, and then run:

```bash
docker compose run fip_instance
```

The results will get printed to the console.

To run a DBH estimation experiment, `cd` into the `docker/` folder and then run:

```bash
docker compose run fip_dbh
```

## Tree inventory: DBH with confidence intervals

`fip inventory` and the web app (`fip app`) extend the pipeline into a full inventory tool. They find trees, fit each stem at breast height, and report **DBH with a confidence interval**. They also export CSV, DXF and other formats through plugins.

```bash
pip install -e ".[app,las]"          # web app + LAS/LAZ input; add `rosbags` for ROS bags
fip demo-data demo/plot.ply          # synthetic plot with known DBH (+ demo/plot_truth.csv)
fip inventory demo/plot.ply -o out   # -> out/trees.csv, out/trees.dxf, out/fip.log
fip app                              # interactive interface at http://localhost:8501
```

### How it works

1. **Read.** PLY, PCD, XYZ, LAS/LAZ, NPY, text/CSV, and ROS1/ROS2 bags (`sensor_msgs/PointCloud2`).
   Bags must contain clouds that are already in a world frame, such as a SLAM-registered topic. Alternatively, pass `-R target_frame=map` and the reader registers each scan using `/tf` and `/tf_static` from the bag. Raw scans in a sensor frame are refused, because stacking them without poses would smear the stems.
2. **Ground.** CSF, followed by a refinement step that removes stem bases from the terrain model. Without it, the terrain is lifted under trees by about 0.4 m, which moves the "1.3 m" band up the stem and biases DBH on tapered stems.
3. **Trees.** quickshift++ instance segmentation, as in the paper.
4. **Stems.** Each stem is fitted in a band at breast height (1.3 m ± 0.25 m by default) in two steps: 3-point circle RANSAC, then a robust (Geman-McClure) Levenberg-Marquardt cylinder fit. The cylinder has 5 parameters (center, tilt, radius), so there is no gauge freedom and the covariance is well-defined.
5. **Confidence interval.** DBH ± CI comes from one of two methods:
   - `analytic` (the default and fast): a cluster-robust sandwich covariance that uses angular sectors of the stem as clusters. This accounts for correlated residuals from bark and non-circular stems.
   - `bootstrap`: resamples angular sectors. Slower, but makes fewer assumptions.

   Both methods add a **systematic term `model_sd`** in quadrature, for error that the point cloud itself cannot reveal. Calibrate it against field data with `fip calibrate trees.csv field.csv`.
6. **Merged trees.** Segmentation can merge a small tree into a large neighbour's cluster, and fitting would then silently measure only the bigger stem. With `fit.split_stems` (on by default), separate stems in the breast-height band are found with DBSCAN (`fit.stem_gap`) and each is fitted. Duplicate estimates of one stem, split across two clusters, are removed.
7. **Quality flags.** Each tree is marked `ok`, `warning` or `failed`, and the reason is recorded. Warnings cover one-sided scans (arc coverage), wide CIs and clutter. A failed tree never aborts the run.

Results on synthetic demo plots with known truth (5 plots × 30 trees): all 150 trees found, no false positives, DBH RMSE about 0.25 cm, and the 95% CI covers the true DBH for every tree. Real data will have larger errors, so calibrate.

### Outputs

- **CSV**: one row per tree. Columns are `x, y, ground_z, bh_elevation` (real-world elevation of the DBH plane), `dbh, ci_low, ci_high, dbh_se` and quality metrics, followed by plugin columns such as basal area.
- **DXF**: one circle per tree, with diameter equal to the DBH, lying in a horizontal plane. The circle is placed at the actual DBH elevation (`-E dxf.z_mode=elevation`), at height above ground (`height`), or flat at z = 0 (`plan`). The drawing also contains dashed CI circles, labels, the stem line from the ground to breast height, and XDATA (app id `FIP`). Units are metres.

### Web app

The sidebar has three sections:

- **Point cloud**: a file path (including bag files and folders), an upload, or the demo plot.
- **Find trees**: ground and segmentation parameters. These steps are slow, so they run only when you press the button.
- **Fit stems (live)**: sliders for breast height, band, confidence level, CI method, systematic uncertainty and quality thresholds. Every change refits all trees instantly.

The main area has five tabs:

- **Map**: tree positions, with status shown by color, shape and label.
- **Tree detail**: the breast-height slice with the fitted circle and CI rings, plus a 3D view of the tree.
- **Table & export**: the results table, with downloads for every exporter.
- **Calibration**: upload field DBH measurements and apply the suggested `model_sd`.
- **Log**.

### Plugins

Readers, exporters and per-tree metrics are plugins. CSV, DXF and the basal-area metric are built-in plugins. See [`plugins/README.md`](plugins/README.md), and the GeoJSON example there:

```bash
fip plugins --plugin-dir plugins
fip inventory cloud.laz --plugin-dir plugins -e csv -e dxf -e geojson
```

### Logging and errors

Every run writes a detailed log (`out/fip.log`, or `fip_output/app.log` for the app). The log includes the source module and line for each entry, and a full traceback for any unexpected error. Invalid parameters, unreadable inputs and stage failures exit with code 2 and a clear message. Use `--log-level debug` for more console output.

## Publication

If you use our code in your academic work, please cite the corresponding [paper](https://www.ipb.uni-bonn.de/pdfs/malladi2024icra.pdf):

```
@inproceedings{malladi2024icra,
  author = {M.V.R. Malladi and T. Guadagnino and L. Lobefaro and M. Mattamala and H. Griess and J. Schweier and N. Chebrolu and M. Fallon and J. Behley and C. Stachniss},
  title = {{Tree Instance Segmentation and Traits Estimation for Forestry Environments Exploiting LiDAR Data Collected by Mobile Robots}},
  booktitle = icra,
  year = 2024,
  codeurl = {https://github.com/PRBonn/forest_inventory_pipeline}
}
```

## License

This project is free software made available under the MIT license. For details, see the [LICENSE](LICENSE) file.
