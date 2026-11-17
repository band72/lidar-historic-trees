"""
A whole drive in one go: every segment measured on its own, georeferenced, and merged.

A long drive is stitched in segments (one lidar-odometry run each, e.g. with
``scripts/register_rosbag_kiss_icp.py``), and every segment has its own local coordinates,
so the segments must never be loaded as one cloud. This runs, for each segment,
``fip inventory`` and ``fip georeference`` (with the segment's trajectory and the drive's
GNSS), then ``fip merge`` into one map.

A segment is a point cloud file with its trajectory next to it:
``segment_0-120s.laz`` and ``segment_0-120s_trajectory.csv``.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Sequence

from .errors import FIPError, InputDataError
from .utils import logger

CLOUD_SUFFIXES = (".laz", ".las", ".ply", ".pcd", ".npy")
ProgressFn = Callable[[float, str], None]


def find_segments(source: Path | Sequence[Path]) -> list[tuple[Path, Path]]:
    """
    (cloud, trajectory) pairs of a drive: the point cloud files of a folder (or the files
    given), each with ``<name>_trajectory.csv`` next to it, in name order.

    :raises InputDataError: no point cloud, or a cloud without its trajectory.
    """
    if isinstance(source, (str, Path)):
        folder = Path(source)
        if not folder.is_dir():
            raise InputDataError(f"{folder} is not a folder")
        clouds = sorted(p for p in folder.iterdir() if p.suffix.lower() in CLOUD_SUFFIXES)
    else:
        clouds = sorted(Path(p) for p in source)
    if not clouds:
        raise InputDataError(f"no point cloud files ({', '.join(CLOUD_SUFFIXES)}) in {source}")
    pairs, missing = [], []
    for c in clouds:
        t = c.with_name(c.stem + "_trajectory.csv")
        (pairs if t.exists() else missing).append((c, t))
    if missing:
        raise InputDataError(
            "each segment needs its trajectory next to it (written by "
            "scripts/register_rosbag_kiss_icp.py); missing: "
            + ", ".join(t.name for _, t in missing[:5])
        )
    return pairs


def _drop_log_file(path: Path) -> None:
    """Detach a run's log file so later segments do not write into it."""
    lg = getattr(logger, "_logger", None)
    for h in list(getattr(lg, "handlers", [])):
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == path.resolve():
            lg.removeHandler(h)
            h.close()


def run_drive(
    source: Path | Sequence[Path],
    gnss: Path,
    out_dir: Path,
    *,
    progress: ProgressFn | None = None,
    gnss_topics: Sequence[str] | None = None,
    merge_distance: float = 1.5,
    **inventory_options,
) -> dict:
    """
    Measure, georeference and merge every segment of a drive.

    :param source: the folder holding the segments, or the cloud files themselves.
    :param gnss: GNSS fixes of the drive: a CSV (time_s, lat, lon) or the ROS bag.
    :param inventory_options: passed to ``fip inventory`` for every segment (``profile``,
        ``scanner``, ``config_fp``, ``output_units``, ``export``, ...).
    :return: ``{"merged": folder, "trees": N, "duplicates": N, "segments": [per segment:
        name, folder, trees, gnss_rms_m, seconds, error]}``
    :raises InputDataError: no segment could be measured (one failing segment is reported
        and skipped).
    """
    from . import fip, georef
    from . import merge as mg

    def tell(frac: float, msg: str) -> None:
        logger.info(msg)
        if progress:
            progress(frac, msg)

    pairs = find_segments(source)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tell(0.0, f"reading the GNSS fixes from {Path(gnss).name}")
    fixes = georef.read_gnss(Path(gnss), gnss_topics)
    inventory_options.setdefault("export", ["csv", "dxf", "geojson"])
    segments, n = [], len(pairs)
    for i, (cloud, traj) in enumerate(pairs):
        name, d, t0 = cloud.stem, out_dir / cloud.stem, time.perf_counter()
        info = dict(name=name, folder=str(d), trees=0, gnss_rms_m=None, seconds=0.0, error="")
        segments.append(info)
        tell(i / n * 0.95, f"segment {i + 1} of {n}: {cloud.name}")
        try:
            try:
                fip.inventory(inputs=[cloud], output_dir=d, **inventory_options)
            except SystemExit as exc:
                if exc.code not in (0, None):
                    raise InputDataError(f"measuring failed; see {d / 'fip.log'}") from None
            finally:
                _drop_log_file(d / "fip.log")
            t, xy = georef.read_trajectory(traj)
            alignment = georef.fit_alignment(t, xy, fixes)
            alignment.save(d / "georef.json")
            rows, cols = georef.georeference_trees(d / "trees.csv", alignment)
            georef.write_outputs(rows, cols, d, stem="trees")
            info["trees"] = sum(georef._is_tree(r) for r in rows)
            info["gnss_rms_m"] = round(alignment.rms_m, 2)
        except FIPError as exc:
            info["error"] = str(exc)
            logger.warning(f"segment {name} skipped: {exc}")
        info["seconds"] = round(time.perf_counter() - t0, 1)
    good = [s for s in segments if not s["error"] and s["trees"]]
    if not good:
        raise InputDataError(
            "no segment could be measured: "
            + "; ".join(f"{s['name']}: {s['error'] or 'no trees'}" for s in segments[:5])
        )
    tell(0.96, f"merging {len(good)} segments into one map")
    trees = [t for s in good for t in mg.read_run(Path(s["folder"]), s["name"])]
    quality = {s["name"]: mg.run_quality(Path(s["folder"])) for s in good}
    res = mg.merge_trees(trees, distance=merge_distance, run_quality=quality)
    merged = out_dir / "merged"
    mg.write_merged(res, merged, [s["name"] for s in good])
    tell(1.0, f"one map: {len(res.trees)} trees from {len(good)} segments "
              f"({len(res.pairs)} seen twice and merged) in {merged}")
    return dict(merged=merged, trees=len(res.trees), duplicates=len(res.pairs), segments=segments)
