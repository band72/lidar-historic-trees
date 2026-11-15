from types import SimpleNamespace

import numpy as np
import pytest

from forest_inventory_pipeline.errors import InputDataError, ParameterError
from forest_inventory_pipeline.inventory import load_points, refine_ground
from forest_inventory_pipeline.plugins import load_plugins
from forest_inventory_pipeline.plugins.builtin.readers import _TfBuffer, pointcloud2_to_xyz
from forest_inventory_pipeline.preprocess.height_normalization import interpolate_ground_z


def cloud(n=2000, seed=0):
    rng = np.random.default_rng(seed)
    return np.c_[rng.uniform(0, 10, n), rng.uniform(0, 10, n), rng.uniform(0, 5, n)]


def test_npy_and_text_readers(tmp_path):
    pts = cloud()
    np.save(tmp_path / "a.npy", pts)
    np.testing.assert_allclose(load_points(tmp_path / "a.npy"), pts)
    with (tmp_path / "a.csv").open("w") as fh:
        fh.write("X,Y,Z,intensity\n")
        for p in pts:
            fh.write(f"{p[0]},{p[1]},{p[2]},7\n")
    np.testing.assert_allclose(load_points(tmp_path / "a.csv"), pts, atol=1e-9)


def test_reader_rejects_bad_input(tmp_path):
    with pytest.raises(InputDataError, match="not found"):
        load_points(tmp_path / "missing.ply")
    np.save(tmp_path / "flat.npy", np.c_[cloud()[:, :2], np.zeros(2000)])
    with pytest.raises(InputDataError, match="vertical extent"):
        load_points(tmp_path / "flat.npy")
    np.save(tmp_path / "tiny.npy", cloud(50))
    with pytest.raises(InputDataError, match="only 50 points"):
        load_points(tmp_path / "tiny.npy")
    (tmp_path / "x.unknown").write_text("1 2 3")
    load_plugins(entry_points=False)
    with pytest.raises(Exception, match="no reader"):
        load_points(tmp_path / "x.unknown")


def test_nan_points_are_dropped(tmp_path):
    pts = cloud()
    pts[5] = np.nan
    np.save(tmp_path / "n.npy", pts)
    assert len(load_points(tmp_path / "n.npy")) == len(pts) - 1


def test_ply_roundtrip(tmp_path):
    import open3d as o3d

    pts = cloud()
    pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(pts))
    o3d.io.write_point_cloud(str(tmp_path / "a.ply"), pcd)
    np.testing.assert_allclose(load_points(tmp_path / "a.ply"), pts, atol=1e-6)


def _pc2(xyz: np.ndarray, pad: int = 4, frame="map"):
    """Build a fake deserialized sensor_msgs/PointCloud2 (x, y, z float32 + intensity + padding)."""
    step = 16 + pad
    buf = np.zeros((len(xyz), step), dtype=np.uint8)
    buf[:, 0:12] = np.frombuffer(xyz.astype("<f4").tobytes(), dtype=np.uint8).reshape(-1, 12)
    fields = [
        SimpleNamespace(name=n, offset=o, datatype=7, count=1)
        for n, o in (("x", 0), ("y", 4), ("z", 8), ("intensity", 12))
    ]
    return SimpleNamespace(
        fields=fields,
        height=1,
        width=len(xyz),
        point_step=step,
        row_step=step * len(xyz),
        is_bigendian=False,
        data=buf.ravel(),
        header=SimpleNamespace(frame_id=frame),
    )


def test_pointcloud2_decode():
    xyz = cloud(100)
    np.testing.assert_allclose(pointcloud2_to_xyz(_pc2(xyz)), xyz, atol=1e-5)


def test_pointcloud2_missing_field():
    msg = _pc2(cloud(10))
    msg.fields = msg.fields[1:]
    with pytest.raises(InputDataError, match="no \\['x'\\]"):
        pointcloud2_to_xyz(msg)


def _tf(parent, child, t, xyz, quat=(0, 0, 0, 1)):
    sec = int(t)
    return SimpleNamespace(
        transforms=[
            SimpleNamespace(
                header=SimpleNamespace(
                    frame_id=parent, stamp=SimpleNamespace(sec=sec, nanosec=int((t - sec) * 1e9))
                ),
                child_frame_id=child,
                transform=SimpleNamespace(
                    translation=SimpleNamespace(x=xyz[0], y=xyz[1], z=xyz[2]),
                    rotation=SimpleNamespace(x=quat[0], y=quat[1], z=quat[2], w=quat[3]),
                ),
            )
        ]
    )


def test_tf_buffer_chain_and_interpolation():
    buf = _TfBuffer(tolerance_s=0.05)
    buf.add(_tf("map", "base", 0.0, (0, 0, 0)), static=False)
    buf.add(_tf("map", "base", 1.0, (2, 0, 0)), static=False)
    s = np.sin(np.pi / 4)
    buf.add(_tf("base", "lidar", 0.0, (0, 0, 1), (0, 0, s, s)), static=True)  # 90 deg yaw, 1 m up
    buf.finalize()
    T = buf.lookup("map", "lidar", 0.5)
    p = T[:3, :3] @ np.array([1.0, 0, 0]) + T[:3, 3]
    np.testing.assert_allclose(p, [1.0, 1.0, 1.0], atol=1e-9)
    assert buf.lookup("map", "lidar", 5.0) is None  # no extrapolation beyond tolerance
    assert buf.lookup("map", "unknown_frame", 0.5) is None


def test_interpolate_ground_z():
    g = np.array([[0, 0, 1.0], [1, 0, 1.0], [0, 1, 1.0], [1, 1, 1.0], [0.5, 0.5, 3.0]])
    z = interpolate_ground_z(g, np.array([[0.5, 0.5], [0.0, 0.0], [100, 100]]), k=4, rmax=5)
    assert z[0] == pytest.approx(3.0)  # exact hit
    assert z[1] == pytest.approx(1.0)
    assert np.isfinite(z[2])  # out of range falls back to nearest
    with pytest.raises(ParameterError):
        interpolate_ground_z(g, np.zeros((1, 2)), k=0)
    with pytest.raises(InputDataError):
        interpolate_ground_z(np.empty((0, 3)), np.zeros((1, 2)))


def test_refine_ground_removes_stem_bases():
    rng = np.random.default_rng(0)
    xy = rng.uniform(0, 10, (5000, 2))
    terrain = np.c_[xy, 0.1 * xy[:, 0] + rng.normal(0, 0.01, len(xy))]  # sloped
    base = np.c_[rng.normal(5, 0.2, (300, 2)), 0.5 + rng.uniform(0.2, 0.5, 300)]  # stem base above
    kept, lifted = refine_ground(np.r_[terrain, base], cell=1.0, tolerance=0.15)
    assert len(lifted) >= 290
    assert len(kept) >= 4950  # real ground on the slope is kept
