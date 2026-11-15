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
import numpy as np
from scipy.spatial import KDTree

from ..cloud import Cloud
from ..errors import InputDataError, ParameterError
from ..utils import logger
from ..utils.decorators import log_scalars_in_arguments


def interpolate_ground_z(
    ground_xyz: np.ndarray,
    query_xy: np.ndarray,
    k: int = 5,
    p: float = 2,
    rmax: float = 10.0,
    kdtree: KDTree | None = None,
) -> np.ndarray:
    """
    Ground elevation at ``query_xy`` via k-nearest-neighbour Inverse Distance Weighting.

    Queries with no ground point within ``rmax`` fall back to the single nearest ground point.

    :param ground_xyz: (N, 3) ground points.
    :param query_xy: (M, 2) or (M, 3) query positions; only x, y are used.
    :param kdtree: optional prebuilt KDTree over ``ground_xyz[:, :2]`` to avoid rebuilding.
    :return: (M,) interpolated ground z.
    """
    ground_xyz = np.asarray(ground_xyz, dtype=float)
    query_xy = np.asarray(query_xy, dtype=float)
    if ground_xyz.ndim != 2 or ground_xyz.shape[1] < 3 or len(ground_xyz) == 0:
        raise InputDataError("ground cloud is empty; cannot interpolate ground elevation")
    if query_xy.ndim != 2 or query_xy.shape[1] < 2:
        raise InputDataError(f"query points must be (M, 2+), got shape {query_xy.shape}")
    if k < 1:
        raise ParameterError(f"IDW k must be >= 1, got {k}")
    if p <= 0:
        raise ParameterError(f"IDW power p must be > 0, got {p}")
    if len(query_xy) == 0:
        return np.empty((0,), dtype=float)

    search_z = ground_xyz[:, 2]
    k = min(k, len(ground_xyz))
    if kdtree is None:
        kdtree = KDTree(data=ground_xyz[:, :2])
    # query in blocks: k neighbours x (distance, index, weight) for tens of millions of points
    # at once peaked at +7 GB on a 45 M point field cloud
    z_interp = np.empty(len(query_xy), dtype=float)
    n_invalid = 0
    for s in range(0, len(query_xy), _BLOCK):
        q = query_xy[s : s + _BLOCK, :2]
        z_interp[s : s + len(q)], bad = _idw_block(kdtree, search_z, q, k, p, rmax)
        n_invalid += bad
    if n_invalid:
        logger.debug(
            f"no ground neighbour within rmax={rmax} for {n_invalid} points;"
            " used the single closest ground point"
        )
    return z_interp


_BLOCK = 2_000_000


def _idw_block(kdtree, search_z, q, k, p, rmax):
    dist, idx = kdtree.query(q, k=k, p=2, distance_upper_bound=rmax, workers=-1)
    dist = np.asarray(dist, dtype=float).reshape(len(q), k)
    idx = np.asarray(idx).reshape(len(q), k)
    invalid = np.flatnonzero(np.isinf(dist[:, 0]))
    if invalid.size:
        patch_dist, patch_idx = kdtree.query(q[invalid], k=1, p=2, workers=-1)
        dist[invalid, 0] = patch_dist
        idx[invalid, 0] = patch_idx
    # out-of-range neighbours have dist=inf -> weight 0; their idx==len(search_z) is clamped
    clean_idx = np.where(idx >= len(search_z), 0, idx)
    weights = 1.0 / np.power(np.maximum(dist, 1e-12), p)
    weights[np.isinf(dist)] = 0.0
    z = np.sum(weights * search_z[clean_idx], axis=1) / np.sum(weights, axis=1)
    exact = dist[:, 0] == 0  # exact hits: take that ground point's z directly
    z[exact] = search_z[clean_idx[exact, 0]]
    return z, int(invalid.size)


@log_scalars_in_arguments()
def normalize_height(
    non_ground_cloud: Cloud,
    ground_cloud: Cloud,
    k: int = 5,
    p: int = 2,
    rmax: float = 10.0,
) -> Cloud:
    """
    Normalizes non_ground_cloud using the 2d information (points) from
    ground_cloud. Runs Knn Inverse Distance Weighting (IDW).

    :param k: number of nearest neighbours to use for IDW.
    :param p: power factor for IDW.
    :param rmax: max distance knn queries.
    :return: the normalized version of non_ground_cloud.
    """
    logger.debug("Interpolating ground elevation under", non_ground_cloud.num_points, "points")
    z_interp = interpolate_ground_z(
        ground_cloud.points, non_ground_cloud.points, k=k, p=p, rmax=rmax
    )
    logger.debug("Knn IDW interpolation complete.")
    normalized_cloud = non_ground_cloud.clone()
    normalized_cloud.points[:, 2] = normalized_cloud.points[:, 2] - z_interp
    return normalized_cloud
