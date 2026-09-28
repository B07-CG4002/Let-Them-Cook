"""Train a separate P01+P02 model and compare it on held-out whole trials.

Expected split by trial number:
  P01: trials 01-07 train, 08 validation, 09-10 test
  P02: trials 01-11 train, 12 validation, 13-15 test

Example:
  python -m src.train_model --combined-dir data/combined_processed --person2-dir data/person2_processed
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix
from torch.utils.data import DataLoader, TensorDataset

from src.model import GestureCNN


def load_data(folder: Path):
    X = np.load(folder / "X.npy")
    y = np.load(folder / "y.npy")
    metadata = pd.read_csv(folder / "metadata.csv")
    if X.ndim != 3 or X.shape[1:] != (50, 22):
        raise ValueError(f"Expected X shape (trials, 50, 22), got {X.shape} in {folder}")
    if len(X) != len(y) or len(X) != len(metadata):
        raise ValueError(f"X, y, and metadata row counts differ in {folder}")
    return X, y, metadata


def masks(metadata: pd.DataFrame):
    people = metadata["person"].astype(str).str.upper().to_numpy()
    trials = pd.to_numeric(metadata["trial"], errors="raise").to_numpy()
    train = np.zeros(len(metadata), dtype=bool)
    validation = np.zeros(len(metadata), dtype=bool)
    test = np.zeros(len(metadata), dtype=bool)
    for person in sorted(set(people)):
        person_mask = people == person
        if person == "P01":
            train[person_mask] = trials[person_mask] <= 7
            validation[person_mask] = trials[person_mask] == 8
            test[person_mask] = trials[person_mask] >= 9
        elif person == "P02":
            train[person_mask] = trials[person_mask] <= 11
            validation[person_mask] = trials[person_mask] == 12
            test[person_mask] = trials[person_mask] >= 13
        else:
            raise ValueError(f"Unexpected person ID {person}; define an explicit trial split first")
    return train, validation, test


def validate_class_coverage(y, class_names, metadata, mask, split_name):
    labels = set(np.asarray(y)[mask].tolist())
    expected = set(range(len(class_names)))
    if labels != expected:
        found = [class_names[index] for index in sorted(labels)]
        raise ValueError(f"{split_name} split must contain all classes; found {found}")
    counts = metadata.loc[mask].groupby("gesture").size()
    missing = [name for name in class_names if counts.get(name, 0) == 0]
    if missing:
        raise ValueError(f"{split_name} split missing classes: {missing}")


def make_model(class_names, weights_path=None):
    model = GestureCNN(num_features=22, num_classes=len(class_names))
    with torch.no_grad():
        model(torch.zeros(1, 22, 50))  # initialize LazyLinear, if present
    if weights_path is not None:
        try:
            state = torch.load(weights_path, map_location="cpu", weights_only=True)
        except TypeError:
            state = torch.load(weights_path, map_location="cpu")
        model.load_state_dict(state)
    return model


def evaluate(model, X, y, metadata, mask, class_names):
    model.eval()
    inputs = torch.tensor(X[mask], dtype=torch.float32).permute(0, 2, 1)
    labels = np.asarray(y[mask], dtype=np.int64)
    with torch.no_grad():
        predicted = model(inputs).argmax(dim=1).cpu().numpy()
    report = classification_report(
        labels, predicted, labels=list(range(len(class_names))),
        target_names=class_names, output_dict=True, zero_division=0,
    )
    return {
        "n_trials": int(mask.sum()),
        "accuracy": float(accuracy_score(labels, predicted)),
        "classification_report": report,
        "confusion_matrix": confusion_matrix(
            labels, predicted, labels=list(range(len(class_names)))
        ).tolist(),
        "trial_rows": metadata.loc[mask, ["person", "gesture", "trial"]].to_dict("records"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--combined-dir", default="data/combined_processed")
    parser.add_argument("--person2-dir", default="data/person2_processed")
    parser.add_argument("--baseline-model", default="models/gesture_cnn.pth")
    parser.add_argument("--class-names", default="models/class_names.json")
    parser.add_argument("--model-out", default="models/gesture_cnn_person1_person2.pth")
    parser.add_argument("--report-out", default="reports/person2_retraining_eval.json")
    parser.add_argument("--epochs", type=int, default=30)
    args = parser.parse_args()

    combined_dir, person2_dir = Path(args.combined_dir), Path(args.person2_dir)
    model_out, report_out = Path(args.model_out), Path(args.report_out)
    for path in (model_out, report_out):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite existing output: {path}")
    for path in (Path(args.baseline_model), Path(args.class_names)):
        if not path.is_file():
            raise SystemExit(f"Required baseline file is missing: {path}")

    with open(args.class_names, encoding="utf-8") as handle:
        class_names = json.load(handle)
    if class_names != ["PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT"]:
        raise ValueError(f"Unexpected class order: {class_names}")

    X, y, metadata = load_data(combined_dir)
    p2_X, p2_y, p2_metadata = load_data(person2_dir)
    train_mask, val_mask, test_mask = masks(metadata)
    p2_test_mask = (p2_metadata.person.astype(str).str.upper().to_numpy() == "P02") & (
        pd.to_numeric(p2_metadata.trial, errors="raise").to_numpy() >= 13
    )
    for name, mask in (("training", train_mask), ("validation", val_mask), ("test", test_mask)):
        if not mask.any():
            raise ValueError(f"{name} split is empty")
        validate_class_coverage(y, class_names, metadata, mask, name)
    validate_class_coverage(p2_y, class_names, p2_metadata, p2_test_mask, "Person 2 baseline test")

    torch.manual_seed(42)
    np.random.seed(42)
    train_x = torch.tensor(X[train_mask], dtype=torch.float32).permute(0, 2, 1)
    train_y = torch.tensor(y[train_mask], dtype=torch.long)
    train_loader = DataLoader(TensorDataset(train_x, train_y), batch_size=32, shuffle=True)
    val_x = torch.tensor(X[val_mask], dtype=torch.float32).permute(0, 2, 1)
    val_y = torch.tensor(y[val_mask], dtype=torch.long)

    model = make_model(class_names)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.001)
    loss_fn = nn.CrossEntropyLoss()
    best_val_accuracy = -1.0
    best_state = None
    for epoch in range(args.epochs):
        model.train()
        total_loss = 0.0
        for inputs, labels in train_loader:
            optimizer.zero_grad()
            loss = loss_fn(model(inputs), labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        model.eval()
        with torch.no_grad():
            val_pred = model(val_x).argmax(dim=1)
            val_accuracy = (val_pred == val_y).float().mean().item()
        average_loss = total_loss / max(1, len(train_loader))
        print(f"Epoch {epoch + 1}/{args.epochs} - loss={average_loss:.4f} - val_accuracy={val_accuracy:.2%}")
        if val_accuracy > best_val_accuracy:
            best_val_accuracy = val_accuracy
            best_state = {key: value.detach().clone() for key, value in model.state_dict().items()}

    if best_state is None:
        raise RuntimeError("Training produced no checkpoint")
    model.load_state_dict(best_state)
    model_out.parent.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), model_out)

    baseline = make_model(class_names, Path(args.baseline_model))
    p2_original_result = evaluate(baseline, p2_X, p2_y, p2_metadata, p2_test_mask, class_names)
    p2_retrained_mask = (metadata.person.astype(str).str.upper().to_numpy() == "P02") & test_mask
    p1_retrained_mask = (metadata.person.astype(str).str.upper().to_numpy() == "P01") & test_mask
    results = {
        "split": "whole trial; P01 train 01-07, val 08, test >=09; P02 train 01-11, val 12, test >=13",
        "model_out": str(model_out),
        "best_validation_accuracy": best_val_accuracy,
        "original_model_on_P02_test": p2_original_result,
        "retrained_model_on_P02_test": evaluate(model, X, y, metadata, p2_retrained_mask, class_names),
        "retrained_model_on_P01_test": evaluate(model, X, y, metadata, p1_retrained_mask, class_names),
    }
    report_out.parent.mkdir(parents=True, exist_ok=True)
    report_out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    new_p2_predictions = baseline_predictions(model, X[p2_retrained_mask])
    new_p1_predictions = baseline_predictions(model, X[p1_retrained_mask])
    original_p2_predictions = baseline_predictions(baseline, p2_X[p2_test_mask])
    print("\nHeld-out accuracy comparison:")
    print(f"Original P01 on P02: {p2_original_result['accuracy']:.2%}")
    print(f"Retrained model on P02: {results['retrained_model_on_P02_test']['accuracy']:.2%}")
    print(f"Retrained model on P01: {results['retrained_model_on_P01_test']['accuracy']:.2%}")
    print("\nOriginal P01 classification report on held-out P02 trials:")
    print(classification_report(
        p2_y[p2_test_mask], original_p2_predictions,
        labels=list(range(len(class_names))), target_names=class_names, zero_division=0,
    ))
    print("Retrained model classification report on held-out P02 trials:")
    print(classification_report(
        y[p2_retrained_mask], new_p2_predictions,
        labels=list(range(len(class_names))), target_names=class_names, zero_division=0,
    ))
    print(f"Saved new model: {model_out}")
    print(f"Saved evaluation report: {report_out}")


def baseline_predictions(model, X):
    model.eval()
    inputs = torch.tensor(X, dtype=torch.float32).permute(0, 2, 1)
    with torch.no_grad():
        return model(inputs).argmax(dim=1).cpu().numpy()


if __name__ == "__main__":
    main()
