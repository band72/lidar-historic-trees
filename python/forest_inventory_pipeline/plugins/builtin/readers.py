"""
Built-in point cloud readers.

- ``open3d``: .ply .pcd .xyz .xyzn .xyzrgb .pts
- ``las``: .las .laz (laspy; .laz needs ``laspy[lazrs]``)
- ``numpy``: .npy (N x >=3 array)
- ``text``: .txt .csv .asc (first three numeric columns; header lines are skipped)
- ``rosbag``: ROS1 .bag files and ROS2 bag directories / .db3 / .mcap (``rosbags`` package)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from ...errors import InputDataError
from ...utils import logger
from ..base import OptionSpec, Reader
from ..registry import register_reader


def _clean(xyz: np.ndarray, source: Path) -> np.ndarray:
    xyz = np.asarray(xyz, dtype=np.float64)
    if xyz.ndim != 2 or xyz.shape[1] < 3:
        raise InputDataError(f"{source.name}: expected an (N, 3) array, got shape {xyz.shape}")
    xyz = xyz[:, :3]
    finite = np.all(np.isfinite(xyz), axis=1)
    dropped = int((~finite).sum())
    if dropped:
        logger.warning(f"{source.name}: dropped {dropped} non-finite points")
        xyz = xyz[finite]
    if len(xyz) == 0:
        raise InputDataError(f"{source.name}: contains no valid points")
    return np.ascontiguousarray(xyz)


@register_reader
class Open3DReader(Reader):
    name = "open3d"
    extensions = (".ply", ".pcd", ".xyz", ".xyzn", ".xyzrgb", ".pts")
    label = "PLY / PCD / XYZ"
    description = "Point cloud files readable by Open3D."

    def read(self, path: Path, **options: Any) -> np.ndarray:
        import open3d as o3d

        self.resolve_options(options)
        tpcd = o3d.t.io.read_point_cloud(str(path))
        if "positions" not in tpcd.point:
            raise InputDataError(
                f"{path.name}: Open3D could not read any points (corrupt or unsupported file?)"
            )
        return _clean(tpcd.point.positions.numpy(), Path(path))


@register_reader
class LasReader(Reader):
    name = "las"
    extensions = (".las", ".laz")
    label = "LAS / LAZ"
    description = "ASPRS LAS/LAZ point clouds (laspy). Scaled coordinates are applied."
    options = (
        OptionSpec(
            "ground_class_only", False, "keep only ASPRS class 2 (ground) points - for debugging"
        ),
    )

    def read(self, path: Path, **options: Any) -> np.ndarray:
        opts = self.resolve_options(options)
        try:
            import laspy
        except ImportError as exc:
            raise InputDataError(
                "LAS input needs laspy: pip install 'forest_inventory_pipeline[las]'"
            ) from exc
        try:
            las = laspy.read(str(path))
        except Exception as exc:  # laspy raises a variety of errors (incl. missing LAZ backend)
            raise InputDataError(f"{Path(path).name}: cannot read LAS/LAZ: {exc}") from exc
        xyz = np.asarray(las.xyz)
        if opts["ground_class_only"]:
            xyz = xyz[np.asarray(las.classification) == 2]
        return _clean(xyz, Path(path))


@register_reader
class NumpyReader(Reader):
    name = "numpy"
    extensions = (".npy",)
    label = "NumPy array"
    description = "An .npy file holding an (N, 3+) array."

    def read(self, path: Path, **options: Any) -> np.ndarray:
        self.resolve_options(options)
        try:
            arr = np.load(str(path), allow_pickle=False)
        except ValueError as exc:
            raise InputDataError(
                f"{Path(path).name}: not a plain numeric .npy array ({exc})"
            ) from exc
        return _clean(arr, Path(path))


@register_reader
class TextReader(Reader):
    name = "text"
    extensions = (".txt", ".csv", ".asc")
    label = "Text XYZ / CSV"
    description = "Delimited text; the first three numeric columns are x, y, z."
    options = (
        OptionSpec(
            "delimiter",
            "auto",
            "column delimiter: auto, comma, space, tab, semicolon",
            choices=("auto", "comma", "space", "tab", "semicolon"),
        ),
    )

    def read(self, path: Path, **options: Any) -> np.ndarray:
        opts = self.resolve_options(options)
        delim = {"auto": None, "comma": ",", "space": None, "tab": "\t", "semicolon": ";"}[
            opts["delimiter"]
        ]
        path = Path(path)
        if delim is None and opts["delimiter"] == "auto":
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                sample = fh.read(4096)
            for cand in (",", ";", "\t"):
                if cand in sample:
                    delim = cand
                    break
        # skip header rows: find the first line whose first 3 fields parse as floats
        skip = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.strip().split(delim) if delim else line.split()
                try:
                    [float(p) for p in parts[:3]]
                    if len(parts) >= 3:
                        break
                except ValueError:
                    pass
                skip += 1
                if skip > 100:
                    raise InputDataError(
                        f"{path.name}: no numeric x,y,z rows in the first 100 lines"
                    )
        try:
            arr = np.loadtxt(str(path), delimiter=delim, skiprows=skip, usecols=(0, 1, 2), ndmin=2)
        except ValueError as exc:
            raise InputDataError(
                f"{path.name}: cannot parse numeric x,y,z columns ({exc})"
            ) from exc
        return _clean(arr, path)


# --------------------------------------------------------------------------- rosbag

_SENSOR_FRAME_HINTS = (
    "lidar",
    "velodyne",
    "os_sensor",
    "os_lidar",
    "livox",
    "laser",
    "hesai",
    "rslidar",
    "sensor",
)

# sensor_msgs/PointField datatype -> numpy dtype
_PF_DTYPES = {
    1: np.int8,
    2: np.uint8,
    3: np.int16,
    4: np.uint16,
    5: np.int32,
    6: np.uint32,
    7: np.float32,
    8: np.float64,
}


def pointcloud2_to_xyz(msg) -> np.ndarray:
    """Decode the x, y, z fields of a deserialized sensor_msgs/PointCloud2."""
    fields = {f.name: f for f in msg.fields}
    missing = [c for c in "xyz" if c not in fields]
    if missing:
        raise InputDataError(f"PointCloud2 has no {missing} field(s); fields are {sorted(fields)}")
    n = int(msg.height) * int(msg.width)
    if n == 0:
        return np.empty((0, 3))
    endian = ">" if msg.is_bigendian else "<"
    dtype = np.dtype(
        {
            "names": ["x", "y", "z"],
            "formats": [
                np.dtype(_PF_DTYPES[fields[c].datatype]).newbyteorder(endian) for c in "xyz"
            ],
            "offsets": [fields[c].offset for c in "xyz"],
            "itemsize": int(msg.point_step),
        }
    )
    data = np.asarray(msg.data, dtype=np.uint8)
    row_step = int(msg.row_step)
    if msg.height > 1 and row_step != msg.width * msg.point_step:
        # rows are padded: strip padding per row
        data = data.reshape(msg.height, row_step)[:, : msg.width * msg.point_step].reshape(-1)
    arr = np.frombuffer(data.tobytes(), dtype=dtype, count=n)
    return np.stack([arr["x"], arr["y"], arr["z"]], axis=1).astype(np.float64)


class _TfBuffer:
    """Minimal TF tree from /tf and /tf_static with time interpolation (no extrapolation)."""

    def __init__(self, tolerance_s: float):
        self.static: dict[tuple[str, str], np.ndarray] = {}
        self.dynamic: dict[tuple[str, str], list[tuple[float, np.ndarray, np.ndarray]]] = {}
        self.tolerance = tolerance_s

    def add(self, tf_msg, static: bool) -> None:
        for t in tf_msg.transforms:
            parent, child = t.header.frame_id.lstrip("/"), t.child_frame_id.lstrip("/")
            tr, q = t.transform.translation, t.transform.rotation
            trans = np.array([tr.x, tr.y, tr.z])
            quat = np.array([q.x, q.y, q.z, q.w])
            if static:
                self.static[(parent, child)] = _to_matrix(trans, quat)
            else:
                stamp = t.header.stamp.sec + t.header.stamp.nanosec * 1e-9
                self.dynamic.setdefault((parent, child), []).append((stamp, trans, quat))

    def finalize(self) -> None:
        for key in self.dynamic:
            self.dynamic[key].sort(key=lambda s: s[0])

    def _edge(self, parent: str, child: str, stamp: float) -> np.ndarray | None:
        if (parent, child) in self.static:
            return self.static[(parent, child)]
        series = self.dynamic.get((parent, child))
        if not series:
            return None
        times = np.fromiter((s[0] for s in series), float, len(series))
        i = int(np.searchsorted(times, stamp))
        if i == 0:
            return (
                _to_matrix(series[0][1], series[0][2])
                if times[0] - stamp <= self.tolerance
                else None
            )
        if i == len(series):
            return (
                _to_matrix(series[-1][1], series[-1][2])
                if stamp - times[-1] <= self.tolerance
                else None
            )
        (t0, p0, q0), (t1, p1, q1) = series[i - 1], series[i]
        a = 0.0 if t1 == t0 else (stamp - t0) / (t1 - t0)
        from scipy.spatial.transform import Rotation, Slerp

        rot = Slerp([0.0, 1.0], Rotation.from_quat([q0, q1]))([a])[0]
        T = np.eye(4)
        T[:3, :3] = rot.as_matrix()
        T[:3, 3] = (1 - a) * p0 + a * p1
        return T

    def lookup(self, target: str, source: str, stamp: float) -> np.ndarray | None:
        """4x4 transform taking points in ``source`` frame to ``target`` frame, or None."""
        if target == source:
            return np.eye(4)
        parents = {c: p for (p, c) in list(self.static) + list(self.dynamic)}

        def chain(frame):
            out = [frame]
            while out[-1] in parents and len(out) < 64:
                out.append(parents[out[-1]])
            return out

        up_src, up_tgt = chain(source), chain(target)
        common = next((f for f in up_src if f in up_tgt), None)
        if common is None:
            return None
        T_common_src = np.eye(4)
        for child in up_src[: up_src.index(common)]:
            E = self._edge(parents[child], child, stamp)
            if E is None:
                return None
            T_common_src = E @ T_common_src
        T_common_tgt = np.eye(4)
        for child in up_tgt[: up_tgt.index(common)]:
            E = self._edge(parents[child], child, stamp)
            if E is None:
                return None
            T_common_tgt = E @ T_common_tgt
        return np.linalg.inv(T_common_tgt) @ T_common_src


def _to_matrix(trans: np.ndarray, quat_xyzw: np.ndarray) -> np.ndarray:
    from scipy.spatial.transform import Rotation

    T = np.eye(4)
    T[:3, :3] = Rotation.from_quat(quat_xyzw).as_matrix()
    T[:3, 3] = trans
    return T


@register_reader
class RosbagReader(Reader):
    name = "rosbag"
    extensions = (".bag", ".db3", ".mcap", "/")
    label = "ROS bag (PointCloud2)"
    description = (
        "Accumulates sensor_msgs/PointCloud2 messages from one topic. Messages must already be in a "
        "world/map frame (e.g. a SLAM-registered cloud), or set target_frame to transform them using "
        "/tf and /tf_static recorded in the bag."
    )
    options = (
        OptionSpec(
            "topic", "", "PointCloud2 topic; empty = auto-detect when the bag has exactly one"
        ),
        OptionSpec(
            "target_frame",
            "",
            "transform every scan into this frame via /tf (e.g. 'map'); empty = use as-is",
        ),
        OptionSpec("every_nth", 1, "use every n-th message", min=1, max=10_000),
        OptionSpec("max_messages", 0, "stop after this many used messages (0 = all)", min=0),
        OptionSpec(
            "min_range",
            0.0,
            "drop points closer than this to the sensor (m, sensor frame)",
            min=0.0,
        ),
        OptionSpec(
            "max_range", 0.0, "drop points farther than this from the sensor (m, 0 = off)", min=0.0
        ),
        OptionSpec(
            "voxel_size",
            0.01,
            "deduplicate the accumulated cloud on this grid (m, 0 = off)",
            min=0.0,
        ),
        OptionSpec(
            "tf_tolerance",
            0.1,
            "max seconds outside the /tf time range to still accept a scan",
            min=0.0,
        ),
        OptionSpec(
            "allow_sensor_frame",
            False,
            "accept scans in a sensor frame without target_frame (single static scan)",
        ),
    )

    def accepts(self, path: Path) -> bool:
        path = Path(path)
        if path.is_dir():
            return (path / "metadata.yaml").exists()
        return path.suffix.lower() in (".bag", ".db3", ".mcap")

    def read(self, path: Path, **options: Any) -> np.ndarray:
        opts = self.resolve_options(options)
        try:
            from rosbags.highlevel import AnyReader
            from rosbags.typesys import Stores, get_typestore
        except ImportError as exc:
            raise InputDataError(
                "rosbag input needs the 'rosbags' package: pip install rosbags"
            ) from exc

        path = Path(path)
        bag_path = (
            path.parent
            if path.suffix.lower() in (".db3", ".mcap") and (path.parent / "metadata.yaml").exists()
            else path
        )
        try:
            reader_ctx = AnyReader([bag_path], default_typestore=get_typestore(Stores.ROS2_HUMBLE))
        except Exception as exc:
            raise InputDataError(f"{path.name}: cannot open bag: {exc}") from exc

        with reader_ctx as reader:
            pc_conns = [c for c in reader.connections if c.msgtype.endswith("PointCloud2")]
            topics = sorted({c.topic for c in pc_conns})
            if not topics:
                all_topics = sorted({f"{c.topic} [{c.msgtype}]" for c in reader.connections})
                raise InputDataError(
                    f"{path.name}: no PointCloud2 topics. topics in bag: {all_topics}"
                )
            topic = opts["topic"]
            if not topic:
                if len(topics) > 1:
                    raise InputDataError(
                        f"{path.name}: several PointCloud2 topics {topics}; choose one with the 'topic' option"
                    )
                topic = topics[0]
            elif topic not in topics:
                raise InputDataError(
                    f"{path.name}: topic '{topic}' not found; PointCloud2 topics: {topics}"
                )
            conns = [c for c in pc_conns if c.topic == topic]

            tf_buffer = None
            target = opts["target_frame"].lstrip("/")
            if target:
                tf_conns = [c for c in reader.connections if c.topic in ("/tf", "/tf_static")]
                if not tf_conns:
                    raise InputDataError(
                        f"{path.name}: target_frame='{target}' set but the bag has no /tf or /tf_static"
                    )
                tf_buffer = _TfBuffer(opts["tf_tolerance"])
                for conn, _, raw in reader.messages(connections=tf_conns):
                    tf_buffer.add(
                        reader.deserialize(raw, conn.msgtype), static=conn.topic == "/tf_static"
                    )
                tf_buffer.finalize()
                logger.info(
                    f"TF buffer: {len(tf_buffer.static)} static, {len(tf_buffer.dynamic)} dynamic edges"
                )

            clouds, used, seen, skipped_tf = [], 0, 0, 0
            frames: set[str] = set()
            for conn, _, raw in reader.messages(connections=conns):
                seen += 1
                if (seen - 1) % opts["every_nth"]:
                    continue
                msg = reader.deserialize(raw, conn.msgtype)
                frame = msg.header.frame_id.lstrip("/")
                frames.add(frame)
                xyz = pointcloud2_to_xyz(msg)
                xyz = xyz[np.all(np.isfinite(xyz), axis=1)]
                if opts["min_range"] > 0 or opts["max_range"] > 0:
                    rng = np.linalg.norm(xyz, axis=1)
                    keep = rng >= opts["min_range"]
                    if opts["max_range"] > 0:
                        keep &= rng <= opts["max_range"]
                    xyz = xyz[keep]
                if tf_buffer is not None:
                    stamp = msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9
                    T = tf_buffer.lookup(target, frame, stamp)
                    if T is None:
                        skipped_tf += 1
                        continue
                    xyz = xyz @ T[:3, :3].T + T[:3, 3]
                clouds.append(xyz)
                used += 1
                if opts["max_messages"] and used >= opts["max_messages"]:
                    break

        if skipped_tf:
            logger.warning(
                f"{path.name}: skipped {skipped_tf} scans with no transform {target} <- scan frame at their timestamp"
            )
        if not clouds:
            raise InputDataError(
                f"{path.name}: no usable PointCloud2 messages on {topic} (seen {seen}, tf-skipped {skipped_tf})"
            )
        if tf_buffer is None and used > 1:
            sensorish = [f for f in frames if any(h in f.lower() for h in _SENSOR_FRAME_HINTS)]
            if sensorish and not opts["allow_sensor_frame"]:
                raise InputDataError(
                    f"{path.name}: {used} scans on {topic} are in sensor frame(s) {sorted(sensorish)}. "
                    "Stacking them without poses smears the trees. Set target_frame (e.g. 'map' or 'odom') "
                    "to register them with /tf, use a SLAM-registered cloud topic, or set allow_sensor_frame "
                    "if the sensor was static."
                )
        xyz = np.concatenate(clouds)
        logger.info(
            f"rosbag: {used} messages from {topic} (frames {sorted(frames)}) -> {len(xyz)} points"
        )
        if opts["voxel_size"] > 0:
            keys = np.floor(xyz / opts["voxel_size"]).astype(np.int64)
            _, first = np.unique(keys, axis=0, return_index=True)
            xyz = xyz[np.sort(first)]
            logger.info(f"rosbag: voxel dedup at {opts['voxel_size']} m -> {len(xyz)} points")
        return _clean(xyz, path)
