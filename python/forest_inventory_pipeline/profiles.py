"""
Site profiles: starting settings for typical environments.

A profile is a set of overrides of the ground, segmentation and fit defaults (metres, the
keys are the parameter names, as in a ``-c`` config file). Choosing one in the app moves the
sliders, which can still be adjusted; ``fip inventory --profile NAME`` applies the same
values, with a config file and command-line options taking precedence over it.

The values are reasoned starting points, not calibrated settings: check the results on
your own data (the app's map, tree detail and the rejected clusters) and adjust.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT = "Default"


@dataclass(frozen=True)
class Profile:
    name: str
    #: one line for the dropdown's help and the run record
    summary: str
    ground: dict = field(default_factory=dict)
    segment: dict = field(default_factory=dict)
    fit: dict = field(default_factory=dict)

    def sections(self) -> dict[str, dict]:
        return {"ground": dict(self.ground), "segment": dict(self.segment), "fit": dict(self.fit)}


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in [
        Profile(DEFAULT, "The pipeline's defaults: a balanced starting point for mixed sites."),
        Profile(
            "Shrubs",
            "Trees among dense shrubs and saplings: larger clusters so shrub fragments do not "
            "become trees, a stricter trunk check, objects must reach 2.9 m, and stems under "
            "7.5 cm DBH are not counted.",
            segment={"k": 200, "beta": 0.6, "min_tree_points": 100},
            fit={
                "reject_non_stems": True,
                "min_dbh": 0.075,
                "min_top_above_breast_height": 1.5,
                "min_stem_dominance": 0.55,
                "max_interior_fraction": 0.15,
            },
        ),
        Profile(
            "Isolated Trees",
            "Open ground, parks, roadsides and fence lines, often scanned in passing from one "
            "side: smaller clusters keep neighbouring trees apart, open-grown trees may lean "
            "more, and stems seen from one side are completed from a taller window.",
            segment={"k": 100, "min_tree_points": 50},
            fit={
                "max_tilt_deg": 30.0,
                "min_surface_density": 400.0,
                "complete_partial_stems": True,
            },
        ),
        Profile(
            "Clear Forest",
            "Forest with an open understory and many straight stems close together: merged "
            "stems are split at a 10 cm gap, steep tilts (usually branches) are rejected, and "
            "the ground filter follows forest terrain.",
            ground={"rigidness": 2},
            fit={"max_tilt_deg": 15.0, "split_stems": True, "stem_gap": 0.10},
        ),
        Profile(
            "Tall Wide Trees",
            "Tall trees with wide crowns: a taller segmentation slice (1-6 m) and larger "
            "clusters so one crown is not split into several trees, the trunk is checked up to "
            "3.5 m, DBH up to 2 m, and a coarser ground cloth bridges the ground hidden under "
            "the crowns.",
            ground={"cloth_resolution": 1.0},
            segment={"k": 250, "beta": 0.6, "voxel_size": 0.2, "max_z": 6.0, "min_tree_points": 200},
            fit={
                "max_radius": 1.0,
                "stem_check_top": 3.5,
                "min_surface_density": 400.0,
            },
        ),
        Profile(
            "Large DBH Trees",
            "Big old and heritage trees (DBH 20 cm to 3 m): larger clusters, DBH up to 3 m, "
            "a 30 cm stem gap so one big trunk seen in pieces is not split, tolerance for rough "
            "bark, irregular stems and leaning trunks, and stems under 20 cm are not counted.",
            ground={"cloth_resolution": 1.0},
            segment={"k": 250, "beta": 0.6, "voxel_size": 0.2, "min_tree_points": 200},
            fit={
                "max_radius": 1.5,
                "min_dbh": 0.20,
                "stem_gap": 0.30,
                "inlier_tolerance": 0.03,
                "kernel_scale": 0.05,
                "ovality_sd": 0.06,
                "max_tilt_deg": 30.0,
                "min_surface_density": 300.0,
            },
        ),
    ]
}


def get(name: str) -> Profile:
    """The profile called ``name`` (case and spaces/underscores/hyphens ignored)."""
    from .errors import ParameterError

    def norm(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum())

    for p in PROFILES.values():
        if norm(p.name) == norm(name):
            return p
    raise ParameterError(f"unknown profile {name!r}; choose one of: {', '.join(PROFILES)}")


def merged(name: str, config: dict | None = None) -> dict[str, dict]:
    """The profile's sections with ``config`` (a ``-c`` file's sections) applied on top."""
    out = get(name).sections()
    for section, values in (config or {}).items():
        out.setdefault(section, {}).update(values or {})
    return out
