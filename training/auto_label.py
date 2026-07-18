from pathlib import Path
from ultralytics import YOLO

# Load pretrained YOLO model
model = YOLO("yolov8n.pt")

images_dir = Path("training/dataset/images/train")
labels_dir = Path("training/dataset/labels/train")

labels_dir.mkdir(parents=True, exist_ok=True)

image_files = list(images_dir.glob("*.jpg"))

print(f"Found {len(image_files)} images.")

for img_path in image_files:
    results = model(img_path)

    label_path = labels_dir / f"{img_path.stem}.txt"

    with open(label_path, "w") as f:
        for result in results:
            boxes = result.boxes

            if boxes is None:
                continue

            for box in boxes:
                cls = int(box.cls[0])

                # Keep only PERSON (COCO class 0)
                if cls != 0:
                    continue

                x, y, w, h = box.xywhn[0].tolist()

                f.write(f"0 {x:.6f} {y:.6f} {w:.6f} {h:.6f}\n")

print("Done! Labels generated.")