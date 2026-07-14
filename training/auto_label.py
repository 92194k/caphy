"""Draft labels for your collected photos using stock YOLOv8, to speed up
annotation. THIS IS A STARTING POINT ONLY - you MUST open the images in a
labeler afterwards and fix every miss/mistake. That human verification is
what makes the labels (and your trained model) defensible.

    python training/auto_label.py
    python training/auto_label.py --conf 0.35

For each image in training/dataset/images/train it writes a YOLO-format
label file to training/dataset/labels/train with the same name:
    0 cx cy w h        (class 0 = person, coords normalised 0-1)
Images with no detected person get an empty .txt (a valid "no object" label).
"""
import argparse
import os
import glob

from ultralytics import YOLO

IMG_DIR = os.path.join("training", "dataset", "images", "train")
LBL_DIR = os.path.join("training", "dataset", "labels", "train")
PERSON_CLASS = 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", type=float, default=0.30, help="detection confidence (lower = more draft boxes)")
    ap.add_argument("--model", default="yolov8n.pt")
    args = ap.parse_args()

    os.makedirs(LBL_DIR, exist_ok=True)
    imgs = sorted(glob.glob(os.path.join(IMG_DIR, "*.jpg")) +
                  glob.glob(os.path.join(IMG_DIR, "*.png")))
    if not imgs:
        print(f"No images in {IMG_DIR}. Run collect_data.py first (or drop your photos there).")
        return

    model = YOLO(args.model)
    print(f"Drafting labels for {len(imgs)} images (conf>={args.conf})...")
    labeled = boxes_total = 0
    for p in imgs:
        r = model.predict(p, conf=args.conf, classes=[PERSON_CLASS], verbose=False)[0]
        lines = []
        for b in r.boxes:
            cx, cy, w, h = b.xywhn[0].tolist()   # normalised centre-x, centre-y, width, height
            lines.append(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        stem = os.path.splitext(os.path.basename(p))[0]
        with open(os.path.join(LBL_DIR, stem + ".txt"), "w") as f:
            f.write("\n".join(lines))
        labeled += 1
        boxes_total += len(lines)

    print(f"Done. {labeled} label files written, {boxes_total} draft person-boxes total.")
    print("\nNEXT: open the images in LabelImg or Roboflow and VERIFY every box —")
    print("add people it missed (far/dark/partly hidden), delete anything wrong.")


if __name__ == "__main__":
    main()
