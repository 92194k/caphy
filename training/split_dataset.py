from pathlib import Path
import random
import shutil

random.seed(42)

images_train = Path("training/dataset/images/train")
labels_train = Path("training/dataset/labels/train")

images_val = Path("training/dataset/images/val")
labels_val = Path("training/dataset/labels/val")

images_val.mkdir(parents=True, exist_ok=True)
labels_val.mkdir(parents=True, exist_ok=True)

images = list(images_train.glob("*.jpg"))
random.shuffle(images)

# 20% for validation
val_count = int(len(images) * 0.2)

for img in images[:val_count]:
    label = labels_train / f"{img.stem}.txt"

    shutil.move(str(img), images_val / img.name)

    if label.exists():
        shutil.move(str(label), labels_val / label.name)

print(f"Moved {val_count} images and labels to validation set.")
print(f"Training images remaining: {len(images)-val_count}")