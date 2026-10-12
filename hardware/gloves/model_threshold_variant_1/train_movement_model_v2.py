"""Train and test one shared IDLE/MOVING logistic model for both gloves.

Reads paired 5-second master CSVs and/or their one-second per-hand splits.
Only one-second windows are training examples. Fit, threshold-validation, and
test splits are grouped by original recording so windows from one trial cannot
leak across the splits.

Example:
    python train_movement_model_v2.py recordings/idle recordings/moving

Requires numpy and scikit-learn. On success, writes movement_model_params.h
and movement_model_metrics.json beside this script (or under --output-dir).
"""
import argparse
import csv
import json
import math
from pathlib import Path
import random
import re
import sys

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, confusion_matrix, f1_score, precision_score, recall_score
from sklearn.preprocessing import StandardScaler

LABELS = ("IDLE", "MOVING")
LABEL_TO_INT = {"IDLE": 0, "MOVING": 1}
MIN_ROWS_PER_WINDOW = 35
META_COLUMNS = {"timestamp_utc", "elapsed_s", "sample_index"}
SENSOR_SUFFIXES = (
    "ax_g", "ay_g", "az_g", "gx_deg_s", "gy_deg_s", "gz_deg_s",
    "yaw_deg", "pitch_deg", "roll_deg", "flex_1", "flex_2",
)
FEATURE_NAMES = (
    "accel_std_x", "accel_std_y", "accel_std_z",
    "gyro_rms_x", "gyro_rms_y", "gyro_rms_z",
    "flex_range_1", "flex_range_2",
)


def infer_label(path):
    parts = [part.upper() for part in path.parts]
    for label in LABELS:
        if label in parts:
            return label
    match = re.search(r"(?:^|_)(IDLE|MOVING)(?:_|$)", path.stem.upper())
    if match:
        return match.group(1)
    return None


def recording_group(path):
    """Map LEFT/RIGHT 1-second split filenames back to their source trial."""
    stem = re.sub(r"_(?:LEFT|RIGHT)_1S_\d+$", "", path.stem, flags=re.IGNORECASE)
    return stem


def load_csv(path):
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("missing CSV header")
        if not META_COLUMNS.issubset(reader.fieldnames):
            raise ValueError("requires timestamp_utc, elapsed_s, and sample_index columns")
        fields = set(reader.fieldnames)
        paired = all(f"left_{suffix}" in fields for suffix in SENSOR_SUFFIXES) and all(
            f"right_{suffix}" in fields for suffix in SENSOR_SUFFIXES)
        hand = None
        prefix = None
        if not paired:
            for candidate in ("LEFT", "RIGHT"):
                p = candidate.lower() + "_"
                if all(p + suffix in fields for suffix in SENSOR_SUFFIXES):
                    hand, prefix = candidate, p
                    break
            if hand is None:
                raise ValueError("expected all 11 left and right sensor columns, or one hand's 11 columns")

        records = []
        for line_number, row in enumerate(reader, start=2):
            try:
                elapsed = float(row["elapsed_s"])
                if not math.isfinite(elapsed) or elapsed < 0:
                    raise ValueError
                if paired:
                    values = {
                        side: [float(row[f"{side.lower()}_{suffix}"]) for suffix in SENSOR_SUFFIXES]
                        for side in ("LEFT", "RIGHT")
                    }
                    for side in values:
                        if not all(math.isfinite(x) for x in values[side]):
                            raise ValueError
                    records.append((elapsed, values))
                else:
                    values = [float(row[prefix + suffix]) for suffix in SENSOR_SUFFIXES]
                    if not all(math.isfinite(x) for x in values):
                        raise ValueError
                    records.append((elapsed, {hand: values}))
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"invalid numeric sensor/time value on CSV line {line_number}") from exc
    return paired, records


def features_for_window(values):
    """Feature order shared with the Arduino inference code."""
    data = np.asarray(values, dtype=np.float64)
    # Data columns: ax, ay, az, gx, gy, gz, yaw, pitch, roll, flex1, flex2.
    accel_std = np.std(data[:, 0:3], axis=0, ddof=0)
    gyro_rms = np.sqrt(np.mean(np.square(data[:, 3:6]), axis=0))
    flex_range = np.ptp(data[:, 9:11], axis=0)
    return np.concatenate((accel_std, gyro_rms, flex_range)).astype(np.float64)


