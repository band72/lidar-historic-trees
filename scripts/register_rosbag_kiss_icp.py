"""
Register (stitch) raw sensor-frame lidar scans from a ROS bag with KISS-ICP, and write one
point cloud that `fip inventory` can measure.

Use it when a bag has scans in the sensor frame and no usable poses (no /tf from SLAM, or
GNSS/INS too coarse for centimetre work):

    pip install kiss-icp
    python scripts/register_rosbag_kiss_icp.py run.bag --topic /os_cloud_node/points \\
        --start 0 --duration 120 -o out/segment.laz
    fip inventory out/segment.laz -o out/inventory -e csv -e dxf -e points

What it does, per scan:
  1. decode the PointCloud2 (x, y, z and the per-point time field, e.g. Ouster's "t")
  2. drop no-return pixels (0, 0, 0 or NaN) and returns closer than --min-range (the platform)
  3. KISS-ICP lidar odometry, which also removes motion blur using the per-point times
  4. transform the de-blurred scan with its pose and keep returns within --keep-range
Then: level the cloud with gravity from the lidar's IMU (z up), de-duplicate on a --voxel
grid, and write LAZ (or LAS) plus the trajectory as CSV.

Coordinates are local (metres, origin at the first scan), not georeferenced. KISS-ICP drifts
slowly over long runs; keep segments to a few minutes for measurement.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np


def pointcloud2_xyzt(msg) -> tuple[np.ndarray, np.ndarray | None]:
    """x, y, z (float64) and the per-point time field if present, no-return pixels removed."""
    from forest_inventory_pipeline.plugins.builtin.readers import _PF_DTYPES

    fields = {f.name: f for f in msg.fields}
    names = ["x", "y", "z"] + [n for n in ("t", "time", "timestamp") if n in fields][:1]
    endian = ">" if msg.is_bigendian else "<"
    dtype = np.dtype(
        {
            "names": names,
            "formats": [
                np.dtype(_PF_DTYPES[fields[n].datatype]).newbyteorder(endian) for n in names
            ],
            "offsets": [fields[n].offset for n in names],
            "itemsize": int(msg.point_step),
        }
    )
    n = int(msg.height) * int(msg.width)
    data = np.asarray(msg.data, dtype=np.uint8)
    if msg.height > 1 and int(msg.row_step) != msg.width * msg.point_step:
        data = data.reshape(msg.height, int(msg.row_step))[:, : msg.width * msg.point_step].reshape(
            -1
        )
    arr = np.frombuffer(data.tobytes(), dtype=dtype, count=n)
    xyz = np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float64)
    t = arr[names[3]].astype(np.float64) if len(names) == 4 else None
    ok = np.all(np.isfinite(xyz), axis=1) & np.any(xyz != 0.0, axis=1)
    return xyz[ok], (t[ok] if t is not None else None)


def gravity_levelling(reader, imu_topic: str, start_ns: int, seconds: float = 3.0) -> np.ndarray:
    """Rotation that turns the mean measured acceleration (gravity reaction) into +z."""
    conns = [c for c in reader.connections if c.topic == imu_topic]
    if not conns:
        return np.eye(3)
    acc = []
    for con, _, raw in reader.messages(
        connections=conns, start=start_ns, stop=start_ns + int(seconds * 1e9)
    ):
        a = reader.deserialize(raw, con.msgtype).linear_acceleration
        acc.append([a.x, a.y, a.z])
    if len(acc) < 10:
        return np.eye(3)
    g = np.mean(acc, axis=0)
    g = g / np.linalg.norm(g)
    z = np.array([0.0, 0.0, 1.0])
    v, c = np.cross(g, z), float(g @ z)
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    tilt = np.degrees(np.arccos(np.clip(c, -1, 1)))
    print(f"levelling: sensor tilted {tilt:.1f} deg from vertical (from {len(acc)} IMU samples)")
    return np.eye(3) + vx + vx @ vx / (1 + c)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("bag", type=Path)
    ap.add_argument("--topic", default="/os_cloud_node/points")
    ap.add_argument(
        "--imu-topic",
        default="/os_cloud_node/imu",
        help="IMU in (or aligned with) the lidar frame, for levelling",
    )
    ap.add_argument("--start", type=float, default=0.0, help="seconds from the start of the bag")
    ap.add_argument("--duration", type=float, default=120.0, help="seconds to process")
    ap.add_argument(
        "--min-range",
        type=float,
        default=1.5,
        help="drop returns closer than this (platform, operator)",
    )
    ap.add_argument("--max-range", type=float, default=80.0, help="returns used for odometry")
    ap.add_argument(
        "--keep-range", type=float, default=25.0, help="returns kept in the output cloud"
    )
    ap.add_argument(
        "--voxel", type=float, default=0.01, help="de-duplication grid of the output (m)"
    )
    ap.add_argument("-o", "--output", type=Path, required=True, help=".laz or .las")
    a = ap.parse_args()

    try:
        import laspy
        from kiss_icp.config import KISSConfig
        from kiss_icp.kiss_icp import KissICP
        from rosbags.highlevel import AnyReader
        from rosbags.typesys import Stores, get_typestore
    except ImportError as exc:
        print(f"missing dependency: {exc}. Install with: pip install kiss-icp laspy[lazrs] rosbags")
        return 2

    cfg = KISSConfig()
    cfg.data.max_range, cfg.data.min_range, cfg.data.deskew = a.max_range, a.min_range, True
    cfg.mapping.voxel_size = a.max_range / 100.0
    odom = KissICP(cfg)

    a.output.parent.mkdir(parents=True, exist_ok=True)
    chunks, poses, n_scans, t_start = [], [], 0, time.perf_counter()
    with AnyReader([a.bag], default_typestore=get_typestore(Stores.ROS1_NOETIC)) as reader:
        conns = [c for c in reader.connections if c.topic == a.topic]
        if not conns:
            print(f"topic {a.topic} not in bag")
            return 2
        start_ns = reader.start_time + int(a.start * 1e9)
        stop_ns = start_ns + int(a.duration * 1e9)
        R_level = gravity_levelling(reader, a.imu_topic, start_ns)
        for con, stamp, raw in reader.messages(connections=conns, start=start_ns, stop=stop_ns):
            xyz, t = pointcloud2_xyzt(reader.deserialize(raw, con.msgtype))
            deskewed, _ = odom.register_frame(xyz, t if t is not None else np.array([]))
            pose = odom.last_pose
            rng = np.linalg.norm(deskewed, axis=1)
            keep = deskewed[(rng >= a.min_range) & (rng <= a.keep_range)]
            world = keep @ pose[:3, :3].T + pose[:3, 3]
            chunks.append(world @ R_level.T)
            p = R_level @ pose[:3, 3]
            poses.append((stamp / 1e9, *p))
            n_scans += 1
            if n_scans % 100 == 0:  # de-duplicate as we go to bound memory
                merged = np.concatenate(chunks)
                _, first = np.unique(
                    np.floor(merged / a.voxel).astype(np.int64), axis=0, return_index=True
                )
                chunks = [merged[np.sort(first)]]
                el = time.perf_counter() - t_start
                print(f"  {n_scans} scans, {len(chunks[0]):,} points, {n_scans / el:.1f} scans/s")
    if not chunks:
        print("no scans in the requested time range")
        return 2
    merged = np.concatenate(chunks)
    _, first = np.unique(np.floor(merged / a.voxel).astype(np.int64), axis=0, return_index=True)
    cloud = merged[np.sort(first)]
    traj = np.array(poses)
    path = float(np.sum(np.linalg.norm(np.diff(traj[:, 1:4], axis=0), axis=1)))
    print(
        f"registered {n_scans} scans in {time.perf_counter() - t_start:.0f}s; path {path:.0f} m; "
        f"{len(cloud):,} points after {a.voxel} m de-duplication"
    )

    header = laspy.LasHeader(point_format=6, version="1.4")
    header.scales = np.array([0.001] * 3)
    header.offsets = np.floor(cloud.min(axis=0))
    las = laspy.LasData(header)
    las.x, las.y, las.z = cloud[:, 0], cloud[:, 1], cloud[:, 2]
    las.write(str(a.output), do_compress=a.output.suffix.lower() == ".laz")
    traj_fp = a.output.with_name(a.output.stem + "_trajectory.csv")
    with traj_fp.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["time_s", "x", "y", "z"])
        w.writerows(traj.round(4).tolist())
    print(f"wrote {a.output} ({a.output.stat().st_size / 1e6:.0f} MB) and {traj_fp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
