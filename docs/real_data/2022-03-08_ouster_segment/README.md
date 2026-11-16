# Real-data check: vehicle-mounted Ouster lidar, 2-minute segment

**Status: partial results.** There is no field (tape) DBH for this data, so these files show what the pipeline does on real mobile-lidar data. They are plausibility checks, not accuracy figures.

## Data

- **Source:** a 2022-03-08 field recording, 11.4 min, 27 GB ROS1 bag. It contains a 64-beam Ouster lidar at 10 Hz (`/os_cloud_node/points`), a MicroStrain GNSS/INS and an IMU, recorded from a vehicle moving at a median 3.3 m/s along a 2.2 km route.
- **No usable poses:** the bag has `/tf_static` only. GNSS/INS uncertainty is about 0.4 m, with no RTK fix, and nothing in the bag links the lidar to the INS. The scans therefore had to be stitched with lidar odometry.
- **Segment:** the first 120 s, 1,198 scans along 215 m of route. It was stitched with `scripts/register_rosbag_kiss_icp.py` (KISS-ICP 1.3.0, motion correction from per-point times, levelled with the lidar IMU) into 45 M points at 1 cm, in local metres with the origin at the first scan. The INS distance over the same time is about 222 m (+3 %).
- **The scene:** mostly open ground with fences and buildings. The forest is an edge 15-25 m to one side, seen from one side only.

## Site location

A rural site in north Florida. Coordinates are WGS84 latitude/longitude in decimal degrees, from the bag's GNSS (`/gnss1/fix`). They are also in [`site_location.json`](site_location.json).

| | Latitude | Longitude |
|---|---|---|
| **Processed segment (first 120 s), centre** | **30.081775** | **-83.094395** |
| Recording start | 30.081531 | -83.094380 |
| Processed segment, end | 30.082648 | -83.094319 |
| Whole route (684 s), centre | 30.084460 | -83.095278 |
| Whole route, extent | 30.081480 to 30.087935 | -83.096426 to -83.094179 |

The whole route covers about 720 m north-south by 220 m east-west, and starts and ends at nearly the same spot.

**GNSS RTK was not in use on this rural site.** These are standalone, uncorrected fixes. The receiver reports a horizontal uncertainty of about ±1 m (median 0.96 m), which is good enough to locate the site and the route, but not individual trees to centimetre level. The recorded altitude (about -14 m) is most likely height above the WGS84 ellipsoid rather than above sea level. Sea level there is roughly 29 m below the ellipsoid, which would put the ground at about +15 m; this has not been verified.

The tree table (`trees.csv`) and `trajectory.csv` are in local metres from the stitching (origin at the first scan), not latitude/longitude. Fitting the stitched trajectory to the GNSS track could convert them, with about 1 m accuracy.

## Files

| File | Content |
|---|---|
| `trees.csv` | The `fip inventory` table for the segment with version 0.1.6 (default settings, lengths in feet), including the rejected clusters with their reasons: 11 trees, the rest rejected (mostly posts under roofs) |
| `run.json` | Run record of that run: parameters, summary, output checksums |
| `site_location.json` | Site and route coordinates (WGS84), GNSS quality and the no-RTK note |
| `trajectory.csv` | Vehicle path from KISS-ICP (time, x, y, z in local metres) |
| `before_trunk_check_*.png` | The same segment before the trunk check: 227 clusters were measured, most of them shrubs, posts, walls and scatter |
| `dxf_before_trunk_check.png`, `dxf_after_trunk_check.png` | This segment's `trees.dxf` from the first and the final run, rendered from the files with ezdxf (227 and 53 DBH circles) |
| `dxf_after_trunk_check_zoom.png` | Close-up of a DXF from the earlier run, on the posts of the open shed (then still taken for trees): circles, interval rings and labels |
| `shed_row_side_view.png` | The open shed (pole barn), 40 m long: posts 2.4 m apart under one roof at 2-3.3 m, a beam at about 2.1 m, nothing above. Earlier taken for a planted row of trees |
| `roof_posts_rejected.png` | Ten objects the roof check rejects, away from the shed: porch, carport and shed posts, two of them under a tree crown |
| `dxf_in_cad_example.png` | An earlier DXF export opened in a CAD program (2D wireframe), showing how the export looks in CAD; not from this segment's runs |
| `accepted_*.png` | From the earlier run with the crown-aware trunk check and the height check (53 accepted, many of them roof posts). Overview of the breast-height slice with DBH circles, and close-ups |