def windows_from_records(records, paired, source, label, group):
    bins = {}
    if paired:
        for elapsed, values in records:
            second = int(elapsed)
            bins.setdefault(second, {"LEFT": [], "RIGHT": []})
            for hand in ("LEFT", "RIGHT"):
                bins[second][hand].append(values[hand])
    else:
        # A split file is already one second; collect all its rows together.
        hand = next(iter(records[0][1])) if records else None
        if hand is not None:
            bins[0] = {hand: [values[hand] for _, values in records]}

    out = []
    for second, hand_map in bins.items():
        for hand, values in hand_map.items():
            if len(values) < MIN_ROWS_PER_WINDOW:
                continue
            out.append({
                "x": features_for_window(values),
                "y": LABEL_TO_INT[label],
                "label": label,
                "group": group,
                "hand": hand,
                "source": str(source),
                "window": second,
                "rows": len(values),
            })
    return out


def discover_examples(inputs):
    paths = []
    for item in inputs:
        path = Path(item)
        if path.is_dir():
            paths.extend(sorted(p for p in path.rglob("*.csv") if p.is_file()))
        elif path.is_file() and path.suffix.lower() == ".csv":
            paths.append(path)
        else:
            raise ValueError(f"Not a CSV file or directory: {path}")

    paths = list(dict.fromkeys(p.resolve() for p in paths))
    if not paths:
        raise ValueError("No CSV files found")

    candidates = []
    paired_groups = set()
    skipped = []
    for path in paths:
        label = infer_label(path)
        if label is None:
            skipped.append((path, "could not infer IDLE/MOVING from parent folder or filename"))
            continue
        group = recording_group(path)
        try:
            paired, records = load_csv(path)
        except ValueError as exc:
            skipped.append((path, str(exc)))
            continue
        if paired:
            paired_groups.add((label, group))
        candidates.append((path, label, group, paired, records))

    examples = []
    for path, label, group, paired, records in candidates:
        # Avoid counting both a master file and its already-generated splits.
        if not paired and (label, group) in paired_groups:
            continue
        examples.extend(windows_from_records(records, paired, path, label, group))

    if skipped:
        print(f"Skipped {len(skipped)} CSV(s) that were not usable as labelled sensor data.")
    if not examples:
        raise ValueError("No usable one-second sensor windows found")
    return examples


def grouped_holdout(examples, test_fraction, seed):
    groups_by_label = {label: sorted({e["group"] for e in examples if e["label"] == label}) for label in LABELS}
    for label, groups in groups_by_label.items():
        if len(groups) < 2:
            raise ValueError(
                f"Need at least 2 independent {label} recordings for a recording-level test split; "
                f"found {len(groups)}. Collect more recordings before training."
            )
    rng = random.Random(seed)
    test_groups = set()
    for label, groups in groups_by_label.items():
        rng.shuffle(groups)
        n_test = max(1, int(math.ceil(len(groups) * test_fraction)))
        n_test = min(n_test, len(groups) - 1)
        test_groups.update(groups[:n_test])
    train = [e for e in examples if e["group"] not in test_groups]
    test = [e for e in examples if e["group"] in test_groups]
    return train, test, groups_by_label, test_groups


def classification_metrics(y_true, y_pred):
    matrix = confusion_matrix(y_true, y_pred, labels=[0, 1])
    true_idle, false_moving, false_idle, true_moving = matrix.ravel()
    return {
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "precision_moving": float(precision_score(y_true, y_pred, zero_division=0)),
        "recall_moving": float(recall_score(y_true, y_pred, zero_division=0)),
        "f1_moving": float(f1_score(y_true, y_pred, zero_division=0)),
        "false_moving_rate_on_idle": float(false_moving / (true_idle + false_moving))
        if true_idle + false_moving else 0.0,
        "false_idle_rate_on_moving": float(false_idle / (false_idle + true_moving))
        if false_idle + true_moving else 0.0,
        "confusion_matrix_rows_true_cols_pred_idle_moving": matrix.tolist(),
    }


def threshold_sweep(y_true, probabilities):
    """Evaluate 0.01-spaced probability cutoffs using validation data only."""
    results = []
    for step in range(101):
        threshold = step / 100.0
        predicted = (probabilities >= threshold).astype(np.int32)
        metrics = classification_metrics(y_true, predicted)
        results.append({
            "threshold": threshold,
            "accuracy": metrics["accuracy"],
            "precision_moving": metrics["precision_moving"],
            "recall_moving": metrics["recall_moving"],
            "f1_moving": metrics["f1_moving"],
            "false_moving_rate_on_idle": metrics["false_moving_rate_on_idle"],
            "false_idle_rate_on_moving": metrics["false_idle_rate_on_moving"],
        })
    return results


