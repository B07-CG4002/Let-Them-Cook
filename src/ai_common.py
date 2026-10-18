"""Shared eight-class contract. All model inputs are left 11 then right 11."""
import json
from pathlib import Path
import numpy as np

CLASS_NAMES = ['PICKUP', 'DROP', 'CHOP', 'WASH', 'THROW', 'FIREEXT', 'POURING', 'IDLE']
FEATURES = ['ax_g','ay_g','az_g','gx_deg_s','gy_deg_s','gz_deg_s','yaw_deg','pitch_deg','roll_deg','flex_1','flex_2']
COLUMNS = [f'{hand}_{feature}' for hand in ('left','right') for feature in FEATURES]
ANGLE_INDICES = [6,7,8,17,18,19]
CONFIG = dict(version='20261018_v2', class_names=CLASS_NAMES, feature_columns=COLUMNS,
              window_seconds=2.0, time_positions=50, normalization='none',
              angle_interpolation='unwrap degrees, interpolate, wrap to [-180,180)')

def resample(times, values, target=None):
    """Interpolate paired values using actual time; never bridge a gap >100ms."""
    times = np.asarray(times, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    if times.ndim != 1 or values.shape != (len(times),22) or len(times)<2:
        raise ValueError('Expected >=2 timestamps and values shaped (N,22)')
    if not np.isfinite(times).all() or not np.isfinite(values).all():
        raise ValueError('Non-finite input')
    if np.any(np.diff(times)<=0) or np.max(np.diff(times))>0.100:
        raise ValueError('Timestamps must increase; gaps must not exceed 100ms')
    if target is None:
        target = np.linspace(times[0], times[-1],50)
    target = np.asarray(target)
    if target[0]<times[0]-1e-9 or target[-1]>times[-1]+1e-9:
        raise ValueError('Target would extrapolate outside recorded data')
    result = np.empty((len(target),22),dtype=np.float32)
    for j in range(22):
        v = values[:,j]
        if j in ANGLE_INDICES:
            v = np.rad2deg(np.unwrap(np.deg2rad(v)))
        out = np.interp(target,times,v)
        if j in ANGLE_INDICES:
            out = (out+180)%360-180
        result[:,j]=out
    return result

def read_config(folder):
    config = json.loads((Path(folder)/'config.json').read_text(encoding='utf-8'))
    if config != CONFIG:
        raise ValueError('Dataset/model preprocessing contract differs from this version')
    return config
