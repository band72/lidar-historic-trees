# Stems that are only partly visible

When the scan sees only one side of a trunk, the program still measures it, says how much of the circumference it saw (`arc_coverage`), **flags it as a `warning`**, and **widens its interval**.

`arc_coverage` counts as unseen only the gaps wider than those that randomly spaced points leave anyway, so a sparse stem seen all round still reads close to 100 % (and is not flagged), while a stem seen from one side, or from two opposite sides, reads about 50 %.

**What happens to the measurement.** The program fits a circle to the arc it can see. For a round stem that works well even from half the circumference. A stem that is not round cannot be recognised from one side, and the fit then reflects the *local curvature*: too small if the scanner sees the flat side of an oval, too large if it sees its narrow end. Simulations on synthetic stems with known diameter:

| Stem seen | Round stem, DBH error | Oval stem (44 × 36 cm), DBH error |
|---|---|---|
| All the way round | 0.03 cm | +0.1 cm |
| About three quarters (270°) | 0.03 cm | about 5 % low |
| About half (180°) | 0.1-0.2 cm | **13 % low or 17 % high**, depending on which side faces the scanner |
| Under 40 % (120° or less) | 0.1-0.7 cm | not simulated |

**What the program does about it.**

- **Flag:** any stem whose scanned arc covers less than 60 % of the circumference (`fit.min_arc_coverage`, default 0.6, so half-visible stems are flagged) gets status `warning`, with a message such as *"low arc coverage (53 %): stem seen from one side; DBH assumes a round stem, interval widened by ±3.1 cm for possible non-roundness"*.
- **Widen:** the interval includes an allowance for the part of the stem that was not seen: `view_sd = ovality_sd × DBH × 3 × (1 − arc_coverage)`, which is zero for a stem seen all round. `fit.ovality_sd` (default 0.03, i.e. 3 %) is the assumed non-roundness of your stems, (long − short) / (long + short). The allowance is added to the measurement uncertainty, and the CSV has it as the `view_sd` column. The measured DBH itself never changes.
- **Check, and what it does and doesn't prove:** on simulated stems that really are 3 % oval, in random orientation, the 95 % interval contains the true diameter in essentially 100 % of cases at 270°, 180° and 120°; before this allowance it held only 57 % of the time at 180° and 38 % at 120°. At 6 % ovality (twice the assumption) it holds about 100 % at 270°, 90-99 % at 180° and 70-82 % at 120° (repeated simulations differ by a few percent). **These figures show the method is consistent with its own assumptions:** the factor 3 and the 3 % were chosen on the same kind of simulated stems the check uses. They do not show that real stems are 3 % oval, or that the intervals hold on your data. Only [calibration](../README.md#9-calibrating-the-confidence-interval) against tape-measured trees can show that. At the assumed ovality the intervals are, if anything, a little wider than needed (about 100 % instead of the nominal 95 %).

**Optional: complete partly seen stems (`fit.complete_partial_stems`, `--complete-partial`).** DBH is always the diameter of the full circle through the visible arc, never the width of the visible part. This setting goes further and tries to see more of the circle. For every stem that is flagged for low arc coverage, or that has too few points at breast height to be measured at all, the program widens the measurement window in 0.1 m steps (up to `fit.max_band_half_width`, default ±0.75 m around breast height) until about 85 % of the circumference has been seen, then refits. This helps when the scanner moved, so that other heights show other sides of the stem. The taller fit includes a **taper term**, so the result is still the diameter *at breast height* and a window that isn't centred (the segmentation slice starts at 1.0 m) doesn't bias it.

Simulated stems (60 per row, a 120°-wide view that turns 120° per metre of height, points only above 1.0 m, 0.5 cm/m taper):

| Stems actually | Setting | DBH RMSE | Arc coverage | Still flagged |
|---|---|---|---|---|
| 3 % oval | off | 1.64 cm | 48 % | 100 % |
| 3 % oval | **on** | **1.07 cm** | 66 % | 0 % |
| 6 % oval | off | 3.37 cm | 47 % | 100 % |
| 6 % oval | **on** | **2.55 cm** | 64 % | 12 % |

- **It changes nothing when it can't help.** If the same side is hidden at every height, the stem keeps its standard fit and its `warning`, and no note is added. Stems that are already seen well are not touched. On the synthetic benchmark plots (same side hidden at every height) results are identical with the setting on.
- **Stems it can now include.** A stem that would fail with "too few points in the band" is measured from the taller window instead of being dropped, with a note saying so.
- **It is opt-in, because it changes measurements.** Completed trees stay identifiable: `dbh_method` is *taller window (taper)* instead of *breast-height band*, `band_half_width` gives the window used, and the `message` records the step, e.g. *"seen from one side at breast height (arc coverage 48 %); completed to 66 % using breast height ±0.74 m with a taper term"*. Once 60 % of the circumference is seen the tree is no longer a `warning`, but you can always filter on `dbh_method`.
- **Its limits.** A taller window can pick up low branches or butt swell, which the robust fit down-weights but cannot remove completely. It does not make an interval honest for stems far more oval than `ovality_sd` assumes: at 6 % ovality the 95 % interval held about 92 % of the time with completion (83 % without) in this simulation. These are simulations of the same idealised kind as above, not field results.

**What the program cannot do.** The 3 % is an assumption, not a measurement of your forest. Stems that are strongly oval, fluted or buttressed (like the 44 × 36 cm example above) will still be mis-measured from one side by more than the interval says. For such species raise `fit.ovality_sd` (the app has a *Stem non-roundness* slider), or better, scan from more sides.

What to do:

- **Scan from at least two sides**, so every stem is seen all round or from both flanks. Then none of this matters.
- **Review the flagged trees.** In the table, filter by status `warning` or sort by `arc_coverage`.
- **Calibrate with partly visible trees.** Tape-measure some of them and run [calibration](../README.md#9-calibrating-the-confidence-interval); it checks how well the intervals work on your data. If the calibration shows the intervals are too narrow, raise `ovality_sd`.
