"""Print fixed-point sizing evidence from conv1d_layer1_test.bin.

Run in the directory containing the exported test binary:
    python analyze_conv1d_ranges.py conv1d_layer1_test.bin
The binary layout is documented in export_conv1d_test.py.
"""
from pathlib import Path
import sys
import numpy as np

path = Path(sys.argv[1] if len(sys.argv) > 1 else "conv1d_layer1_test.bin")
raw = np.fromfile(path, dtype="<f4")
expected_count = 50 * 22 + 32 * 22 * 3 + 32 + 32 * 48
if raw.size != expected_count:
    raise SystemExit(f"Expected {expected_count} float32 values ({expected_count * 4} bytes), got {raw.size}")

i = 0
def take(n, shape):
    global i
    a = raw[i:i+n].reshape(shape)
    i += n
    return a

x = take(50 * 22, (50, 22))
w = take(32 * 22 * 3, (32, 22, 3))
b = take(32, (32,))
y = take(32 * 48, (32, 48))

def report(name, a):
    print(f"{name:12s} min={a.min(): .8g} max={a.max(): .8g} max_abs={np.abs(a).max():.8g}")

report("input", x)
report("weights", w)
report("bias", b)
report("golden", y)
product_bound = float(np.abs(x).max() * np.abs(w).max())
acc_bound = float(np.abs(b).max() + (22 * 3) * product_bound)
print(f"worst-case |input*weight| bound: {product_bound:.8g}")
print(f"conservative accumulator magnitude bound: {acc_bound:.8g}")
print("Choose fixed-point integer bits from the conservative bound; choose fractional bits by testing the quantized kernel against golden outputs and model accuracy.")
