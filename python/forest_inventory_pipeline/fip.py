# MIT License
#
# Copyright (c) 2024 Meher Malladi, Luca Lobefaro, Tiziano Guadagnino, Cyrill Stachniss
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
from pathlib import Path
from typing_extensions import Annotated
from typing import Optional, Callable
from rich.pretty import pretty_repr

from .cloud import Cloud
from .render import render
from .utils import logger
from .utils.decorators import timer
from .utils.io import load_yaml_or_json
from .preprocess import segment_ground, normalize_height

import typer


def log_config(callable: Callable):
    def wrapper(*args, **kwargs):
        config = callable(*args, **kwargs)
        logger.info(f"Read in config: {pretty_repr(config)}")
        return config

    return wrapper


load_yaml_or_json = log_config(load_yaml_or_json)


@timer(level="info", name="Segmenting instances")
def _segment_instance(
    cloud: Cloud,
    min_z: float | None = None,
    max_z: float | None = None,
    voxel_size: float | None = None,
    upsample: bool = False,
    **quickshift_config,
):
    from .utils.upsample import upsample as _upsample
    from .cluster import project_2d_then_cluster_3d
    from .clip_z import clip_z

    cloud = clip_z(cloud, min_z=min_z, max_z=max_z)
    if voxel_size:
        high_res_cloud = cloud.clone()
        cloud = cloud.voxel_down_sample(voxel_size)
    clustered_cloud = project_2d_then_cluster_3d(cloud, **quickshift_config)
    if upsample and voxel_size:
        upsampled_cloud = _upsample(clustered_cloud, high_res_cloud, low_res_voxel_size=voxel_size)
        logger.info("upsampled clustered cloud back to original resolution")
        clustered_cloud = upsampled_cloud
    return clustered_cloud


#### Public

cli = typer.Typer(name="Forest Inventory Pipeline")


@cli.command()
def instance(
    input_fp: Annotated[
        Optional[Path],
        typer.Argument(
            metavar="INPUT",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Input cloud to process. If passed, will be used if ground segmentation is not being skipped. It is an error to not pass this if output dir is also not specified or the output dir does not contain the appropriate stage to start processing from.",
        ),
    ] = None,
    config_fp: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="a config file which can be used for individual processing steps.",
        ),
    ] = None,
    output_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            file_okay=False,
            dir_okay=True,
            help="directory used to cache results. intermediate process files like ground segmented cloud, normalized cloud are written here as well. they can be used to skip stages in subsequent runs",
        ),
    ] = None,
    visualize: Annotated[
        bool, typer.Option("--visualize", "-v", help="visualize the clustered result.")
    ] = False,
    skip_ground_seg: Annotated[
        bool,
        typer.Option(
            "--skip_gs",
            help="Skip ground segmentation. Only skipped if ground_cloud.ply cloud exists inside the output dir.",
        ),
    ] = False,
    skip_normalize: Annotated[
        bool,
        typer.Option(
            "--skip_norm",
            help="Skip height normalization. Only skipped if height_normalized.ply cloud exists inside the output dir. Will not be skipped if ground segmantation has been run in the current process, so you have to specify both options.",
        ),
    ] = False,
):
    """
    Preprocess the cloud (ground segmentation and height normalization) and then instance segment the cloud.
    """
    config: dict = load_yaml_or_json(config_fp) if config_fp else {}  # type:ignore
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    # define all intermediate file paths
    gc_fp = output_dir / "ground_cloud.ply" if output_dir else None
    ngc_fp = output_dir / "non_ground_cloud.ply" if output_dir else None
    normalized_fp = output_dir / "normalized_cloud.ply" if output_dir else None
    instance_seg_fp = output_dir / "instance_segmented_cloud.ply" if output_dir else None

    # ground seg
    force_normalize = False
    if skip_ground_seg and gc_fp and gc_fp.exists() and ngc_fp and ngc_fp.exists():
        ground_cloud = Cloud.load(gc_fp)
        non_ground_cloud = Cloud.load(ngc_fp)
    else:
        if input_fp is None:
            if skip_ground_seg:
                raise Exception(
                    "Necessary files for skipping ground segmentation do no exist in the config dir if passed. Please start from processing an input point cloud"
                )
            else:
                raise Exception("Please pass an input point cloud")
        input_cloud: Cloud = Cloud.load(input_fp)
        ground_seg_config = config.get("ground_seg", {})
        ground_cloud, non_ground_cloud = segment_ground(input_cloud, **ground_seg_config)
        force_normalize = True
        if gc_fp and ngc_fp:
            ground_cloud.write(gc_fp)
            non_ground_cloud.write(ngc_fp)

    # normalization
    if skip_normalize and normalized_fp and normalized_fp.exists() and not force_normalize:
        normalized_cloud = Cloud.load(normalized_fp)
    else:
        normalization_config = config.get("normalize", {})
        normalized_cloud = normalize_height(
            non_ground_cloud=non_ground_cloud, ground_cloud=ground_cloud, **normalization_config
        )
        if normalized_fp:
            normalized_cloud.write(normalized_fp)

    cluster_config = config.get("cluster", {})
    clustered_cloud = _segment_instance(normalized_cloud, **cluster_config)
    if instance_seg_fp:
        clustered_cloud.write(instance_seg_fp)

    if visualize:
        render([clustered_cloud.colorize_using_attribute("instance", random=True)])


