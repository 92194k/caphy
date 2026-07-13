"""Fine-tune YOLOv8-nano on your own CAPHY dataset.

    python training/train.py

Starts from the pretrained nano model and trains on your labelled data.
The best weights land in training/runs/detect/caphy_person/weights/best.pt.
Point config.YOLO_MODEL at that file to use your custom model.
"""
from ultralytics import YOLO


def main():
    model = YOLO("yolov8n.pt")            # start from pretrained (transfer learning)
    model.train(
        data="training/data.yaml",
        epochs=50,
        imgsz=640,
        batch=8,
        name="caphy_person",
        patience=15,                       # early-stop if it stops improving
    )
    print("\nDONE. Your model: training/runs/detect/caphy_person/weights/best.pt")
    print('In config.py set:  YOLO_MODEL = "training/runs/detect/caphy_person/weights/best.pt"')


if __name__ == "__main__":
    main()