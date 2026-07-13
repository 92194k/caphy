"""Capture frames from your webcam to build a training dataset.

    python training/collect_data.py           # uses webcam 0
    python training/collect_data.py --source 1

Keys:  SPACE = save the current frame,  q = quit
Saved frames go to training/dataset/images/train/ - then you label them.
Aim for 150-300 varied shots: different distances, angles, lighting, people.
"""
import argparse
import os
import cv2

OUT = os.path.join("training", "dataset", "images", "train")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0")
    args = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    cap = cv2.VideoCapture(int(args.source) if args.source.isdigit() else args.source)
    i = len([f for f in os.listdir(OUT) if f.lower().endswith(".jpg")])
    print("SPACE = save frame,  q = quit")
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        cv2.putText(frame, f"saved: {i}   SPACE=save  q=quit", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 200, 120), 2)
        cv2.imshow("CAPHY - collect dataset", frame)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord(" "):
            p = os.path.join(OUT, f"img_{i:04d}.jpg")
            cv2.imwrite(p, frame)
            print("saved", p)
            i += 1
    cap.release()
    cv2.destroyAllWindows()
    print(f"\nDone. {i} images in {OUT}")


if __name__ == "__main__":
    main()