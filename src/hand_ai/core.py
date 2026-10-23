"""Versioned single-hand data contract and validation."""
import hashlib
import json
from pathlib import Path
import numpy as np

LABELS = ['PICKUP', 'DROP', 'CHOP', 'WASH', 'THROW', 'POURING', 'IDLE']
CHANNELS = ['ax', 'ay', 'az', 'gx', 'gy', 'gz', 'yaw', 'pitch', 'roll', 'flex1', 'flex2']
SPLITS = ['train', 'validation', 'test']
CONFIG = dict(version='independent_hand_v1', class_names=LABELS,
              feature_columns=CHANNELS, window_seconds=2.0, time_positions=50,
              normalization='training-only per-feature mean/std after resampling',
              angle_interpolation='unwrap degrees, interpolate, wrap to [-180,180)')

def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)

def resample(times, values, target=None):
    t = np.asarray(times, dtype=np.float64)
    v = np.asarray(values, dtype=np.float64)
    if t.ndim != 1 or len(t) < 2 or v.shape != (len(t), 11):
        raise ValueError('Expected timestamps and an (N,11) feature array')
    if not np.isfinite(t).all() or not np.isfinite(v).all():
        raise ValueError('Nonfinite sensor data')
    dt = np.diff(t)
    if np.any(dt <= 0) or np.max(dt) > .100001:
        raise ValueError('Timestamps must increase; gaps cannot exceed 100 ms')
    target = np.linspace(t[0], t[-1], 50) if target is None else np.asarray(target)
    if target.shape != (50,) or not np.isfinite(target).all() or np.any(np.diff(target) <= 0):
        raise ValueError('Expected 50 increasing finite target positions')
    if target[0] < t[0] - 1e-8 or target[-1] > t[-1] + 1e-8:
        raise ValueError('Resampling would extrapolate')
    result = np.empty((50, 11), dtype=np.float32)
    for c in range(11):
        x = np.rad2deg(np.unwrap(np.deg2rad(v[:, c]))) if c in (6,7,8) else v[:, c]
        out = np.interp(target, t, x)
        result[:, c] = (out + 180) % 360 - 180 if c in (6,7,8) else out
    return result

def validate_ratios(rows):
    """Explicit sessions; per-class 75/10/15 with largest-remainder rounding.

    For 27 recordings this is 20/3/4; for 40 it is 30/4/6.
    Existing split assignments are validated, never shuffled or reassigned.
    """
    owners = {}
    counts = {label: {s: 0 for s in SPLITS} for label in LABELS}
    for row in rows:
        label, split = row['gesture'], row['split']
        if label not in LABELS or split not in SPLITS:
            raise ValueError('Invalid gesture/split')
        key = (row['person'], row['session'])
        if key in owners and owners[key] != split:
            raise ValueError(f'Session crosses dataset splits: {key}')
        owners[key] = split
        counts[label][split] += 1
    for label, values in counts.items():
        n = sum(values.values())
        raw = n * np.array([.75, .10, .15])
        expected = np.floor(raw).astype(int)
        for j in np.argsort(-(raw - expected), kind='stable')[:n-int(expected.sum())]:
            expected[j] += 1
        if n == 0 or np.any(expected == 0) or any(values[s] != int(v) for s,v in zip(SPLITS,expected)):
            wanted = dict(zip(SPLITS, map(int,expected)))
            raise ValueError(f'{label}: need rounded 75/10/15 per class; got {values}; '
                             f'expected {wanted}. Examples: 20/3/4 or 30/4/6.')
    return counts

def fit_normalizer(x):
    x = np.asarray(x)
    if x.ndim != 3 or x.shape[1:] != (50,11) or not len(x) or not np.isfinite(x).all():
        raise ValueError('Invalid training inputs')
    mean = x.mean(axis=(0,1), dtype=np.float64).astype(np.float32)
    std = x.std(axis=(0,1), dtype=np.float64).astype(np.float32)
    std = np.where(std < 1e-6, 1., std).astype(np.float32)
    return mean, std

def check_config(path):
    if json.loads(Path(path).read_text(encoding='utf-8')) != CONFIG:
        raise ValueError('Incompatible preprocessing contract')
