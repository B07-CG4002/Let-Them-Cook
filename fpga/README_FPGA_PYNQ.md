# FPGA HLS Accelerator: PYNQ Deployment

This folder contains the Vivado/Vitis HLS proof-of-concept for the CG4002 FPGA accelerator.

## Hardware

- Board: Ultra96-V2
- Target part: `xazu3eg-sbva484-1-i`
- Accelerator IP: `multiply_accumulate_0`
- Input arrays: 16-bit signed values, 16 elements each
- Output: 32-bit signed accumulated result

## Required deployment files

PYNQ requires the bitstream and matching hardware metadata file:

```text
multiply_accumulate_wrapper.bit
multiply_accumulate_wrapper.hwh
```

The two files must have the same filename prefix.

## Copy the files to the board

From Windows PowerShell, copy the files to the board's temporary directory. The SSH jump host is required for remote access.

```powershell
scp -o ProxyJump=ooiwenre@stujump.comp.nus.edu.sg `
"C:\path\to\multiply_accumulate_wrapper.bit" `
xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg:/tmp/

scp -o ProxyJump=ooiwenre@stujump.comp.nus.edu.sg `
"C:\path\to\multiply_accumulate_wrapper.hwh" `
xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg:/tmp/
```

Verify the files on the board:

```bash
ls -lh /tmp/multiply_accumulate_wrapper.*
```

## Load the overlay

The UltraScale+ programmable-logic clock requires root permissions, so start the PYNQ Python interpreter with `sudo`:

```bash
sudo -E /usr/local/share/pynq-venv/bin/python
```

Then load the overlay:

```python
from pynq import Overlay

overlay = Overlay("/tmp/multiply_accumulate_wrapper.bit")
print(list(overlay.ip_dict.keys()))
```

The output should include:

```text
multiply_accumulate_0
```

## Functional hardware test

The following test allocates two 16-element arrays in board DDR memory. The first array contains all ones and the second contains values from 0 to 15. The expected result is 120.

```python
import numpy as np
from pynq import allocate

ip = overlay.multiply_accumulate_0

a_buf = allocate(shape=(16,), dtype=np.int16)
b_buf = allocate(shape=(16,), dtype=np.int16)

a_buf[:] = 1
b_buf[:] = np.arange(16, dtype=np.int16)

a_buf.flush()
b_buf.flush()

a_addr = int(a_buf.device_address)
b_addr = int(b_buf.device_address)

# Write the 64-bit AXI master addresses as low and high 32-bit words.
ip.write(0x10, a_addr & 0xffffffff)
ip.write(0x14, (a_addr >> 32) & 0xffffffff)
ip.write(0x1C, b_addr & 0xffffffff)
ip.write(0x20, (b_addr >> 32) & 0xffffffff)

# Start the HLS IP.
ip.write(0x00, 1)

# Wait for ap_done (CTRL bit 1).
while (ip.read(0x00) & 0x2) == 0:
    pass

result = np.int32(ip.read(0x28))
print("Result:", result)
```

Expected output:

```text
Result: 120
```

## Verification status

- Vitis HLS synthesis completed successfully.
- HLS IP packaging completed successfully.
- Vivado synthesis and implementation completed successfully.
- Vivado bitstream generation completed successfully.
- PYNQ successfully loaded the overlay on the Ultra96-V2.
- The hardware test returned the expected result: `120`.

## Current limitation

This is an FPGA hardware integration proof-of-concept using a multiply-accumulate accelerator. The trained six-class gesture-recognition CNN has not yet been converted into an FPGA accelerator. The next hardware task is to implement the actual model or a suitable hardware approximation and compare its output with the software model.