@cli.command()
def dbh(
    input_fp: Annotated[
        Optional[Path],
        typer.Argument(
            metavar="INPUT",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="Input cloud to process. If passed, will be used if ground segmentation is not being skipped. It is an error to not pass this if output dir is also not specified or the output dir does not contain the appropriate stage to start processing from.",
        ),
    ] = None,
    config_fp: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            exists=True,
            file_okay=True,
            dir_okay=False,
            help="a config file which can be used for individual processing steps.",
        ),
    ] = None,
    output_dir: Annotated[
        Optional[Path],
        typer.Option(
            "--output",
            "-o",
            file_okay=False,
            dir_okay=True,
            help="directory used to cache results. intermediate process files like ground segmented cloud, normalized cloud are written here as well. they can be used to skip stages in subsequent runs",
        ),
    ] = None,
    visualize: Annotated[
        bool,
        typer.Option(
            "--visualize",
            "-v",
            help="visualize the cylinder fitting results. Needs input cloud to be passed as well.",
        ),
    ] = False,
    skip_ground_seg: Annotated[
        bool,
        typer.Option(
            "--skip_gs",
            help="Skip ground segmentation. Only skipped if ground_cloud.ply cloud exists inside the output dir.",
        ),
    ] = False,
    skip_normalize: Annotated[
        bool,
        typer.Option(
            "--skip_norm",
            help="Skip height normalization. Only skipped if height_normalized.ply cloud exists inside the output dir. Will not be skipped if ground segmantation has been run in the current process, so you have to specify both options.",
        ),
    ] = False,
    skip_cluster: Annotated[
        bool,
        typer.Option(
            "--skip_cluster",
            help="Skip clustering. Only skipped if clustered_cloud.ply cloud exists inside the output dir. Will not be skipped if earlier stages have been run.",
        ),
    ] = False,
):
    """
    Preprocess the cloud, clip the cloud to relevant height (config settings),
    cluster and then fit cylinders to estimate DBH.
    """
    config: dict = load_yaml_or_json(config_fp) if config_fp else {}  # type:ignore
    if visualize and not input_fp:
        raise Exception(
            "Please pass the path to the input cloud if you would like to visualize the results"
        )
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
    # define all intermediate file paths
    gc_fp = output_dir / "ground_cloud.ply" if output_dir else None
    ngc_fp = output_dir / "non_ground_cloud.ply" if output_dir else None
    normalized_fp = output_dir / "normalized_cloud.ply" if output_dir else None
    clustered_fp = output_dir / "clustered_cloud.ply" if output_dir else None
    cylinders_fp = output_dir / "cylinders.pickle" if output_dir else None
    dbh_csv_fp = output_dir / "tree_dbh.csv" if output_dir else None

    # ground seg
    force_process = False
    if skip_ground_seg and gc_fp and gc_fp.exists() and ngc_fp and ngc_fp.exists():
        ground_cloud = Cloud.load(gc_fp)
        non_ground_cloud = Cloud.load(ngc_fp)
    else:
        if input_fp is None:
            if skip_ground_seg:
                raise Exception(
                    "Necessary files for skipping ground segmentation do no exist in the config dir if passed. Please start from processing an input point cloud"
                )
            else:
                raise Exception("Please pass an input point cloud")
        input_cloud: Cloud = Cloud.load(input_fp)
        ground_seg_config = config.get("ground_seg", {})
        ground_cloud, non_ground_cloud = segment_ground(input_cloud, **ground_seg_config)
        force_process = True
        if gc_fp and ngc_fp:
            ground_cloud.write(gc_fp)
            non_ground_cloud.write(ngc_fp)

    # normalization
    if skip_normalize and normalized_fp and normalized_fp.exists() and not force_process:
        normalized_cloud = Cloud.load(normalized_fp)
    else:
        normalization_config = config.get("normalize", {})
        normalized_cloud = normalize_height(
            non_ground_cloud=non_ground_cloud, ground_cloud=ground_cloud, **normalization_config
        )
        force_process = True
        if normalized_fp:
            normalized_cloud.write(normalized_fp)

    # clustering
    cluster_config = config.get("cluster", {})
    if skip_cluster and clustered_fp and clustered_fp.exists() and not force_process:
        clustered_cloud = Cloud.load(clustered_fp)
    else:
        clustered_cloud = _segment_instance(normalized_cloud, **cluster_config)
        if clustered_fp:
            clustered_cloud.write(clustered_fp)

    # cylinder fitting
    from .traits.dbh import estimate_dbh, write_dbh_csv

    fitting_config = config.get("fitting", {})
    voxel_size = fitting_config.get("voxel_size", None) or cluster_config.get("voxel_size", None)
    ransac_iterations = fitting_config.get("ransac_iterations", 10000)
    xyz_dbh_list, cylinders = estimate_dbh(
        clustered_cloud, voxel_size=voxel_size, ransac_iterations=ransac_iterations
    )
    if dbh_csv_fp:
        write_dbh_csv(xyz_dbh_list, dbh_csv_fp)
    if cylinders_fp:
        import pickle

        with cylinders_fp.open("wb") as cfp_file:
            pickle.dump(cylinders, cfp_file)

    if visualize:
        if "input_cloud" not in locals():
            assert input_fp is not None, "input cloud path is needed"
            input_cloud = Cloud.load(input_fp)
        geoms = [input_cloud]  # type: ignore
        geoms.extend(cylinders)
        render([geoms])


