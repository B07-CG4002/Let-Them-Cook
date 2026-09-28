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

# Software emulation of quantized operands and an accumulator. Integer bit
# counts include the sign bit. This checks one actual held-out vector; it does
# not establish model-wide accuracy or replace the HLS C simulation.
def q_signed(a, width, integer_bits):
    frac = width - integer_bits
    scale = 2.0 ** frac
    lo = -(2.0 ** (integer_bits - 1))
    hi = (2.0 ** (integer_bits - 1)) - 1.0 / scale
    return np.clip(np.rint(a * scale) / scale, lo, hi)

print("\nSingle-vector quantization checks (input W27/I9, accumulator W40/I13):")
for weight_width in (18, 20, 22, 24):
    xq = q_signed(x, 27, 9)
    wq = q_signed(w, weight_width, 1)
    bq = q_signed(b, 40, 13)
    yq = np.empty_like(y)
    acc_frac = 40 - 13
    acc_scale = 2.0 ** acc_frac
    lo, hi = -(2.0 ** 12), (2.0 ** 12) - 1.0 / acc_scale
    for oc in range(32):
        for t in range(48):
            acc = bq[oc]
            for ic in range(22):
                for k in range(3):
                    acc = np.clip(np.rint((acc + xq[t + k, ic] * wq[oc, ic, k]) * acc_scale) / acc_scale, lo, hi)
            yq[oc, t] = acc
    err = np.abs(yq - y)
    limits = 1.0e-3 + 1.0e-4 * np.abs(y)
    print(f"weight W{weight_width}/I1: max_abs_error={err.max():.8g}, failures={(err > limits).sum()}/1536")
print("These checks cover only this held-out sample. Also measure validation-set accuracy after quantization.")