The stitched cloud (135 MB) and the segmented points (67 MB) are too large for the repository. They can be regenerated with:

```bash
python scripts/register_rosbag_kiss_icp.py 2022-03-08-12-42-07.bag --start 0 --duration 120 -o segment_0-120s.laz
fip inventory segment_0-120s.laz -o inventory -e csv -e dxf -e geojson -e points
```

## What it showed, and what changed because of it

| Finding | Change |
|---|---|
| 227 of 308 clusters got a DBH; image review showed most were not trunks | Trunk check (on by default) |
| The first trunk check kept 14. A row of 16 evenly spaced objects under a flat layer was then taken for a planted row of trees with low crowns, and the check zone was made to end at the crown base: 54 measured | The site owner confirmed (2026-11-16) that the row is an **open shed**: the "crown" is its roof. The crown-base zone is kept (real trees with low crowns exist), and a roof check was added (below) |
| One accepted "tree" was a low shrub mound (84 cm "DBH", 1.7 m tall) | Objects must reach 1 m above breast height: 53 measured, only the mound removed. Remaining doubtful objects can be excluded by a reviewer in the app |
| Iteration-limit warning on 71 % of trees, although DBH changed < 0.6 mm with 20x more iterations | Realistic convergence test |
| 16.7 GB peak memory | 7.4 GB (merged-tree splitting on 2 cm cells), identical results |
| Ouster "no return" pixels decoded as (0, 0, 0) | Dropped by the bag reader (they would trace the vehicle path once scans are stitched) |
| Narrow strips of real stems seen through a fence or between stems fitted 80-130 cm circles (centres 30-40 cm off), or were rejected for a 55° "lean" | Kept, with the visible width as a lower-bound DBH and the position half a width behind the strip (`dbh_method` *visible width (lower bound)*) |
| 36 accepted objects were posts of the shed, carports and porches: round, hollow, continuous, tall enough | Roof check: at 3 cm, 28-60 % of the material in the layer above a post lies on one plane (trees: 8-12 %). On this segment 53 → 11 trees. A sample of 10 rejections away from the shed: all posts (`roof_posts_rejected.png`); the 15 accepted before the last fix: 7 trees, 8 posts, which fitting the plane to raw elevations instead of terrain-normalised heights then caught |

The test fixture `tests/data/ouster_real_2scans.bag` (0.8 MB) is two scans from the same recording, with 8 of the 64 rings kept and the byte layout unchanged, plus its `/tf_static`. It contains no GNSS or other topics.

## Still needed

- A segment **inside** the forest, where stems are seen from several sides.
- **Tape-measured DBH** for 15-30 trees, to measure accuracy and calibrate the intervals: the whole drive's [`field_sheet.csv`](../2022-03-08_drive/field_sheet.csv) and [`trees_merged.gpx`](../2022-03-08_drive/trees_merged.gpx) are ready for this (see [the whole drive](../2022-03-08_drive/README.md)).

`tests/data/real_objects_2022-03-08.npz` (1.1 MB) holds 12 objects of this drive (strip trees, an ambiguous cluster, well-seen trees, roof posts and structures) cut out around their positions; `tests/inventory/test_real_objects.py` pins each one's outcome. `tests/data/shed_row_2022-03-08.laz` is a 7 m strip along the shed.