#### Inventory: robust breast-height DBH with confidence intervals, plugins, exports


def _parse_kv(items: list[str] | None, what: str) -> dict[str, dict[str, str]]:
    """Parse repeated ``name.key=value`` options into {name: {key: value}}."""
    out: dict[str, dict[str, str]] = {}
    for item in items or []:
        if "=" not in item or "." not in item.split("=", 1)[0]:
            raise typer.BadParameter(f"{what} option must look like name.key=value, got {item!r}")
        lhs, value = item.split("=", 1)
        name, key = lhs.split(".", 1)
        out.setdefault(name, {})[key] = value
    return out


@cli.command()
def inventory(
    inputs: Annotated[list[Path], typer.Argument(metavar="INPUT...", exists=True, help="Point cloud file(s) (.las .laz .ply .pcd .npy .txt .csv ...), a folder of tiles, a ROS bag file or ROS2 bag folder. Several inputs are merged.")],
    output_dir: Annotated[Path, typer.Option("--output", "-o", file_okay=False, help="Output directory for exports, run.json and the log file.")] = Path("fip_output"),
    config_fp: Annotated[Optional[Path], typer.Option("--config", "-c", exists=True, dir_okay=False, help="YAML/JSON with sections ground, segment, fit (keys = parameter names, metres).")] = None,
    export: Annotated[list[str], typer.Option("--export", "-e", help="Exporter(s) to run; repeatable: csv, dxf, geojson, points, ... (see `fip plugins`).")] = ["csv", "dxf"],
    export_option: Annotated[Optional[list[str]], typer.Option("--export-option", "-E", help="Exporter option name.key=value, e.g. dxf.z_mode=plan. Repeatable.")] = None,
    reader: Annotated[Optional[str], typer.Option("--reader", help="Force a reader by name (default: by file extension).")] = None,
    reader_option: Annotated[Optional[list[str]], typer.Option("--reader-option", "-R", help="Reader option key=value, e.g. topic=/cloud_registered. Repeatable.")] = None,
    plugin_dir: Annotated[Optional[list[Path]], typer.Option("--plugin-dir", help="Extra plugin folder(s) to load.")] = None,
    units: Annotated[str, typer.Option("--units", "-u", help="Length unit of the input: auto (from the LAS header, else m), m, ft or us_ft. Also the unit of --breast-height, --model-sd and --crop.")] = "auto",
    output_units: Annotated[Optional[str], typer.Option("--output-units", help="Length unit of the exports: m, ft or us_ft (default: the input's unit).")] = None,
    crop: Annotated[Optional[str], typer.Option("--crop", help="Only process points inside xmin,ymin,xmax,ymax (input coordinates). LAS is cropped while reading.")] = None,
    ground_source: Annotated[Optional[str], typer.Option("--ground-source", help="auto (use the file's ground classification if present), csf or classification.")] = None,
    crs: Annotated[Optional[str], typer.Option("--crs", help="Coordinate system to record in the outputs, e.g. EPSG:25832 (overrides the file header).")] = None,
    tile: Annotated[Optional[float], typer.Option("--tile", help="Process a large cloud as square tiles of this size (m, at least 20), each with an overlap buffer, and write one result. LAS/LAZ are cropped while reading, so memory is that of one tile.")] = None,
    tile_overlap: Annotated[float, typer.Option("--tile-overlap", help="Buffer around each tile (m, at least 5, less than half the tile): a tree is kept by the tile containing its stem, measured with this much context.")] = 10.0,
    keep_noise: Annotated[bool, typer.Option("--keep-noise", help="Keep points classified as noise (ASPRS 7/18).")] = False,
    breast_height: Annotated[Optional[float], typer.Option(help="Override fit.breast_height, in the input unit (default 1.3 m; 4.5 ft for feet).")] = None,
    confidence: Annotated[Optional[float], typer.Option(help="Override fit.confidence, e.g. 0.95.")] = None,
    ci_method: Annotated[Optional[str], typer.Option(help="Override fit.ci_method: analytic or bootstrap.")] = None,
    model_sd: Annotated[Optional[float], typer.Option(help="Override fit.model_sd, in the input unit; calibrate with `fip calibrate`.")] = None,
    complete_partial: Annotated[bool, typer.Option("--complete-partial/--no-complete-partial", help="Complete stems seen from one side by measuring over a taller window (fit.complete_partial_stems).")] = False,
    profile: Annotated[Optional[str], typer.Option("--profile", "-p", help="Site profile to start from: Default, 'Roadside / Farmstead', Shrubs, 'Isolated Trees', 'Clear Forest', 'Tall Wide Trees', 'Large DBH Trees', or one you saved in the app. A --config file and other options override its values.")] = None,
    scanner: Annotated[Optional[str], typer.Option("--scanner", help="Scanner profile: 'Vehicle (mobile lidar)' (default), 'Backpack / handheld', 'Tripod (terrestrial)' or 'Drone (UAV)' (a unique start, e.g. drone, is enough). Sets the trunk point density the site profile scales.")] = None,
    exclusions_fp: Annotated[Optional[Path], typer.Option("--exclusions", exists=True, help="Reapply a saved review: exclusions.json from an earlier run (or its folder). Trees are matched by stem position, so ids may differ between runs.")] = None,
    log_level: Annotated[str, typer.Option(help="Console log level: trace, debug, info, warn.")] = "info",
):
    """
    Full inventory: read, ground, height normalization, tree segmentation, breast-height DBH
    with confidence intervals, metric plugins, exports (CSV, DXF, ...) and run.json.
    """
    import sys
    from .errors import FIPError
    from . import exclusions as excl_mod
    from . import inventory as inv
    from .plugins import load_plugins, registry
    from .traits.dbh.robust_fit import FitParams
    from .units import US_BREAST_HEIGHT_M, is_feet, normalize_unit, to_metres

    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "fip.log"
    logger.configure(level=log_level, log_file=log_file)
    logger.info(f"fip inventory {' '.join(map(str, inputs))} -> {output_dir} (log: {log_file})")
    try:
        load_plugins(extra_dirs=plugin_dir or [])
        config: dict = load_yaml_or_json(config_fp) if config_fp else {}  # type: ignore
        unknown = set(config) - {"ground", "segment", "fit"}
        if unknown:
            raise typer.BadParameter(f"unknown config section(s) {sorted(unknown)}; expected ground, segment, fit")
        if profile or scanner:
            from . import profiles

            profile = profiles.get(profile or profiles.DEFAULT).name
            scanner = profiles.get_scanner(scanner or profiles.DEFAULT_SCANNER).name
            config = profiles.merged(profile, config, scanner)
            logger.info(
                f"site profile: {profile}; scanner: {scanner}"
                + (f" (overridden by {config_fp})" if config_fp else "")
            )
        try:
            req_unit = "auto" if units == "auto" else normalize_unit(units)
            out_unit_opt = normalize_unit(output_units) if output_units else None
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        ground_cfg = dict(config.get("ground", {}))
        if ground_source:
            ground_cfg["source"] = ground_source
        fit_cfg = dict(config.get("fit", {}))
        for key, val in dict(confidence=confidence, ci_method=ci_method).items():
            if val is not None:
                fit_cfg[key] = val
        if complete_partial:
            fit_cfg["complete_partial_stems"] = True
        try:
            gp = inv.GroundParams(**ground_cfg)
            sp = inv.SegmentParams(**config.get("segment", {}))
            FitParams(**fit_cfg)  # validate now; unit-dependent values are set after reading
        except TypeError as exc:
            raise typer.BadParameter(f"bad config key: {exc}") from exc
        # validate exporters and their options before any heavy processing
        export_opts = _parse_kv(export_option, "export")
        for name in set(export) | set(export_opts):
            registry.exporter(name).resolve_options(export_opts.get(name, {}))
        stray = set(export_opts) - set(export)
        if stray:
            logger.warning(f"options given for exporter(s) {sorted(stray)} that are not in --export; ignored")
        reader_opts = {}
        for item in reader_option or []:
            if "=" not in item:
                raise typer.BadParameter(f"reader option must be key=value, got {item!r}")
            k, v = item.split("=", 1)
            reader_opts[k] = v

        tiles_info = None
        if tile is not None:
            from . import tiling

            if crop:
                raise typer.BadParameter("use --crop or --tile, not both")
            tiling.plan_tiles((0.0, 0.0, 1.0, 1.0), tile, tile_overlap)  # check the sizes first
            meta = None if reader else tiling.input_metadata(inputs)
            probe = None if reader else tiling.cloud_extent(inputs, req_unit)
            if meta is not None and probe is not None:
                cloud = inv.LoadedCloud(
                    xyz=None, classification=None, units=probe[1], crs=meta["crs"],  # type: ignore[arg-type]
                    crs_wkt=meta["crs_wkt"], sources=meta["sources"],
                )
            else:  # cannot be cropped while reading: load once, run_tiled reuses it
                cloud = inv.load_cloud(inputs, reader=reader, units=req_unit, keep_noise=keep_noise, **reader_opts)
        else:
            cloud = inv.load_cloud(inputs, reader=reader, units=req_unit, crop=crop, keep_noise=keep_noise, **reader_opts)
        in_unit = cloud.units
        out_unit = out_unit_opt or in_unit
        if breast_height is not None:
            fit_cfg["breast_height"] = float(to_metres(breast_height, in_unit))
        elif is_feet(in_unit) and "breast_height" not in fit_cfg:
            fit_cfg["breast_height"] = US_BREAST_HEIGHT_M
            logger.info("units are feet: using the US breast height of 4.5 ft (1.3716 m)")
        if model_sd is not None:
            fit_cfg["model_sd"] = float(to_metres(model_sd, in_unit))
        fp = FitParams(**fit_cfg)
        for issue in inv.check_band_inside_slice(fp, sp):
            logger.warning(issue)

        if tile is not None:
            records, tiles_info = tiling.run_tiled(
                inputs, tile, tile_overlap, gp, sp, fp, reader=reader, units=req_unit,
                keep_points="points" in export, keep_noise=keep_noise, reader_options=reader_opts,
                cloud=cloud if cloud.xyz is not None else None,
            )
            used = [t for t in tiles_info if not t["skipped"]]
            if used:
                peak = max(used, key=lambda t: t["points"])
                logger.info(
                    f"{len(used)} of {len(tiles_info)} tiles processed; largest {peak['name']} "
                    f"with {peak['points']:,} points"
                )
        else:
            records = inv.process_cloud(
                cloud.xyz, cloud.classification, gp, sp, fp, keep_points="points" in export
            )
        excluded: dict[int, str] = {}
        added: dict[int, str] = {}
        if exclusions_fp:
            review = excl_mod.load(exclusions_fp)
            excluded, _ = excl_mod.match(records, review)
            added, _ = excl_mod.match(records, review, action=excl_mod.ADD)
            records = inv.apply_additions(inv.apply_exclusions(records, excluded), added)
        ctx = inv.context_for(
            "; ".join(cloud.sources), fp, units=out_unit,
            crs=crs or cloud.crs, crs_wkt=None if crs and crs != cloud.crs else cloud.crs_wkt,
            ground=gp, segment=sp, input_units=in_unit,
            site_profile=profile,
            scanner_profile=scanner,
            excluded_by_reviewer=sorted(excluded),
            added_by_reviewer=sorted(added),
            exclusions_file=str(exclusions_fp) if exclusions_fp else None,
            **({"tiles": tiles_info, "tile_size_m": tile, "tile_overlap_m": tile_overlap}
               if tiles_info is not None else {}),
        )
        written = inv.export_records(records, output_dir, export, ctx, options=export_opts)
        s = inv.summarize(records)
        logger.info(
            f"done: {s['ok']} ok, {s['warning']} warnings, {s['failed']} failed, {s['excluded']} excluded"
            + (f", {s['added']} added by reviewer" if s["added"] else "") + "; "
            f"mean DBH {s['mean_dbh'] * 100:.1f} cm; basal area {s['basal_area_m2']:.2f} m^2"
        )
        why = inv.rejection_summary(records)
        if why:
            logger.info(
                f"rejected {s['failed']} objects; why: "
                + ", ".join(f"{w['reason']} {w['count']}" for w in why)
                + " (an object can have several reasons; see trees.csv 'message')"
            )
        for w in written:
            logger.info(f"wrote {w}")
    except (FIPError, typer.BadParameter) as exc:
        logger.error(str(exc))
        logger.debug("details", exc_info_str(exc))
        sys.exit(2)
    except Exception:
        logger.exception("unexpected failure; full traceback in the log file")
        sys.exit(1)


