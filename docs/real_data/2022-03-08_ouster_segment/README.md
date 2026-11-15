# Real-data check: vehicle-mounted Ouster lidar, 2-minute segment

**Status: partial results.** There is no field (tape) DBH for this data, so these files show what the pipeline does on real mobile-lidar data. They are plausibility checks, not accuracy figures.

## Data

- **Source:** a 2022-03-08 field recording, 11.4 min, 27 GB ROS1 bag. It contains a 64-beam Ouster lidar at 10 Hz (`/os_cloud_node/points`), a MicroStrain GNSS/INS and an IMU, recorded from a vehicle moving at a median 3.3 m/s along a 2.2 km route.
- **No usable poses:** the bag has `/tf_static` only. GNSS/INS uncertainty is about 0.4 m, with no RTK fix, and nothing in the bag links the lidar to the INS. The scans therefore had to be stitched with lidar odometry.
- **Segment:** the first 120 s, 1,198 scans along 215 m of route. It was stitched with `scripts/register_rosbag_kiss_icp.py` (KISS-ICP 1.3.0, motion correction from per-point times, levelled with the lidar IMU) into 45 M points at 1 cm, in local metres with the origin at the first scan. The INS distance over the same time is about 222 m (+3 %).
- **The scene:** mostly open ground with fences and buildings. The forest is an edge 15-25 m to one side, seen from one side only.

## Files

| File | Content |
|---|---|
| `trees.csv` | The `fip inventory` table for the segment (default settings, trunk check on), including the rejected clusters with their reasons |
| `run.json` | Run record: parameters, summary, output checksums. It reports version "0.1.0" because the development install's version metadata was stale; the code was the development version between 0.1.3 and 0.1.4 |
| `trajectory.csv` | Vehicle path from KISS-ICP (time, x, y, z in local metres) |
| `before_trunk_check_*.png` | The same segment before the trunk check: 227 clusters were measured, most of them shrubs, posts, walls and scatter |
| `dxf_in_cad_before_trunk_check.png` | `trees.dxf` from the first run (before the trunk check) opened in a CAD program, 2D wireframe: DBH circles, CI rings and labels for all 227 fitted clusters |
| `accepted_*.png` | After the trunk check: 14 accepted. Overview of the breast-height slice with DBH circles, and close-ups of the accepted stems |

The stitched cloud (135 MB) and the segmented points (67 MB) are too large for the repository. They can be regenerated with:

```bash
python scripts/register_rosbag_kiss_icp.py 2022-03-08-12-42-07.bag --start 0 --duration 120 -o segment_0-120s.laz
fip inventory segment_0-120s.laz -o inventory -e csv -e dxf -e geojson -e points
```

## What it showed, and what changed because of it

| Finding | Change |
|---|---|
| 227 of 308 clusters got a DBH; image review showed most were not trunks | Trunk check (on by default). It accepts 14. Clear trunks keep crisp rings; 4 posts holding a roof still pass |
| Iteration-limit warning on 71 % of trees, although DBH changed < 0.6 mm with 20x more iterations | Realistic convergence test |
| 16.7 GB peak memory | 7.4 GB (merged-tree splitting on 2 cm cells), identical results |
| Ouster "no return" pixels decoded as (0, 0, 0) | Dropped by the bag reader (they would trace the vehicle path once scans are stitched) |

The test fixture `tests/data/ouster_real_2scans.bag` (0.8 MB) is two scans from the same recording, with 8 of the 64 rings kept and the byte layout unchanged, plus its `/tf_static`. It contains no GNSS or other topics.

## Still needed

- A segment **inside** the forest, where stems are seen from several sides.
- **Tape-measured DBH** for 15-30 trees in that segment, to measure accuracy and calibrate the intervals.
