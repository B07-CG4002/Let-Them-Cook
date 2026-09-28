"""Export the trained CNN's parameter/range report and golden test vectors.

Run from the repository root in the project's PyTorch environment:
  python export_fpga_reference.py

Calibration ranges are collected only from training/validation trials.
Held-out trials are exported as software golden vectors for later HLS checks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn

from src.model import GestureCNN

LAYER_NAMES = [
    "features.0", "features.1", "features.2", "features.3", "features.4",
    "features.5", "classifier.0", "classifier.1", "classifier.2", "classifier.3",
]


def trial_masks(metadata: pd.DataFrame):
    people = metadata["person"].astype(str).str.upper().to_numpy()
    trials = pd.to_numeric(metadata["trial"], errors="raise").to_numpy()
    calibration = np.zeros(len(metadata), dtype=bool)
    test = np.zeros(len(metadata), dtype=bool)
    for person in sorted(set(people)):
        mask = people == person
        if person == "P01":
            calibration[mask] = trials[mask] <= 8
            test[mask] = trials[mask] >= 9
        elif person == "P02":
            calibration[mask] = trials[mask] <= 12
            test[mask] = trials[mask] >= 13
        else:
            raise ValueError(f"Unexpected person ID {person}; update the split rules")
    if not calibration.any() or not test.any():
        raise ValueError("Calibration or held-out test split is empty")
    if np.any(calibration & test):
        raise AssertionError("Calibration and test trials overlap")
    return calibration, test


def load_checkpoint(model, path: Path):
    try:
        state = torch.load(path, map_location="cpu", weights_only=True)
    except TypeError:
        state = torch.load(path, map_location="cpu")
    model.load_state_dict(state)


def tensor_stats(values):
    array = np.asarray(values, dtype=np.float64)
    return {
        "min": float(np.min(array)),
        "max": float(np.max(array)),
        "max_abs": float(np.max(np.abs(array))),
        "shape": list(array.shape),
    }


def run_batched(model, data, batch_size, mode, ranges, saved_outputs):
    model.eval()
    outputs = []
    hooks = []

    def make_hook(name):
        def hook(_module, _inputs, output):
            value = output.detach().cpu().numpy()
            if mode == "range":
                info = ranges.setdefault(name, {"min": float("inf"), "max": float("-inf"), "max_abs": 0.0})
                info["min"] = min(info["min"], float(value.min()))
                info["max"] = max(info["max"], float(value.max()))
                info["max_abs"] = max(info["max_abs"], float(np.abs(value).max()))
            elif mode == "save":
                saved_outputs.setdefault(name, []).append(value.astype(np.float32, copy=False))
        return hook

    modules = dict(model.named_modules())
    for name in LAYER_NAMES:
        if name not in modules:
            raise KeyError(f"Expected layer '{name}' in GestureCNN; found architecture differs")
        hooks.append(modules[name].register_forward_hook(make_hook(name)))

    try:
        with torch.no_grad():
            for start in range(0, len(data), batch_size):
                batch = torch.from_numpy(data[start:start + batch_size]).float().permute(0, 2, 1)
                logits = model(batch)
                outputs.append(logits.cpu().numpy().astype(np.float32))
    finally:
        for hook in hooks:
            hook.remove()
    return np.concatenate(outputs, axis=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="models/gesture_cnn_person1_person2.pth")
    parser.add_argument("--class-names", default="models/class_names.json")
    parser.add_argument("--data-dir", default="data/combined_processed")
    parser.add_argument("--output-dir", default="fpga/validation/person1_person2")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--overwrite", action="store_true",
                        help="Allow replacing report/vector files in the selected output folder")
    args = parser.parse_args()

    model_path = Path(args.model)
    data_dir = Path(args.data_dir)
    output_dir = Path(args.output_dir)
    if not model_path.is_file():
        raise SystemExit(f"Model checkpoint not found: {model_path}")
    with open(args.class_names, encoding="utf-8") as handle:
        class_names = json.load(handle)
    if class_names != ["PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT"]:
        raise ValueError(f"Unexpected class order: {class_names}")

    X = np.load(data_dir / "X.npy")
    y = np.load(data_dir / "y.npy").astype(np.int64)
    metadata = pd.read_csv(data_dir / "metadata.csv")
    if X.ndim != 3 or X.shape[1:] != (50, 22) or len(X) != len(y) or len(X) != len(metadata):
        raise ValueError(f"Expected aligned X=(N,50,22), y=(N,), metadata=N; got {X.shape}, {y.shape}, {len(metadata)}")
    calibration_mask, test_mask = trial_masks(metadata)
    if set(y[test_mask]) != set(range(len(class_names))):
        raise ValueError("Held-out test set does not contain all six classes")
    if not np.isfinite(X).all():
        raise ValueError("Input data contains non-finite values")

    output_files = [output_dir / "fpga_calibration_report.json",
                    output_dir / "fpga_golden_test_vectors.npz",
                    output_dir / "fpga_golden_test_metadata.csv"]
    if not args.overwrite and any(path.exists() for path in output_files):
        raise SystemExit("Output already exists; choose another --output-dir or pass --overwrite")

    torch.manual_seed(42)
    model = GestureCNN(num_features=22, num_classes=len(class_names))
    with torch.no_grad():
        model(torch.zeros(1, 22, 50))
    load_checkpoint(model, model_path)
    model.eval()

    calibration_input = X[calibration_mask]
    test_input = X[test_mask]
    layer_ranges = {}
    run_batched(model, calibration_input, args.batch_size, "range", layer_ranges, {})
    golden_logits = run_batched(model, test_input, args.batch_size, "none", {}, {})
    intermediate = {}
    run_batched(model, test_input, args.batch_size, "save", {}, intermediate)
    intermediate = {f"layer__{name.replace('.', '_')}": np.concatenate(chunks, axis=0)
                    for name, chunks in intermediate.items()}
    predictions = np.argmax(golden_logits, axis=1).astype(np.int64)

    module_dict = dict(model.named_modules())
    parameter_report = {}
    parameter_count = 0
    for name in ("features.0", "features.3", "classifier.1", "classifier.3"):
        module = module_dict[name]
        weight = module.weight.detach().cpu().numpy()
        bias = module.bias.detach().cpu().numpy()
        parameter_report[name] = {
            "weight": tensor_stats(weight),
            "bias": tensor_stats(bias),
            "parameters": int(weight.size + bias.size),
        }
        parameter_count += weight.size + bias.size

    feature_min = calibration_input.min(axis=(0, 1)).astype(float).tolist()
    feature_max = calibration_input.max(axis=(0, 1)).astype(float).tolist()
    report = {
        "checkpoint": str(model_path),
        "input_shape_per_trial": [50, 22],
        "model_input_layout": "batch, features, time",
        "calibration_trials": int(calibration_mask.sum()),
        "held_out_test_trials": int(test_mask.sum()),
        "split": "P01 trials <=08 calibration, >=09 test; P02 trials <=12 calibration, >=13 test",
        "classes_in_order": class_names,
        "parameter_count": int(parameter_count),
        "layer_parameters": parameter_report,
        "input_feature_min_on_calibration": feature_min,
        "input_feature_max_on_calibration": feature_max,
        "activation_ranges_on_calibration": layer_ranges,
        "hardware_design_notes": {
            "operations": "Conv1d layers use valid padding, stride 1; MaxPool1d default kernel/stride 2; Linear layers are 704->64->6",
            "mac_count_per_trial": 281984,
            "quantization": "Not applied by this script; select formats after reviewing observed ranges and checking accuracy",
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    output_files[0].write_text(json.dumps(report, indent=2), encoding="utf-8")
    golden = {
        "inputs": test_input.astype(np.float32),
        "labels": y[test_mask],
        "logits": golden_logits,
        "predictions": predictions,
        "class_names": np.asarray(class_names),
        **intermediate,
    }
    np.savez_compressed(output_files[1], **golden)
    metadata.loc[test_mask].reset_index(drop=True).to_csv(output_files[2], index=False)

    print(f"Checkpoint: {model_path}")
    print(f"Calibration trials: {calibration_mask.sum()}; held-out trials: {test_mask.sum()}")
    print(f"Parameter count: {parameter_count}")
    print("Layer parameter ranges:")
    for name, values in parameter_report.items():
        print(f"  {name}: weights {values['weight']['shape']} [{values['weight']['min']:.6g}, {values['weight']['max']:.6g}], "
              f"bias [{values['bias']['min']:.6g}, {values['bias']['max']:.6g}]")
    print("Held-out software predictions:")
    for row, prediction in zip(metadata.loc[test_mask].itertuples(index=False), predictions):
        print(f"  {row.person} {row.gesture} trial {row.trial}: predicted {class_names[prediction]}")
    print(f"Saved calibration report and golden vectors under {output_dir}")


if __name__ == "__main__":
    main()