def exc_info_str(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _field_sheet_reference(path: Path, recs):
    """Reference trees (x, y, dbh in m) from a filled field sheet, matched to recs by tree_id."""
    import csv as _csv
    import numpy as np
    from .errors import InputDataError

    by_id = {r.tree_id: r for r in recs}
    rows, checked, not_trees, unknown = [], 0, [], []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for row in _csv.DictReader(fh):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            tid = int(row["tree_id"])
            verdict = row.get("is_tree", "").lower()
            if verdict in ("no", "n", "false", "0"):
                not_trees.append(tid)
            if verdict or row["tape_dbh_cm"]:
                checked += 1
            if not row["tape_dbh_cm"] or verdict in ("no", "n", "false", "0"):
                continue
            r = by_id.get(tid)
            if r is None:
                unknown.append(tid)
                continue
            rows.append([r.x, r.y, float(row["tape_dbh_cm"]) / 100.0])
    if checked:
        logger.info(
            f"field check: {checked} objects visited, {len(not_trees)} were not trees "
            f"({len(not_trees) / checked:.0%} false positives)"
            + (f": {', '.join(f'T{t}' for t in not_trees[:15])}" if not_trees else "")
        )
    if unknown:
        logger.warning(f"field sheet ids not in the trees CSV: {unknown[:15]}")
    if not rows:
        raise InputDataError(f"{path.name}: no tape_dbh_cm values filled in")
    return np.asarray(rows)


@cli.command()
def calibrate(
    trees_csv: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="CSV written by `fip inventory`.")],
    reference_csv: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Field data: columns x, y, dbh; or the field_sheet.csv from `fip georeference` / `fip merge` with tape_dbh_cm (and is_tree) filled in, matched by tree_id.")],
    dbh_unit: Annotated[str, typer.Option(help="Unit of reference dbh: auto (m or cm), m, cm, in or ft.")] = "auto",
    xy_unit: Annotated[Optional[str], typer.Option(help="Unit of reference x, y: m, ft or us_ft (default: the length_unit of TREES_CSV).")] = None,
    max_distance: Annotated[float, typer.Option(help="Max stem position mismatch (m) for matching.")] = 0.5,
    current_model_sd: Annotated[float, typer.Option(help="model_sd used for TREES_CSV (m).")] = 0.005,
):
    """Estimate fit.model_sd from field-measured reference trees."""
    logger.configure(level="info")  # the result is the output: show it whatever LOG_LEVEL says
    import csv as _csv
    import sys
    import numpy as np
    from .calibration import calibrate as _calibrate, read_reference_csv
    from .errors import FIPError
    from .records import TreeRecord

    try:
        from .units import normalize_unit, to_metres

        recs = []
        csv_unit = "m"
        with trees_csv.open(newline="", encoding="utf-8") as fh:
            for row in _csv.DictReader(fh):
                csv_unit = normalize_unit(row.get("length_unit") or "m")

                def f(k, conv=True):
                    if row.get(k) in (None, "", "nan"):
                        return float("nan")
                    v = float(row[k])
                    return float(to_metres(v, csv_unit)) if conv else v

                x, y = f("x"), f("y")
                if not np.isfinite(x) and row.get("utm_e"):  # a merged map (fip merge)
                    x, y = float(row["utm_e"]), float(row["utm_n"])
                se = f("dbh_se")
                if not np.isfinite(se):  # from the interval (merged maps carry no se)
                    se = (f("ci_high") - f("ci_low")) / (2 * 1.96)
                status = row["status"]
                if (row.get("dbh_method") or "").startswith("visible width"):
                    status = "excluded"  # a lower bound, not a measurement: not calibrated
                recs.append(TreeRecord(
                    tree_id=int(row["tree_id"]), status=status, x=x, y=y,
                    ground_z=f("ground_z"), breast_height=f("breast_height"), bh_elevation=f("bh_elevation"),
                    n_points=int(row.get("n_points") or 0), dbh=f("dbh"), dbh_se=se,
                    ci_low=f("ci_low"), ci_high=f("ci_high"),
                ))
        with reference_csv.open(newline="", encoding="utf-8-sig") as fh:
            ref_cols = {c.strip().lower() for c in (_csv.DictReader(fh).fieldnames or [])}
        if {"tree_id", "tape_dbh_cm"} <= ref_cols:
            ref = _field_sheet_reference(reference_csv, recs)
        else:
            ref = read_reference_csv(reference_csv, dbh_unit)
            ref[:, :2] = to_metres(ref[:, :2], normalize_unit(xy_unit) if xy_unit else csv_unit)
        res = _calibrate(recs, ref, current_model_sd, max_distance)
    except (FIPError, KeyError, ValueError) as exc:
        logger.error(f"calibration failed: {exc}")
        sys.exit(2)
    logger.info(
        f"matched {res.n_matched}/{res.n_reference} reference trees: bias {res.bias * 100:+.2f} cm, "
        f"RMSE {res.rmse * 100:.2f} cm, CI coverage {res.coverage:.0%}"
    )
    from .units import SHORT, from_metres

    sd_cli = float(from_metres(res.model_sd, csv_unit))
    logger.info(
        f"suggested fit.model_sd = {res.model_sd:.4f} m"
        + (f" = {sd_cli:.5f} {SHORT[csv_unit]}" if csv_unit != "m" else "")
        + f"  (re-run with --model-sd {sd_cli:.5f}"
        + (f" --units {csv_unit})" if csv_unit != "m" else ")")
    )
    if abs(res.bias) > 2 * res.rmse / np.sqrt(res.n_matched) and abs(res.bias) > 0.005:
        logger.warning("bias is significant; check breast_height and ground segmentation before trusting the CI")


