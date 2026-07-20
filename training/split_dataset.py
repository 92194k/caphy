"""Split the labelled dataset into train/val for YOLO.

Run this AFTER you have verified your labels. It moves a fraction of the
images (default 20%) from images/train -> images/val, and moves each image's
matching label file along with it, so the two stay paired.

    python training/split_dataset.py
    python training/split_dataset.py --val 0.15
"""
import argparse
import os
import glob
import random
import shutil

ROOT = os.path.join("training", "dataset")
IMG_TRAIN = os.path.join(ROOT, "images", "train")
IMG_VAL   = os.path.join(ROOT, "images", "val")
LBL_TRAIN = os.path.join(ROOT, "labels", "train")
LBL_VAL   = os.path.join(ROOT, "labels", "val")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--val", type=float, default=0.20, help="fraction sent to validation")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    for d in (IMG_VAL, LBL_VAL, LBL_TRAIN):
        os.makedirs(d, exist_ok=True)

    imgs = sorted(glob.glob(os.path.join(IMG_TRAIN, "*.jpg")) +
                  glob.glob(os.path.join(IMG_TRAIN, "*.png")))
    if not imgs:
        print(f"No images in {IMG_TRAIN}. Nothing to split.")
        return

    missing = [p for p in imgs if not os.path.exists(
        os.path.join(LBL_TRAIN, os.path.splitext(os.path.basename(p))[0] + ".txt"))]
    if missing:
        print(f"WARNING: {len(missing)} image(s) have no label file yet. Label them first, "
              f"or they'll train as 'no person'. Example: {os.path.basename(missing[0])}")

    random.seed(args.seed)
    random.shuffle(imgs)
    n_val = max(1, int(len(imgs) * args.val))
    val = imgs[:n_val]

    moved = 0
    for p in val:
        stem = os.path.splitext(os.path.basename(p))[0]
        shutil.move(p, os.path.join(IMG_VAL, os.path.basename(p)))
        lbl = os.path.join(LBL_TRAIN, stem + ".txt")
        if os.path.exists(lbl):
            shutil.move(lbl, os.path.join(LBL_VAL, stem + ".txt"))
        moved += 1

    print(f"Split done: {len(imgs) - moved} train / {moved} val.")
    print("Now train:  yolo detect train model=yolov8n.pt data=training/data.yaml "
          "epochs=50 imgsz=640 batch=8 name=caphy_person")


if __name__ == "__main__":
    main()
