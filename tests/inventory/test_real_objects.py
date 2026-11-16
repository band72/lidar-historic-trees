"""
Regression tests on REAL objects from the 2022-03-08 Ouster drive (segment 0-120 s), each cut
out around its position and height-normalized (tests/data/real_objects_2022-03-08.npz):

- ``<name>__fit``: points within 0.8 m, 1-4 m above ground (roughly what a cluster holds);
- ``<name>__overhead``: points within 1.5 m, 1.5-8 m, thinned to 5 cm (the roof check's input).

Reviewed by eye in side and top views; the shed/carport reading was confirmed by the site
owner. The cases are the ones the checks got wrong at some point: narrow strips of real
stems seen through gaps, a lean that a strip cannot fix, roof posts that pass every trunk
check, and a post under a roof with a tree crown above it.
"""

from pathlib import Path

import numpy as np
import pytest

from forest_inventory_pipeline.errors import FitError
from forest_inventory_pipeline.structures import OverheadIndex
from forest_inventory_pipeline.traits.dbh import robust_fit as rf

DATA = np.load(Path(__file__).parents[1] / "data" / "real_objects_2022-03-08.npz")
FP = rf.FitParams(breast_height=1.3716)  # the US breast height used on this drive
TREES = ["tree_strip_56", "tree_strip_62", "tree_ambiguous_29", "tree_big_21", "tree_36"]
ROOFED = ["post_180", "post_237", "post_264", "post_285", "post_286", "struct_20", "struct_89"]


def fit(name):
    return rf.fit_stem(DATA[name + "__fit"].astype(float), FP)


@pytest.mark.parametrize("name, lo, hi", [("tree_strip_56", 0.20, 0.28), ("tree_strip_62", 0.30, 0.40)])
def test_strips_of_real_stems_keep_their_tree_with_a_lower_bound(name, lo, hi):
    """Seen through a fence / between stems: 80 cm phantoms before, then rejected; now kept."""
    f = fit(name)
    assert f.dbh_method == rf.VISIBLE_WIDTH and f.status == "warning"
    assert lo <= f.dbh <= hi
    assert f.ci_low < f.dbh < f.ci_high
    # placed next to the strip (about 25-35 cm from the cut-out origin), not 40 cm deep
    assert np.hypot(f.center_x, f.center_y) < 0.45


def test_strip_lean_is_not_a_reason_to_reject():
    """Tree 56's strip fitted a 55 deg lean; the stem is upright."""
    assert fit("tree_strip_56").tilt_deg == 0.0


def test_lower_bound_is_stable_under_small_changes():
    pts = DATA["tree_strip_62__fit"].astype(float)
    rng = np.random.default_rng(0)
    a = rf.fit_stem(pts, FP).dbh
    b = rf.fit_stem(pts + rng.normal(0, 0.002, pts.shape), FP).dbh
    c = rf.fit_stem(pts[rng.random(len(pts)) < 0.8], FP).dbh
    assert max(abs(a - b), abs(a - c)) < 0.01


@pytest.mark.parametrize("name, dbh", [("tree_big_21", 0.521), ("tree_36", 0.394)])
def test_well_seen_trees_are_measured_as_before(name, dbh):
    f = fit(name)
    assert f.dbh_method == "breast-height band"
    assert f.dbh == pytest.approx(dbh, abs=0.01)


def test_ambiguous_one_sided_cluster_stays_rejected():
    """Two arcs in a '7' shape; once a 105 cm phantom. Rejected rather than guessed."""
    with pytest.raises(FitError, match="one side"):
        fit("tree_ambiguous_29")


def _roof(name):
    return OverheadIndex(DATA[name + "__overhead"].astype(float)).roof(
        0.0, 0.0, 0.15, FP.breast_height + 0.5
    )


@pytest.mark.parametrize("name", ROOFED)
def test_roof_posts_and_structures_are_under_a_roof(name):
    r = _roof(name)
    assert r is not None and r.is_roof(FP.roof_layer_fraction, FP.roof_min_span), r


def test_post_under_a_roof_with_a_crown_above_is_still_a_post():
    r = _roof("post_264")  # a tree's crown hangs over this carport
    assert r.above_fraction > 0.5 and r.is_roof(FP.roof_layer_fraction, FP.roof_min_span)


@pytest.mark.parametrize("name", TREES)
def test_trees_are_not_under_a_roof(name):
    r = _roof(name)
    assert r is None or not r.is_roof(FP.roof_layer_fraction, FP.roof_min_span), r
    assert r is None or r.layer_fraction < FP.roof_layer_fraction / 1.5  # with margin