@cli.command()
def georeference(
    trees_csv: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="trees.csv written by `fip inventory` (local coordinates).")],
    trajectory: Annotated[Optional[Path], typer.Option("--trajectory", "-t", exists=True, dir_okay=False, help="Scanner trajectory CSV (time_s, x, y[, z]) in the same local frame, e.g. from scripts/register_rosbag_kiss_icp.py.")] = None,
    gnss: Annotated[Optional[Path], typer.Option("--gnss", "-g", exists=True, help="GNSS fixes: CSV (time_s, lat, lon[, alt, h_sd]), a ROS bag or a ROS2 bag folder with NavSatFix messages.")] = None,
    gnss_topic: Annotated[Optional[list[str]], typer.Option("--gnss-topic", help="NavSatFix topic(s) to use from a bag (default: all; several antennas are pooled). Repeatable.")] = None,
    alignment_fp: Annotated[Optional[Path], typer.Option("--alignment", "-a", exists=True, dir_okay=False, help="Reuse georef.json from an earlier run instead of fitting (same local frame).")] = None,
    output_dir: Annotated[Optional[Path], typer.Option("--output", "-o", file_okay=False, help="Output directory (default: next to TREES_CSV).")] = None,
    log_level: Annotated[str, typer.Option(help="Console log level: trace, debug, info, warn.")] = "info",
):
    """
    Give every tree a latitude/longitude by fitting the run's trajectory to the GNSS fixes of
    the drive. Writes trees_wgs84.csv, trees_wgs84.geojson, trees.gpx (for a phone or GPS
    in the field) and georef.json (the fitted transform and its accuracy).
    """
    import math
    import sys
    from . import georef
    from .errors import FIPError

    out = output_dir or trees_csv.parent
    out.mkdir(parents=True, exist_ok=True)
    logger.configure(level=log_level, log_file=out / "georeference.log")
    try:
        if alignment_fp:
            alignment = georef.Alignment.load(alignment_fp)
            logger.info(f"using the saved georeference {alignment_fp}")
        else:
            if not (trajectory and gnss):
                raise typer.BadParameter("give --trajectory and --gnss, or --alignment")
            fixes = georef.read_gnss(gnss, gnss_topic)
            t, xy = georef.read_trajectory(trajectory)
            alignment = georef.fit_alignment(t, xy, fixes)
            alignment.save(out / "georef.json")
            if gnss.suffix.lower() != ".csv":
                inside = (fixes.time_s >= t[0]) & (fixes.time_s <= t[-1])
                georef.GnssFixes(fixes.time_s[inside], fixes.lat[inside], fixes.lon[inside],
                                 fixes.alt[inside], fixes.h_sd[inside]).write_csv(out / "gnss_fixes.csv")
        rows, cols = georef.georeference_trees(trees_csv, alignment)
        written = georef.write_outputs(rows, cols, out, stem=trees_csv.stem)
        sd = [float(r["position_sd_m"]) for r in rows]
        n_trees = sum(georef._is_tree(r) for r in rows)
        logger.info(
            f"georeferenced {len(rows)} rows ({n_trees} measured trees go to the GPX and GeoJSON); "
            f"position 1-sd {min(sd):.1f}-{max(sd):.1f} m "
            f"(GNSS + alignment; add the antenna-to-lidar offset if it is large). "
            f"Residual RMS {alignment.rms_m:.2f} m, rotation sd {math.degrees(alignment.theta_sd_rad):.2f} deg"
        )
        for w in written + ([out / "georef.json"] if not alignment_fp else []):
            logger.info(f"wrote {w}")
    except (FIPError, typer.BadParameter) as exc:
        logger.error(str(exc))
        sys.exit(2)
    except Exception:
        logger.exception("unexpected failure; full traceback in the log file")
        sys.exit(1)