def choose_threshold(sweep, max_idle_false_moving_rate):
    """Favor MOVING recall while respecting the configured IDLE false-alarm cap."""
    acceptable = [
        row for row in sweep
        if row["false_moving_rate_on_idle"] <= max_idle_false_moving_rate
    ]
    if acceptable:
        selected = max(acceptable, key=lambda row: (
            row["recall_moving"], -row["threshold"]))
        cap_met = True
    else:
        # This is a defensive fallback; threshold 1.0 normally makes no IDLE
        # windows positive, so an attainable cap should always be present.
        selected = min(sweep, key=lambda row: (
            row["false_moving_rate_on_idle"], -row["recall_moving"], row["threshold"]))
        cap_met = False
    return selected, cap_met


def cpp_array(name, values):
    body = ", ".join(f"{float(x):.10g}f" for x in values)
    return f"static const float {name}[MOVEMENT_MODEL_FEATURE_COUNT] = {{{body}}};\n"


def write_header(path, scaler, model, train_group_counts, threshold):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = [
        "// Generated by train_movement_model_v2.py. Do not edit by hand.",
        "#pragma once",
        "#define MOVEMENT_MODEL_TRAINED 1",
        f"#define MOVEMENT_MODEL_FEATURE_COUNT {len(FEATURE_NAMES)}",
        f"#define MOVEMENT_MODEL_TRAIN_RECORDINGS_IDLE {train_group_counts['IDLE']}",
        f"#define MOVEMENT_MODEL_TRAIN_RECORDINGS_MOVING {train_group_counts['MOVING']}",
        cpp_array("MOVEMENT_FEATURE_MEAN", scaler.mean_),
        cpp_array("MOVEMENT_FEATURE_SCALE", scaler.scale_),
        cpp_array("MOVEMENT_LOGISTIC_COEFFICIENTS", model.coef_[0]),
        f"static const float MOVEMENT_LOGISTIC_INTERCEPT = {float(model.intercept_[0]):.10g}f;",
        f"static const float MOVEMENT_PROBABILITY_THRESHOLD = {threshold:.4f}f;",
        "",
    ]
    path.write_text("\n".join(content), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", default=["recordings/idle", "recordings/moving"],
                        help="labelled folders or CSV files (default: recordings/idle recordings/moving)")
    parser.add_argument("--output-dir", type=Path, default=Path("."),
                        help="where movement_model_params.h and metrics JSON are written")
    parser.add_argument("--test-fraction", type=float, default=0.2,
                        help="fraction of recordings per class held out for testing (default: 0.2)")
    parser.add_argument("--validation-fraction", type=float, default=0.25,
                        help="fraction of non-test recordings per class used to select the threshold (default: 0.25)")
    parser.add_argument("--max-idle-false-moving-rate", type=float, default=0.15,
                        help="maximum validation IDLE windows allowed to trigger MOVING (default: 0.15)")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if not 0.05 <= args.test_fraction <= 0.5:
        parser.error("--test-fraction must be between 0.05 and 0.5")
    if not 0.05 <= args.validation_fraction <= 0.5:
        parser.error("--validation-fraction must be between 0.05 and 0.5")
    if not 0.0 <= args.max_idle_false_moving_rate <= 1.0:
        parser.error("--max-idle-false-moving-rate must be between 0 and 1")

    try:
        examples = discover_examples(args.inputs)
        development, test, groups_by_label, test_groups = grouped_holdout(
            examples, args.test_fraction, args.seed)
        fit, validation, development_groups_by_label, validation_groups = grouped_holdout(
            development, args.validation_fraction, args.seed + 1)

        X_fit = np.vstack([e["x"] for e in fit])
        y_fit = np.asarray([e["y"] for e in fit], dtype=np.int32)
        X_validation = np.vstack([e["x"] for e in validation])
        y_validation = np.asarray([e["y"] for e in validation], dtype=np.int32)
        X_test = np.vstack([e["x"] for e in test])
        y_test = np.asarray([e["y"] for e in test], dtype=np.int32)

        scaler = StandardScaler()
        X_fit_scaled = scaler.fit_transform(X_fit)
        model = LogisticRegression(class_weight="balanced", max_iter=2000, random_state=args.seed)
        model.fit(X_fit_scaled, y_fit)

        validation_probabilities = model.predict_proba(scaler.transform(X_validation))[:, 1]
        sweep = threshold_sweep(y_validation, validation_probabilities)
        selected_row, threshold_cap_met = choose_threshold(
            sweep, args.max_idle_false_moving_rate)
        selected_threshold = float(selected_row["threshold"])

        test_probabilities = model.predict_proba(scaler.transform(X_test))[:, 1]
        test_pred = (test_probabilities >= selected_threshold).astype(np.int32)
        metrics = classification_metrics(y_test, test_pred)
        default_test_pred = (test_probabilities >= 0.5).astype(np.int32)
        default_test_metrics = classification_metrics(y_test, default_test_pred)

        test_by_hand = {}
        for hand in ("LEFT", "RIGHT"):
            indices = [i for i, e in enumerate(test) if e["hand"] == hand]
            if indices:
                test_by_hand[hand] = classification_metrics(y_test[indices], test_pred[indices])

        development_groups_by_class = {
            label: len(development_groups_by_label[label]) for label in LABELS
        }
        fit_groups_by_class = {
            label: len({e["group"] for e in fit if e["label"] == label}) for label in LABELS
        }
        validation_groups_by_class = {
            label: len([g for g in development_groups_by_label[label] if g in validation_groups])
            for label in LABELS
        }
        test_groups_by_class = {
            label: len([g for g in groups_by_label[label] if g in test_groups]) for label in LABELS
        }

        # Refit on all recordings for the ESP32 model after threshold selection
        # and untouched held-out testing are complete.
        X_all = np.vstack([e["x"] for e in examples])
        y_all = np.asarray([e["y"] for e in examples], dtype=np.int32)
        final_scaler = StandardScaler().fit(X_all)
        final_model = LogisticRegression(class_weight="balanced", max_iter=2000, random_state=args.seed)
        final_model.fit(final_scaler.transform(X_all), y_all)

        args.output_dir.mkdir(parents=True, exist_ok=True)
        header_path = args.output_dir / "movement_model_params.h"
        metrics_path = args.output_dir / "movement_model_metrics.json"
        write_header(header_path, final_scaler, final_model,
                     {label: len(groups_by_label[label]) for label in LABELS}, selected_threshold)
        report = {
            "model": "shared per-glove logistic regression",
            "features": list(FEATURE_NAMES),
            "training_recordings_by_class": {label: len(groups_by_label[label]) for label in LABELS},
            "training_windows": len(examples),
            "train_recordings_by_class_after_holdout": development_groups_by_class,
            "model_fit_recordings_by_class": fit_groups_by_class,
            "threshold_validation_recordings_by_class": validation_groups_by_class,
            "test_recordings_by_class": test_groups_by_class,
            "model_fit_windows": len(fit),
            "threshold_validation_windows": len(validation),
            "test_windows": len(test),
            "threshold_selection": {
                "criterion": "maximize MOVING recall subject to the IDLE false-MOVING rate limit",
                "max_idle_false_moving_rate": args.max_idle_false_moving_rate,
                "limit_met": threshold_cap_met,
                "selected_threshold": selected_threshold,
                "validation_metrics_at_selected_threshold": selected_row,
                "validation_sweep": sweep,
            },
            "held_out_metrics": metrics,
            "held_out_metrics_by_hand": test_by_hand,
            "held_out_metrics_at_default_threshold_0_5": default_test_metrics,
            "warning": ("Fewer than 50 source recordings in at least one class; treat these results as a pilot, "
                        "not evidence of reliable generalization.")
                       if min(len(groups_by_label[label]) for label in LABELS) < 50 else None,
        }
        metrics_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

        print("Shared IDLE/MOVING model; source recordings grouped for the test split.")
        print("Source recordings:", ", ".join(f"{label}={len(groups_by_label[label])}" for label in LABELS))
        print("One-second windows:", f"fit={len(fit)} validation={len(validation)} test={len(test)}")
        print(f"Selected MOVING threshold={selected_threshold:.2f} using validation data; "
              f"maximum IDLE false-MOVING rate={args.max_idle_false_moving_rate:.0%}.")
        print(f"Validation at selected threshold: MOVING recall="
              f"{selected_row['recall_moving']:.3f}, IDLE false-MOVING rate="
              f"{selected_row['false_moving_rate_on_idle']:.3f}.")
        print(f"Held-out: accuracy={metrics['accuracy']:.3f}, precision(MOVING)={metrics['precision_moving']:.3f}, "
              f"recall(MOVING)={metrics['recall_moving']:.3f}, F1(MOVING)={metrics['f1_moving']:.3f}")
        print(f"At default threshold 0.50 on the same held-out test: "
              f"recall(MOVING)={default_test_metrics['recall_moving']:.3f}, "
              f"IDLE false-MOVING rate={default_test_metrics['false_moving_rate_on_idle']:.3f}.")
        print("Confusion matrix (rows=true IDLE/MOVING, columns=predicted IDLE/MOVING):")
        print(np.asarray(metrics["confusion_matrix_rows_true_cols_pred_idle_moving"]))
        if report["warning"]:
            print("WARNING:", report["warning"])
        print("ESP32 parameter header:", header_path.resolve())
        print("Metrics report:", metrics_path.resolve())
        print("Copy movement_model_params.h beside the ESP32 .ino before compiling.")
    except (OSError, ValueError, csv.Error) as exc:
        print(f"Training stopped: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
