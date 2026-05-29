#!/usr/bin/env python3
"""Validate or preview a trained YOLO vehicle model on the project dataset.

This script supports two practical modes:

1. Preview-only validation
   Used when the dataset split has images but no label files yet.
   It runs inference, saves annotated previews, and writes summary reports.

2. Metric validation
   Used when YOLO label files exist for the chosen split.
   It runs `model.val(...)` to compute mAP/precision/recall in addition to
   the preview outputs.

3. Live source preview
   Used for a direct RTSP/video/image source when you want to quickly inspect
   detections from the trained model on a real stream.

Examples:
  python3 validate_model.py
  python3 validate_model.py --split test --limit 50
  python3 validate_model.py --compare-model yolov8n.pt
  python3 validate_model.py --source 'rtsp://user:pass@host/stream1' --stream-frames 120
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import yaml
from ultralytics import YOLO


ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL = ROOT / "Vehicle_Detection_v8n.pt"
DEFAULT_DATA_YAML = ROOT.parent / "datasets" / "vehicle_yolo_v1" / "data.yaml"
DEFAULT_OUTPUT_ROOT = ROOT / "validation_runs"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class DatasetSplit:
    data_yaml: Path
    dataset_root: Path
    split_name: str
    images_dir: Path
    labels_dir: Path
    image_paths: list[Path]
    label_count: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL, help="Path to trained YOLO .pt model")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_YAML, help="Path to dataset data.yaml")
    parser.add_argument("--split", default="val", choices=["train", "val", "test"], help="Dataset split to evaluate")
    parser.add_argument(
        "--source",
        default=None,
        help="Optional image/video/RTSP source. When provided, dataset split evaluation is skipped.",
    )
    parser.add_argument(
        "--source-name",
        default=None,
        help="Optional tag for naming RTSP/video preview outputs.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Directory for validation outputs")
    parser.add_argument("--imgsz", type=int, default=960, help="Inference image size")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    parser.add_argument("--device", default=None, help="Ultralytics device string, e.g. cpu, 0")
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of images to process")
    parser.add_argument(
        "--stream-frames",
        type=int,
        default=120,
        help="Max frames to process in --source mode. Use 0 for unbounded.",
    )
    parser.add_argument(
        "--compare-model",
        type=str,
        default=None,
        help="Optional second model path/name for side-by-side validation, e.g. yolov8n.pt",
    )
    parser.add_argument(
        "--no-preview",
        action="store_true",
        help="Skip saving annotated preview images",
    )
    return parser.parse_args()


def load_dataset_split(data_yaml: Path, split_name: str, limit: int = 0) -> DatasetSplit:
    if not data_yaml.exists():
        raise FileNotFoundError(f"Dataset YAML not found: {data_yaml}")

    with data_yaml.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}

    dataset_root = Path(cfg.get("path") or data_yaml.parent)
    if not dataset_root.is_absolute():
        dataset_root = (data_yaml.parent / dataset_root).resolve()

    split_rel = cfg.get(split_name)
    if not split_rel:
        raise ValueError(f"Split '{split_name}' not defined in {data_yaml}")

    images_dir = Path(split_rel)
    if not images_dir.is_absolute():
        images_dir = (dataset_root / images_dir).resolve()

    labels_dir = resolve_labels_dir(dataset_root, split_rel, split_name)
    image_paths = sorted(
        path for path in images_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTS
    )
    if limit > 0:
        image_paths = image_paths[:limit]

    label_count = 0
    if labels_dir.exists():
        label_count = sum(1 for _ in labels_dir.rglob("*.txt"))

    return DatasetSplit(
        data_yaml=data_yaml.resolve(),
        dataset_root=dataset_root,
        split_name=split_name,
        images_dir=images_dir,
        labels_dir=labels_dir,
        image_paths=image_paths,
        label_count=label_count,
    )


def resolve_labels_dir(dataset_root: Path, split_rel: str, split_name: str) -> Path:
    split_rel_path = Path(split_rel)
    parts = split_rel_path.parts
    if parts and parts[0] == "images":
        return (dataset_root / Path("labels", *parts[1:])).resolve()
    return (dataset_root / "labels" / split_name).resolve()


def normalize_names(names: Any) -> dict[int, str]:
    if isinstance(names, dict):
        return {int(k): str(v) for k, v in names.items()}
    if isinstance(names, (list, tuple)):
        return {idx: str(name) for idx, name in enumerate(names)}
    return {}


def sanitize_tag(raw: str) -> str:
    safe = []
    for ch in raw:
        if ch.isalnum():
            safe.append(ch)
        elif ch in {"-", "_", "."}:
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe).strip("._") or "model"


def scalarize(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): scalarize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scalarize(v) for v in value]
    return str(value)


def source_display_name(source: str, source_name: str | None = None) -> str:
    if source_name:
        return source_name
    return sanitize_tag(source.split("?")[0].rstrip("/").split("/")[-1] or "stream")


def _iter_source_results(
    model: YOLO,
    source: str,
    imgsz: int,
    conf: float,
    device: str | None,
    stream_frames: int,
):
    kwargs: dict[str, Any] = {
        "imgsz": imgsz,
        "conf": conf,
        "verbose": False,
        "save": False,
    }
    if device:
        kwargs["device"] = device

    source_path = Path(source)
    if source_path.exists() and source_path.suffix.lower() in IMAGE_EXTS:
        frame = cv2.imread(str(source_path))
        if frame is None:
            raise RuntimeError(f"Unable to read image source: {source}")
        for result in model.predict(source=frame, **kwargs):
            yield result
        return

    pipeline = (
        f"uridecodebin uri={source} source::latency=0 ! "
        "videoconvert ! video/x-raw, format=BGR ! appsink drop=true"
    )
    cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(source, cv2.CAP_GSTREAMER)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        cap.release()
        cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        cap.release()
        raise RuntimeError(f"Unable to open source: {source}")

    try:
        frame_idx = 0
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                break
            frame_idx += 1
            for result in model.predict(source=frame, **kwargs):
                yield result
            if stream_frames > 0 and frame_idx >= stream_frames:
                break
    finally:
        cap.release()


def run_metric_validation(
    model: YOLO,
    split: DatasetSplit,
    model_output_dir: Path,
    imgsz: int,
    conf: float,
    device: str | None,
) -> dict[str, Any] | None:
    if split.label_count <= 0:
        return None

    kwargs: dict[str, Any] = {
        "data": str(split.data_yaml),
        "split": split.split_name,
        "imgsz": imgsz,
        "conf": conf,
        "plots": True,
        "save_json": False,
        "project": str(model_output_dir),
        "name": "metrics",
        "exist_ok": True,
        "verbose": False,
    }
    if device:
        kwargs["device"] = device

    metrics = model.val(**kwargs)
    summary: dict[str, Any] = {
        "results_dict": scalarize(getattr(metrics, "results_dict", {})),
    }

    box = getattr(metrics, "box", None)
    if box is not None:
        for attr in ("map", "map50", "map75", "mp", "mr"):
            value = getattr(box, attr, None)
            if value is not None:
                summary[attr] = scalarize(value)

    return summary


def run_preview_validation(
    model: YOLO,
    split: DatasetSplit,
    model_output_dir: Path,
    imgsz: int,
    conf: float,
    device: str | None,
    save_preview: bool,
) -> dict[str, Any]:
    preview_dir = model_output_dir / "previews"
    if save_preview:
        preview_dir.mkdir(parents=True, exist_ok=True)

    detections_by_class: Counter[str] = Counter()
    conf_sum_by_class: defaultdict[str, float] = defaultdict(float)
    images_with_detections = 0
    per_image_rows: list[dict[str, Any]] = []

    kwargs: dict[str, Any] = {
        "source": [str(path) for path in split.image_paths],
        "stream": True,
        "imgsz": imgsz,
        "conf": conf,
        "verbose": False,
        "save": False,
    }
    if device:
        kwargs["device"] = device

    for result in model.predict(**kwargs):
        names = normalize_names(getattr(result, "names", {}))
        boxes = getattr(result, "boxes", None)
        image_name = Path(result.path).name
        image_counts: Counter[str] = Counter()
        image_conf_sum = 0.0
        box_count = 0

        if boxes is not None:
            for box in boxes:
                cls_id = int(box.cls.item())
                cls_name = names.get(cls_id, str(cls_id))
                score = float(box.conf.item())
                detections_by_class[cls_name] += 1
                conf_sum_by_class[cls_name] += score
                image_counts[cls_name] += 1
                image_conf_sum += score
                box_count += 1

        if box_count > 0:
            images_with_detections += 1

        if save_preview:
            result.save(filename=str(preview_dir / image_name))

        per_image_rows.append(
            {
                "image": image_name,
                "detections": box_count,
                "classes": ", ".join(f"{name}:{count}" for name, count in sorted(image_counts.items())),
                "avg_conf": round(image_conf_sum / box_count, 4) if box_count else 0.0,
            }
        )

    avg_conf_by_class = {
        cls_name: round(conf_sum_by_class[cls_name] / count, 4)
        for cls_name, count in sorted(detections_by_class.items())
        if count > 0
    }

    csv_path = model_output_dir / "per_image_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["image", "detections", "classes", "avg_conf"])
        writer.writeheader()
        writer.writerows(per_image_rows)

    return {
        "images_processed": len(split.image_paths),
        "images_with_detections": images_with_detections,
        "images_without_detections": max(0, len(split.image_paths) - images_with_detections),
        "detections_by_class": dict(sorted(detections_by_class.items())),
        "avg_conf_by_class": avg_conf_by_class,
        "per_image_csv": str(csv_path),
        "preview_dir": str(preview_dir) if save_preview else None,
    }


def run_source_preview(
    model: YOLO,
    source: str,
    source_name: str,
    model_output_dir: Path,
    imgsz: int,
    conf: float,
    device: str | None,
    save_preview: bool,
    stream_frames: int,
) -> dict[str, Any]:
    preview_dir = model_output_dir / "previews"
    if save_preview:
        preview_dir.mkdir(parents=True, exist_ok=True)

    detections_by_class: Counter[str] = Counter()
    conf_sum_by_class: defaultdict[str, float] = defaultdict(float)
    frames_with_detections = 0
    per_frame_rows: list[dict[str, Any]] = []

    for frame_idx, result in enumerate(
        _iter_source_results(
            model=model,
            source=source,
            imgsz=imgsz,
            conf=conf,
            device=device,
            stream_frames=stream_frames,
        ),
        start=1,
    ):
        names = normalize_names(getattr(result, "names", {}))
        boxes = getattr(result, "boxes", None)
        frame_counts: Counter[str] = Counter()
        frame_conf_sum = 0.0
        box_count = 0

        if boxes is not None:
            for box in boxes:
                cls_id = int(box.cls.item())
                cls_name = names.get(cls_id, str(cls_id))
                score = float(box.conf.item())
                detections_by_class[cls_name] += 1
                conf_sum_by_class[cls_name] += score
                frame_counts[cls_name] += 1
                frame_conf_sum += score
                box_count += 1

        if box_count > 0:
            frames_with_detections += 1

        if save_preview:
            frame_name = f"{sanitize_tag(source_name)}_{frame_idx:06d}.jpg"
            result.save(filename=str(preview_dir / frame_name))

        per_frame_rows.append(
            {
                "frame": frame_idx,
                "detections": box_count,
                "classes": ", ".join(f"{name}:{count}" for name, count in sorted(frame_counts.items())),
                "avg_conf": round(frame_conf_sum / box_count, 4) if box_count else 0.0,
            }
        )

    avg_conf_by_class = {
        cls_name: round(conf_sum_by_class[cls_name] / count, 4)
        for cls_name, count in sorted(detections_by_class.items())
        if count > 0
    }

    csv_path = model_output_dir / "per_frame_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["frame", "detections", "classes", "avg_conf"])
        writer.writeheader()
        writer.writerows(per_frame_rows)

    frames_processed = len(per_frame_rows)
    return {
        "source": source,
        "source_name": source_name,
        "frames_processed": frames_processed,
        "frames_with_detections": frames_with_detections,
        "frames_without_detections": max(0, frames_processed - frames_with_detections),
        "detections_by_class": dict(sorted(detections_by_class.items())),
        "avg_conf_by_class": avg_conf_by_class,
        "per_frame_csv": str(csv_path),
        "preview_dir": str(preview_dir) if save_preview else None,
    }


def validate_one_model(
    model_spec: str | Path,
    split: DatasetSplit | None,
    run_dir: Path,
    imgsz: int,
    conf: float,
    device: str | None,
    save_preview: bool,
    source: str | None = None,
    source_name: str | None = None,
    stream_frames: int = 0,
) -> dict[str, Any]:
    model_path = str(model_spec)
    model = YOLO(model_path)
    model_names = normalize_names(getattr(model, "names", {}))
    model_tag = sanitize_tag(Path(model_path).stem if Path(model_path).suffix else str(model_spec))
    model_output_dir = run_dir / model_tag
    model_output_dir.mkdir(parents=True, exist_ok=True)

    if source is not None:
        display_name = source_display_name(source, source_name)
        preview_summary = run_source_preview(
            model=model,
            source=source,
            source_name=display_name,
            model_output_dir=model_output_dir,
            imgsz=imgsz,
            conf=conf,
            device=device,
            save_preview=save_preview,
            stream_frames=stream_frames,
        )
        metrics_summary = None
        mode = "source_preview"
    else:
        if split is None:
            raise ValueError("Dataset split is required when source mode is not used")
        preview_summary = run_preview_validation(
            model=model,
            split=split,
            model_output_dir=model_output_dir,
            imgsz=imgsz,
            conf=conf,
            device=device,
            save_preview=save_preview,
        )
        metrics_summary = run_metric_validation(
            model=model,
            split=split,
            model_output_dir=model_output_dir,
            imgsz=imgsz,
            conf=conf,
            device=device,
        )
        mode = "metrics+preview" if metrics_summary else "preview_only"

    summary = {
        "model_spec": model_path,
        "class_names": model_names,
        "split": split.split_name if split else None,
        "data_yaml": str(split.data_yaml) if split else None,
        "images_dir": str(split.images_dir) if split else None,
        "labels_dir": str(split.labels_dir) if split else None,
        "label_files_found": split.label_count if split else 0,
        "source": source,
        "source_name": source_display_name(source, source_name) if source else None,
        "mode": mode,
        "preview": preview_summary,
        "metrics": metrics_summary,
    }

    summary_path = model_output_dir / "summary.json"
    with summary_path.open("w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    summary["summary_json"] = str(summary_path)
    return summary


def main() -> None:
    args = parse_args()

    split: DatasetSplit | None = None
    if args.source:
        source_name = source_display_name(args.source, args.source_name)
    else:
        split = load_dataset_split(args.data.resolve(), args.split, limit=args.limit)
        if not split.image_paths:
            raise SystemExit(f"No images found in {split.images_dir}")
        source_name = None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_suffix = source_name or args.split
    run_dir = (args.output / f"{timestamp}_{sanitize_tag(run_suffix)}").resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    top_level_summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": {
            "data_yaml": str(split.data_yaml),
            "dataset_root": str(split.dataset_root),
            "split": split.split_name,
            "images_dir": str(split.images_dir),
            "labels_dir": str(split.labels_dir),
            "images_selected": len(split.image_paths),
            "label_files_found": split.label_count,
        } if split else None,
        "source": {
            "path": args.source,
            "source_name": source_name,
            "stream_frames": args.stream_frames,
        } if args.source else None,
        "models": [],
    }

    model_specs: list[str | Path] = [args.model.resolve()]
    if args.compare_model:
        model_specs.append(args.compare_model)

    for model_spec in model_specs:
        summary = validate_one_model(
            model_spec=model_spec,
            split=split,
            run_dir=run_dir,
            imgsz=args.imgsz,
            conf=args.conf,
            device=args.device,
            save_preview=not args.no_preview,
            source=args.source,
            source_name=source_name,
            stream_frames=args.stream_frames,
        )
        top_level_summary["models"].append(summary)

    top_summary_path = run_dir / "run_summary.json"
    with top_summary_path.open("w", encoding="utf-8") as fh:
        json.dump(top_level_summary, fh, indent=2)

    print("\nValidation complete")
    print(f"Run directory : {run_dir}")
    if split:
        print(f"Images used   : {len(split.image_paths)}")
        print(f"Labels found  : {split.label_count}")
    else:
        print(f"Source        : {args.source}")
        print(f"Frames target : {args.stream_frames if args.stream_frames > 0 else 'unbounded'}")
    for model_summary in top_level_summary["models"]:
        print(f"\nModel        : {model_summary['model_spec']}")
        print(f"Mode         : {model_summary['mode']}")
        print(f"Summary JSON : {model_summary['summary_json']}")
        preview = model_summary["preview"]
        print(f"Detections   : {preview['detections_by_class']}")
        if model_summary["metrics"]:
            metrics = model_summary["metrics"]
            printable = {k: metrics[k] for k in ("map50", "map75", "map", "mp", "mr") if k in metrics}
            print(f"Metrics      : {printable}")


if __name__ == "__main__":
    main()