@cli.command()
def merge(
    runs: Annotated[list[Path], typer.Argument(exists=True, help="Run folders (or their trees_wgs84.csv) from `fip georeference`, e.g. the segments of one drive.")],
    output_dir: Annotated[Path, typer.Option("--output", "-o", file_okay=False, help="Output directory.")] = Path("merged"),
    distance: Annotated[float, typer.Option(help="Merge distance (m): records from different runs closer than this, with agreeing DBH, are one tree (after tying the runs together; untied runs: widened to 2.5 combined position sd, up to 3 m).")] = 1.5,
    tie: Annotated[bool, typer.Option("--tie/--no-tie", help="Shift each run onto the others by the offset of the trees they share (removes run-to-run GNSS bias).")] = True,
    log_level: Annotated[str, typer.Option(help="Console log level: trace, debug, info, warn.")] = "info",
):
    """
    Merge georeferenced runs into one map: trees seen in two runs are kept once (the better
    measurement). Writes trees_merged_wgs84.csv (with UTM easting/northing),
    trees_merged_wgs84.geojson, trees_merged.gpx, trees.dxf on the UTM grid and
    merge_report.json (every merged pair, to check).
    """
    import sys
    from . import merge as mg
    from .errors import FIPError

    output_dir.mkdir(parents=True, exist_ok=True)
    logger.configure(level=log_level, log_file=output_dir / "merge.log")
    try:
        names = [r.name if r.is_dir() else r.parent.name for r in runs]
        if len(set(names)) < len(names):  # same folder names in different places
            names = [str(r) for r in runs]
        trees = [t for r, nm in zip(runs, names) for t in mg.read_run(r, nm)]
        quality = {nm: mg.run_quality(r) for r, nm in zip(runs, names)}
        res = mg.merge_trees(trees, distance=distance, run_quality=quality, tie=tie)
        for o in res.offsets:
            logger.info(
                f"{o['run']}: shifted {o['shift_east_m']:+.2f} m E, {o['shift_north_m']:+.2f} m N "
                f"onto the other runs ({o['tie_trees']} shared trees"
                + (f", spread {o['spread_m']:.2f} m)" if o.get("tie_trees") else "; not tied)")
            )
        written = mg.write_merged(res, output_dir, [str(r) for r in runs])
        logger.info(
            f"{res.n_input} trees from {len(runs)} runs -> {len(res.trees)} "
            f"({len(res.pairs)} seen twice and merged; see merge_report.json); "
            f"grid {mg.utm_epsg(res.zone, res.north)}"
        )
        for w in written:
            logger.info(f"wrote {w}")
    except FIPError as exc:
        logger.error(str(exc))
        sys.exit(2)
    except Exception:
        logger.exception("unexpected failure; full traceback in the log file")
        sys.exit(1)


