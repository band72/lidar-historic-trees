"""
Site and scanner profiles: starting settings for typical environments and instruments.

A profile is a set of overrides of the ground, segmentation and fit defaults (metres, the
keys are the parameter names, as in a ``-c`` config file). Two kinds combine:

- a **scanner** profile sets what depends on the instrument: how densely a trunk surface is
  sampled (``min_surface_density``), and for scanners that walk around trees, completing
  partly seen stems;
- a **site** profile sets tree criteria only. It never sets the density itself; it may scale
  the scanner's (``density_factor``), so "Large DBH Trees" means the same on any scanner.

Merge order: defaults <- scanner <- site <- ``-c`` config file <- command-line options.
Choosing profiles in the app moves the sliders, which can still be adjusted;
``fip inventory --profile NAME --scanner NAME`` applies the same values.

Each profile states its evidence: checked on real data, or an untested starting point. Your
own profiles (saved from the app) live in the ``profiles`` folder next to the app's
preferences and are listed after the built-in ones.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT = "Default"
DEFAULT_SCANNER = "Vehicle (mobile lidar)"
#: density the scanner profiles are relative to (FitParams' default, points per m^2)
BASE_DENSITY = 600.0


@dataclass(frozen=True)
class Profile:
    name: str
    #: one line for the dropdown's help and the run record
    summary: str
    ground: dict = field(default_factory=dict)
    segment: dict = field(default_factory=dict)
    fit: dict = field(default_factory=dict)
    #: what the values rest on: "Checked on real data: ...", "Untested: ...", "Saved by you"
    evidence: str = ""
    #: site profiles: multiplies the scanner's min_surface_density
    density_factor: float = 1.0

    def sections(self) -> dict[str, dict]:
        return {"ground": dict(self.ground), "segment": dict(self.segment), "fit": dict(self.fit)}


DRIVE = "the 2022-03-08 vehicle drive (Ouster 64, north Florida)"

PROFILES: dict[str, Profile] = {
    p.name: p
    for p in [
        Profile(
            DEFAULT,
            "The pipeline's defaults: a balanced starting point for mixed sites.",
            evidence=f"Checked on real data: {DRIVE}, all six segments, reviewed in side views.",
        ),
        Profile(
            "Roadside / Farmstead",
            "Trees along roads, yards and farm buildings, scanned from a vehicle: the defaults "
            "(which did best on such a site) with a 30 deg lean limit for open-grown trees. "
            "The roof check keeps carport and shed posts out.",
            fit={"max_tilt_deg": 30.0},
            evidence=f"Checked on real data: {DRIVE}, segment 360-480 s: the 30 deg lean limit "
            "added 3 real trees (two leaning trunks, a multi-stem tree) and lost one doubtful "
            "80 cm one-sided fit.",
        ),
        Profile(
            "Shrubs",
            "Trees among dense shrubs and saplings: a stricter trunk check, objects must reach "
            "2.5 m, short objects with a large 'DBH' (shrubs) are rejected instead of flagged, "
            "and stems under 7.5 cm DBH are not counted.",
            fit={
                "reject_non_stems": True,
                "min_dbh": 0.075,
                "min_top_above_breast_height": 1.2,
                "min_stem_dominance": 0.55,
                "max_interior_fraction": 0.15,
                "short_action": "reject",
            },
            evidence="Untested: reasoned values; the 2.5 m height keeps the 2.7-2.8 m "
            "ornamental trees of the test drive.",
        ),
        Profile(
            "Isolated Trees",
            "Open ground, parks, roadsides and fence lines, often scanned in passing from one "
            "side: smaller clusters keep neighbouring trees apart, and open-grown trees may "
            "lean more.",
            segment={"k": 100, "min_tree_points": 50},
            fit={"max_tilt_deg": 30.0},
            evidence=f"Checked on real data: {DRIVE}, segment 360-480 s, each change alone: "
            "smaller clusters (k 100) added 10 trees, of 8 reviewed 7 clear trees and a stem "
            "in a bush, and lost 2; the 30 deg lean limit added 3 real trees and lost one "
            "doubtful fit. Completing partly seen stems (+1/-2) and a lower point density "
            "(untested) were dropped.",
        ),
        Profile(
            "Clear Forest",
            "Forest with an open understory and many straight stems close together: merged "
            "stems are split at a 10 cm gap and the ground filter follows forest terrain.",
            ground={"rigidness": 2},
            fit={"split_stems": True, "stem_gap": 0.10},
            evidence="Untested: no forest segment has been processed yet.",
        ),
        # Segmentation is left at the defaults in the two big-tree profiles: coarser clusters
        # (k 250, beta 0.6, 20 cm voxels) merged neighbouring avenue trees on real data
        # (379 -> 166 clusters) and lost 50 of 110 trees; a big trunk is one cluster anyway.
        Profile(
            "Tall Wide Trees",
            "Tall trees with wide crowns: the trunk is checked up to 3.5 m, DBH up to 2 m, and "
            "sparser trunk surfaces are accepted (seen from farther away).",
            # a 1 m ground cloth, tested alone on segment 360-480 s, gained 4 and lost 5 trees
            # (one a real 24 cm tree, one refitted at a 78 deg lean): dropped
            fit={"max_radius": 1.0, "stem_check_top": 3.5},
            density_factor=400.0 / 600.0,
            evidence=f"Partly checked: {DRIVE}, segment 360-480 s (its ground cloth change "
            "was tested and dropped; the rest is untested).",
        ),
        Profile(
            "Large DBH Trees",
            "Big old and heritage trees (DBH 20 cm to 3 m): DBH up to 3 m, a 30 cm stem gap so "
            "one big trunk seen in pieces is not split, irregular and leaning trunks, sparser "
            "trunk surfaces, and stems under 20 cm are not counted.",
            # looser circle tolerances (3 cm inlier, 5 cm kernel) and a 1 m ground cloth let
            # big real trunks fit as 55-80 deg leans or change their cluster on real data
            fit={
                "max_radius": 1.5,
                "min_dbh": 0.20,
                "stem_gap": 0.30,
                "ovality_sd": 0.06,
                "max_tilt_deg": 30.0,
                # a 2.5 m object with a 52 cm "DBH" (a shrub) was one of its additions
                "short_action": "reject",
            },
            density_factor=0.5,
            evidence=f"Checked on real data: {DRIVE}, segment 360-480 s: kept 83 of the 88 "
            "trees of 20 cm or more found with the defaults; of its 8 additions, 5 were real "
            "trees (mostly leaning), 1 a shrub (now rejected) and 2 poles fitted with large "
            "circles (flagged for review).",
        ),
    ]
}

SCANNERS: dict[str, Profile] = {
    p.name: p
    for p in [
        Profile(
            DEFAULT_SCANNER,
            "A lidar on a vehicle: trunks seen from the road, mostly from one side, densely "
            "sampled near the road.",
            evidence=f"Checked on real data: {DRIVE}.",
        ),
        Profile(
            "Backpack / handheld",
            "A walking scanner: trunks seen from several sides as the operator moves, so partly "
            "seen stems are completed from a taller window.",
            fit={"complete_partial_stems": True},
            evidence="Untested: reasoned values.",
        ),
        Profile(
            "Tripod (terrestrial)",
            "Static scans from a tripod: dense near the scanner, sparse on distant trunks.",
            fit={"min_surface_density": 300.0},
            evidence="Untested: reasoned values.",
        ),
        Profile(
            "Drone (UAV)",
            "A lidar flown above or under the canopy: trunks sparsely sampled.",
            fit={"min_surface_density": 150.0},
            evidence="Untested: 150 points/m^2 follows the README's advice for UAV scans.",
        ),
    ]
}


# --- your own profiles ------------------------------------------------------------------------


def config_dir() -> Path:
    """The app's settings folder (``FIP_HOME`` if set), shared with the app's preferences."""
    if os.environ.get("FIP_HOME"):
        return Path(os.environ["FIP_HOME"])
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "LiDARTreeInventory"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "lidar-tree-inventory"


def user_profiles_dir() -> Path:
    return config_dir() / "profiles"


def _norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _user_profiles() -> dict[str, Profile]:
    out = {}
    d = user_profiles_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            p = Profile(
                doc["name"],
                doc.get("summary") or "Your saved settings.",
                ground=doc.get("ground", {}),
                segment=doc.get("segment", {}),
                fit=doc.get("fit", {}),
                evidence="Saved by you" + (f" on {doc['saved']}" if doc.get("saved") else ""),
            )
        except (OSError, ValueError, KeyError, TypeError):
            continue  # an unreadable file is skipped, not fatal
        out[p.name] = p
    return out


def all_profiles() -> dict[str, Profile]:
    """Built-in site profiles, then your saved ones."""
    return {**PROFILES, **{k: v for k, v in _user_profiles().items() if k not in PROFILES}}


def save_user_profile(name: str, sections: dict, summary: str = "") -> Path:
    """
    Save ``sections`` ({"ground", "segment", "fit"}: overrides of the defaults) as a profile.

    :raises ParameterError: an empty name, the name of a built-in profile, or values that do
        not make valid settings.
    """
    import datetime

    from . import inventory as inv
    from .errors import ParameterError
    from .traits.dbh.robust_fit import FitParams

    name = name.strip()
    if not _norm(name):
        raise ParameterError("a profile needs a name")
    if any(_norm(name) == _norm(p) for p in PROFILES):
        raise ParameterError(f"{name!r} is a built-in profile; choose another name")
    clean = {k: dict(sections.get(k) or {}) for k in ("ground", "segment", "fit")}
    try:
        inv.GroundParams(**clean["ground"])
        inv.SegmentParams(**clean["segment"])
        FitParams(**clean["fit"])
    except TypeError as exc:
        raise ParameterError(f"unknown setting in the profile: {exc}") from exc
    d = user_profiles_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_norm(name)}.json"
    doc = {"name": name, "summary": summary, "saved": datetime.date.today().isoformat(), **clean}
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    return path


def delete_user_profile(name: str) -> None:
    for f in user_profiles_dir().glob("*.json"):
        try:
            if _norm(json.loads(f.read_text(encoding="utf-8")).get("name", "")) == _norm(name):
                f.unlink()
        except (OSError, ValueError):
            continue


# --- lookup and merging ------------------------------------------------------------------------


def get(name: str) -> Profile:
    """The site profile called ``name`` (case and spaces/punctuation ignored), built-in or yours."""
    from .errors import ParameterError

    for p in all_profiles().values():
        if _norm(p.name) == _norm(name):
            return p
    raise ParameterError(
        f"unknown profile {name!r}; choose one of: {', '.join(all_profiles())}"
    )


def get_scanner(name: str) -> Profile:
    from .errors import ParameterError

    for p in SCANNERS.values():
        if _norm(p.name) == _norm(name) or _norm(p.name).startswith(_norm(name)):
            return p
    raise ParameterError(f"unknown scanner {name!r}; choose one of: {', '.join(SCANNERS)}")


def merged(
    name: str = DEFAULT, config: dict | None = None, scanner: str = DEFAULT_SCANNER
) -> dict[str, dict]:
    """
    Settings from the scanner, then the site profile ``name``, then ``config`` (a ``-c``
    file's sections) on top. The site's density_factor scales the scanner's density.
    """
    sc, site = get_scanner(scanner), get(name)
    out = sc.sections()
    for section, values in site.sections().items():
        out[section].update(values)
    if site.density_factor != 1.0:
        base = sc.fit.get("min_surface_density", BASE_DENSITY)
        out["fit"]["min_surface_density"] = round(base * site.density_factor, 1)
    for section, values in (config or {}).items():
        out.setdefault(section, {}).update(values or {})
    return out
