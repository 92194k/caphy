# A2: Distance Calibration Guide (DISTANCE_K)

**Objective:** Set the `DISTANCE_K` constant so bounding-box height → distance conversion is accurate.

**Why this matters:** The 3-Tier Threat Level depends on distance. Panel needs to know the threshold values are real, not guesses.

## What is DISTANCE_K?

Currently in `config.py`:
```python
DISTANCE_K      = 900.0
TIER1_MIN_DIST  = 4.5     # farther than this -> Tier 1
TIER3_MAX_DIST  = 2.5     # closer than this  -> Tier 3
```

The formula is:
```
distance_meters = DISTANCE_K / yolo_bbox_height_in_pixels
```

On first run, `DISTANCE_K` is a rough guess. We calibrate it once with real data.

## Calibration Steps

### 1. Setup
- [ ] Run `python app.py` (laptop with camera)
- [ ] Open dashboard http://127.0.0.1:5000
- [ ] Have a person available to stand at known distances
- [ ] Measure distances with a tape measure or app (e.g., Google Measure)
- [ ] Ensure good lighting (daylight preferred)

### 2. Record box heights at known distance

Stand the person at **exactly 3.0 meters** from the camera (measure with tape measure):

- [ ] Person stands centered in frame
- [ ] Wait for YOLOv8 to detect (green box appears)
- [ ] Let the system settle (3-5 frames for stable box)
- [ ] Read the bounding box **height in pixels** from the dashboard debug output or browser console
- [ ] Write down: `box_height_at_3m = _____ pixels`

Repeat at other distances if you have space:
- [ ] 2.0 meters: `box_height_at_2m = _____ pixels`
- [ ] 1.5 meters: `box_height_at_1.5m = _____ pixels`

### 3. Calculate new DISTANCE_K

Using the 3-meter measurement:
```
DISTANCE_K_new = 3.0 meters × box_height_at_3m
```

**Example:**
- Person at 3.0 m → box height = 300 pixels
- DISTANCE_K_new = 3.0 × 300 = **900**

If you have multiple distances, average them:
```
DISTANCE_K_new = mean(
  3.0 × box_height_at_3m,
  2.0 × box_height_at_2m,
  1.5 × box_height_at_1.5m
)
```

### 4. Update config.py

Open `C:\GitHub\CAPHY\config.py`, find:
```python
DISTANCE_K      = 900.0
```

Replace with your calculated value:
```python
DISTANCE_K      = <YOUR_DISTANCE_K_NEW>
```

Restart `python app.py` to apply.

### 5. Verify

Test the new constant:
- [ ] Person at 4.5 m (Tier 1 threshold) → system should log as Tier 1
- [ ] Person at 3.0 m (Tier 2) → system should log as Tier 2
- [ ] Person at 2.5 m (Tier 3 threshold) → system should log as Tier 3

If the tiers are off by 0.5 m or more, recalibrate. If within 0.5 m, it's good enough.

## Documentation for Thesis

After calibration, add to **Chapter III (Research Methodology)**, in the section "Distance Calibration":

> "The distance constant (DISTANCE_K) was calibrated by measuring the YOLOv8 bounding-box height when a person stood at exactly 3.0 meters from the camera. The resulting box height in pixels was multiplied by the distance to calculate DISTANCE_K. This single-point calibration provides a linear relationship between bounding-box height and distance, suitable for the 3-Tier Threat Level escalation logic used in evaluation."

Then update the "Scope and Limitations" to state:
> "Distance thresholds (Tier 1 ≥ 4.5 m, Tier 3 ≤ 2.5 m) were calibrated and verified through real-world testing."

## Troubleshooting

**"I can't get a clear box height reading"**
- Increase lighting
- Increase PERSON_CONF in config.py (more strict detection)
- Zoom camera to frame person larger

**"Tiers are still wrong after recalibration"**
- Check TIER_SMOOTHING and TIER_HYSTERESIS in config.py (may need tuning)
- Re-read the box height (jitter of ±5 pixels is normal)

**"Can't measure 3 meters in my space"**
- Use whatever distance you have (e.g., 2 meters)
- Document it: "calibrated at 2.0 meters, DISTANCE_K = ..."
- Still valid for thesis, just different scale

## Success Criteria
✅ DISTANCE_K updated in config.py  
✅ Tier thresholds verified within ±0.5 m  
✅ Calibration method documented in thesis Chapter III  
✅ Ready for A5 (brownout test) and evaluation