@cli.command()
def plugins(
    plugin_dir: Annotated[Optional[list[Path]], typer.Option("--plugin-dir", help="Extra plugin folder(s).")] = None,
):
    """List available readers, exporters and metrics, and any plugins that failed to load."""
    from rich.table import Table
    from .plugins import load_plugins
    from .utils.rich_console import console

    reg = load_plugins(extra_dirs=plugin_dir or [])
    for title, items in (("Readers", reg.readers), ("Exporters", reg.exporters), ("Metrics", reg.metrics)):
        table = Table(title=title)
        table.add_column("name")
        table.add_column("description")
        table.add_column("options")
        for name, obj in sorted(items.items()):
            opts = ", ".join(f"{o.name}={o.default!r}" for o in getattr(obj, "options", ()))
            if hasattr(obj, "columns") and not opts:
                opts = "columns: " + ", ".join(obj.columns)
            table.add_row(name, obj.description, opts)
        console.print(table)
    for src, err in reg.load_errors.items():
        logger.warning(f"failed plugin {src}: {err}")


@cli.command("demo-data")
def demo_data(
    output: Annotated[Path, typer.Argument(help="Where to write the synthetic plot (.npy or .ply).")] = Path("demo_plot.ply"),
    n_trees: Annotated[int, typer.Option(help="Number of trees.")] = 25,
    seed: Annotated[int, typer.Option(help="Random seed.")] = 0,
):
    """Write a synthetic forest plot with known DBH (truth CSV alongside) for testing."""
    import csv as _csv
    import numpy as np
    from .synthetic import make_plot

    plot = make_plot(n_trees=n_trees, seed=seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix == ".npy":
        np.save(output, plot.points)
    else:
        Cloud.from_points(plot.points).write(output)
    truth_fp = output.with_name(output.stem + "_truth.csv")
    with truth_fp.open("w", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=list(plot.truth[0]))
        w.writeheader()
        w.writerows(plot.truth)
    logger.info(f"wrote {output} ({len(plot.points):,} points) and {truth_fp}")


@cli.command("self-test")
def self_test(
    skip_app: Annotated[bool, typer.Option("--skip-app", help="Skip the headless web app check.")] = False,
):
    """Check this installation: plugins, full pipeline on a demo plot, exports, and the web app."""
    import sys
    import tempfile
    import time

    import numpy as np

    from . import inventory as inv
    from .plugins import load_plugins
    from .synthetic import make_plot
    from .traits.dbh.robust_fit import FitParams

    failures: list[str] = []

    def check(name: str, fn):
        t0 = time.perf_counter()
        try:
            detail = fn()
            logger.info(f"[green]PASS[/green] {name} ({time.perf_counter() - t0:.1f}s) {detail or ''}")
        except Exception as exc:  # report every failure, keep checking
            failures.append(name)
            logger.error(f"FAIL {name}: {type(exc).__name__}: {exc}")
            logger.debug("details", exc_info_str(exc))

    state: dict = {}

    def plugins_ok():
        reg = load_plugins()
        missing = {"csv", "dxf"} - set(reg.exporters) | ({"open3d", "las", "rosbag"} - set(reg.readers))
        if missing or reg.load_errors:
            raise RuntimeError(f"missing {sorted(missing)} errors {reg.load_errors}")
        return f"readers={len(reg.readers)} exporters={len(reg.exporters)} metrics={len(reg.metrics)}"

    def pipeline_ok():
        plot = make_plot(n_trees=10, seed=0)
        gp, sp, fp = inv.GroundParams(), inv.SegmentParams(), FitParams()
        ground, non_ground = inv.segment_ground(plot.points, gp)
        normalized = inv.normalize_heights(ground, non_ground, gp)
        pts, labels = inv.segment_trees(normalized, sp)
        recs = inv.fit_trees(inv.group_trees(pts, labels, sp.min_tree_points), ground, fp, gp)
        truth = np.array([[t["x"], t["y"], t["dbh"]] for t in plot.truth])
        errs = []
        for r in recs:
            if r.fitted:
                d = np.hypot(truth[:, 0] - r.x, truth[:, 1] - r.y)
                if d.min() < 0.5:
                    errs.append(r.dbh - truth[d.argmin(), 2])
        if len(errs) < 8:
            raise RuntimeError(f"only {len(errs)}/10 trees found")
        rmse = float(np.sqrt(np.mean(np.square(errs))))
        if rmse > 0.01:
            raise RuntimeError(f"DBH RMSE {rmse * 100:.2f} cm is too high")
        state["records"], state["fp"] = recs, fp
        return f"{len(errs)}/10 trees, RMSE {rmse * 100:.2f} cm"

    def exports_ok():
        with tempfile.TemporaryDirectory() as td:
            fmts = ["csv", "dxf", "geojson", "points"]
            out = inv.export_records(state["records"], Path(td), fmts, inv.context_for("self-test", state["fp"]))
            names = {p.name for p in out}
            expected = {"trees.csv", "trees.dxf", "trees.geojson", "trees.laz", "run.json"}
            if names != expected or not all(p.stat().st_size > 0 for p in out):
                raise RuntimeError(f"exports incomplete: got {sorted(names)}, expected {sorted(expected)}")
        return ", ".join(fmts) + ", run.json"

    def app_ok():
        from streamlit.testing.v1 import AppTest

        from .app.launch import APP_SCRIPT

        # the app keeps its log file open; Windows cannot delete open files, so ignore cleanup errors
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            import os

            old = os.environ.get("FIP_HOME")
            os.environ["FIP_HOME"] = td
            try:
                at = AppTest.from_file(str(APP_SCRIPT), default_timeout=300)
                at.run()
                at.sidebar.number_input[0].set_value(8)
                at.button[0].click().run()
            finally:
                if old is None:
                    os.environ.pop("FIP_HOME", None)
                else:
                    os.environ["FIP_HOME"] = old
            if at.exception or at.error:
                raise RuntimeError([str(e.value)[:300] for e in list(at.exception) + list(at.error)])
            fitted = next(m.value for m in at.metric if m.label == "Trees fitted")
        return f"demo run, trees fitted {fitted}"

    check("plugins", plugins_ok)
    check("pipeline", pipeline_ok)
    if "records" in state:
        check("exports", exports_ok)
    if not skip_app:
        check("web app", app_ok)
    if failures:
        logger.error(f"self-test failed: {', '.join(failures)}")
        sys.exit(1)
    logger.info("self-test passed")


@cli.command()
def app(
    port: Annotated[int, typer.Option(help="Port for the web interface (next free port is used if taken).")] = 8501,
    plugin_dir: Annotated[Optional[list[Path]], typer.Option("--plugin-dir", help="Extra plugin folder(s).")] = None,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open a browser window.")] = False,
    address: Annotated[str, typer.Option(help="Interface to listen on; 0.0.0.0 shares it on the network.")] = "127.0.0.1",
):
    """Launch the interactive web interface (needs the app extra when installed with pip)."""
    import sys

    try:
        import streamlit  # noqa: F401
    except ImportError:
        logger.error("the web app needs streamlit: pip install 'forest_inventory_pipeline[app]'")
        sys.exit(2)
    from .app.launch import run_app

    try:
        run_app(port=port, open_browser=not no_browser, plugin_dirs=plugin_dir, address=address)
    except (RuntimeError, FileNotFoundError) as exc:
        logger.error(str(exc))
        sys.exit(2)

if __name__ == "__main__":
    cli()
