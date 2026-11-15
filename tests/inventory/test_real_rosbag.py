"""
Regression tests on REAL robot data: two Ouster OS-series scans (8 of 64 rings kept, rows
byte-for-byte unchanged) and the /tf_static of a 2022 field recording. The full bag has no
/tf, only /tf_static, and scans in the sensor frame - a common real-world case.
"""

from pathlib import Path

import numpy as np
import pytest

from forest_inventory_pipeline import inventory as inv
from forest_inventory_pipeline.errors import InputDataError

pytest.importorskip("rosbags")

BAG = Path(__file__).parents[1] / "data" / "ouster_real_2scans.bag"
FIRST_RETURN = np.array([-23.1926, 1.2757, 7.1117])  # first non-empty pixel of scan 1, os_sensor
RETURNS_SCAN1 = 7670  # of 8 x 1024 pixels; the other 522 are no-return (0, 0, 0)


def test_moving_sensor_frame_scans_are_refused_by_default():
    with pytest.raises(InputDataError, match=r"sensor frame\(s\) \['os_sensor'\]"):
        inv.load_cloud(BAG)


def test_real_ouster_layout_decodes_and_drops_no_return_pixels():
    cloud = inv.load_cloud(BAG, max_messages=1, allow_sensor_frame="true", voxel_size="0")
    assert len(cloud.xyz) == RETURNS_SCAN1  # no (0, 0, 0) pixels left
    assert not np.any(np.all(cloud.xyz == 0.0, axis=1))
    np.testing.assert_allclose(cloud.xyz[0], FIRST_RETURN, atol=1e-4)
    assert cloud.units == "m"


def test_real_tf_static_transform():
    """os_sensor -> os_lidar in this bag: 180 deg about z, 3.6 cm up."""
    cloud = inv.load_cloud(BAG, max_messages=1, target_frame="os_lidar", voxel_size="0")
    expected = np.array([-FIRST_RETURN[0], -FIRST_RETURN[1], FIRST_RETURN[2] - 0.036])
    np.testing.assert_allclose(cloud.xyz[0], expected, atol=2e-3)


def test_unreachable_target_frame_is_reported():
    with pytest.raises(InputDataError, match="tf-skipped 2"):
        inv.load_cloud(BAG, target_frame="map")
