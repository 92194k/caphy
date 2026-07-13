# CAPHY - Custom YOLO Training Kit

Fine-tune YOLOv8-nano on images from *your own* camera/environment so detection
is more accurate for your setup. Four steps: collect, label, train, use.

## Folder layout (create this inside your project)
```
training/
├── collect_data.py
├── train.py
├── data.yaml
└── dataset/
    ├── images/
    │   ├── train/   <- most images go here
    │   └── val/     <- ~15-20% of images for validation
    └── labels/
        ├── train/   <- one .txt per training image
        └── val/     <- one .txt per validation image
```

## Step 1 - Collect images
```
python training/collect_data.py
```
A webcam window opens. Press **SPACE** to save each frame, **q** to quit.
Capture **150-300** varied shots: different distances, angles, lighting, and
different people (and some empty-room shots too). They save to
`training/dataset/images/train/`.

## Step 2 - Label the images (draw boxes around each person)
Use one of these free tools:
- **Roboflow** (easiest, web-based, roboflow.com) - upload images, draw boxes,
  export as **"YOLOv8"** format. It gives you the `images/` + `labels/` folders
  already split into train/val - just drop them into `training/dataset/`.
- **LabelImg** (desktop app) - `pip install labelImg`, set format to **YOLO**,
  draw a box around each person, save. It writes a `.txt` next to each image.

Each label `.txt` has one line per person: `0 x_center y_center width height`
(all values 0-1). Class `0` = person. Move ~15-20% of your images + their
matching `.txt` files into the `val/` folders.

## Step 3 - Train
```
pip install ultralytics      # if not already
python training/train.py
```
This fine-tunes for up to 50 epochs (stops early if it stops improving).
On a CPU it's slow (hours) - if you have any NVIDIA GPU it's much faster.
When done, your model is at:
```
training/runs/detect/caphy_person/weights/best.pt
```

## Step 4 - Use your model
In `config.py`, change:
```python
YOLO_MODEL = "training/runs/detect/caphy_person/weights/best.pt"
```
Now `main.py` and the dashboard use YOUR trained model instead of the default.

## Tips
- More varied images = better accuracy. Quality of labels matters most.
- Keep class `0` = person so the tier engine keeps working unchanged.
- If training is too slow on your laptop, use Google Colab (free GPU): upload
  the `dataset/` and `data.yaml`, run the same `train.py` there, download `best.pt`.