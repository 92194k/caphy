# Collecting evaluation data (thesis Chapter 4)

Your two-factor claim currently has no numbers behind it. This is how you get
them. Budget **one afternoon plus one evening.**

Everything is recorded to `caphy.db` and turned into statistics by
`tools\evaluate.py`.

---

## How it works

`config.py` has three switches:

```python
EVAL_LOGGING      = True            # record every motion event
EVAL_SESSION      = "daylight-person"   # label for this run
EVAL_GROUND_TRUTH = "person"        # what SHOULD happen
```

`EVAL_GROUND_TRUTH` is the important one. You are telling CAPHY the correct
answer in advance, so it can score itself:

- `"person"` - a real person IS in frame. Anything not detected is a **miss**.
- `"no_person"` - only a pet, branch, shadow, curtain, passing car. Anything
  detected is a **false alarm**.

Edit the three values, restart `python app.py`, run the scenario, stop.
Repeat for the next scenario.

**Set `EVAL_LOGGING = False` when you're done**, or normal use will keep
filling the database and pollute your results.

---

## The six runs

Do all six. Each is 10-15 minutes. Aim for **at least 30 motion events per
run** - walk in and out of frame repeatedly rather than standing still.

| # | EVAL_SESSION | EVAL_GROUND_TRUTH | What to do |
|---|---|---|---|
| 1 | `daylight-person` | `person` | Walk in/out of frame ~20 times. Vary distance: far, medium, close. |
| 2 | `daylight-nonhuman` | `no_person` | Make motion with NO person: throw a ball through frame, wave a branch, let a pet walk, open a curtain, point at a busy window. |
| 3 | `night-person` | `person` | Same as #1 with lights off, night vision on. |
| 4 | `night-nonhuman` | `no_person` | Same as #2 at night. Headlights and moving shadows are ideal. |
| 5 | `angle-person` | `person` | Person at odd angles: back turned, face covered, crouching, partially behind furniture. **This is your silhouette-not-face claim - prove it.** |
| 6 | `idle-empty` | `no_person` | Leave the room empty for 15 minutes. Ideally zero events. Any event here is a pure false alarm. |

> Run #5 matters most for your defence. Your thesis argues CAPHY works
> regardless of face visibility. Run #6 matters second - a system that alerts
> on an empty room is unusable.

---

## Reading the results

```powershell
python tools\evaluate.py --list                        # what you've recorded
python tools\evaluate.py                               # everything combined
python tools\evaluate.py --session daylight-nonhuman   # one run
python tools\evaluate.py --csv results.csv             # raw rows for Excel/SPSS
```

You get three things:

**1. False alarm reduction** - your headline number. A motion-only system
alerts on every motion event; CAPHY alerts only on confirmed people. The gap is
what your contribution prevented. Expect this to be high in the `no_person`
runs - that IS the finding.

**2. Precision / recall / F1** - the standard metrics a panel expects.

- **Precision** - of the alerts raised, how many were real
- **Recall** - of the real intruders, how many were caught
- **F1** - the balance of the two

**3. Tier distribution, distance range, confidence, FPS** - for the
performance section.

---

## What to put in Chapter 4

A table like this, one row per run:

| Scenario | Motion events | Confirmed | Suppressed | Precision | Recall |
|---|---|---|---|---|---|
| Daylight, person | | | | | |
| Daylight, non-human | | | | | |
| Night, person | | | | | |
| Night, non-human | | | | | |
| Obscured angles | | | | | |
| Empty room | | | | | |
| **Overall** | | | | | |

Then the comparison that makes the argument:

> A motion-only baseline would have raised **N** alerts across all scenarios.
> CAPHY's two-factor validation raised **M**, suppressing **X%** of non-human
> motion while maintaining **R%** recall on genuine intrusions.

---

## Be honest about misses

If recall is below 1.0, **say so and discuss it**. For a security system a
missed intruder is worse than a false alarm, and a panel that spots you hiding
a false-negative will trust nothing else in the chapter. Owning the limitation
and explaining the tradeoff (`PERSON_CONF` in `config.py` trades recall against
precision) is a stronger answer than a suspiciously perfect table.

---

## Before you start

1. **Calibrate `DISTANCE_K` first** (see RUN.md). Tier numbers collected with an
   uncalibrated constant are not reportable.
2. Check `HIGHEST_SECURITY = False` - if it's on, everything reports Tier 3.
3. Clear old test data: `python tools\evaluate.py --reset`
4. Keep the camera in the same position for all six runs.
5. Write down the conditions - time of day, lighting, camera height and angle,
   room size. Chapter 3 needs them for reproducibility.
