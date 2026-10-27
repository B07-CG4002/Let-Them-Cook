# Delivery validation and scope

Prepared against the independent-hand runtime and three-flex pipeline used in
this conversation. The main-branch three-flex pipeline was also read from GitHub
and matched the inspected training contract. The user's newer independent-hand
branch is local; the installer checks the required source hashes rather than
assuming remote main contains every local commit.

Local automated suite: **31 tests: 28 passed, 3 skipped** because PyTorch is
absent here. The skipped tests exercise actual training reproducibility/masking,
PyTorch numerical references, and native export/predictor compatibility. They
are included for the user's existing PyTorch environment; no success for those
paths is claimed until they run there.

Verified here:

- Capture channel order, physical-unit values, source identities and clock order.
- Reviewed annotation binding, overlap rejection and explicit unknown handling.
- Read-only recording audit, metadata/hash changes, grouped split enforcement.
- Duplicate source handling, sequence gaps and uint32 wrap behavior.
- Repeated replay/confirmation equivalence in virtual time.
- Right-only evaluation excluding unmodelled-hand misses.
- Complete one-shot versus partial/mixed-window preparation rules.
- Continuous grouped preparation across train/validation/test.
- Prepared-dataset tampering checks and training-only normalizer helper.
- Action-instance matching, duplicate/false activation accounting and exposure.
- Validation-only tuning gate; test selection is refused before output creation.
- Real fixed-point arithmetic in a synthetic parameter wrapper test; PyTorch
  reference call is mocked in that one wrapper test and separately tested in
  the skipped real-PyTorch test. It establishes adapter behavior, not real-model
  quantization accuracy.
- FPGA benchmark cache-control orchestration using a mocked backend, including
  cold forced reloads, warm alternating slots and exact-word comparison wiring.
  This does not program or measure an FPGA.
- HLS XML estimate parsing without a board-latency claim.
- Output overwrite refusal and HTML script-data escaping.
- Python compilation and generated JavaScript syntax checks.

Synthetic demo: passed, 302 predictions, 6 pulse actions, 6 state starts and 6
state ends on two independent streams. The fixture encodes labels in a synthetic
sensor channel and uses scripted probabilities. Its perfect matches are not
trained gesture accuracy. The replay/sweep run without real-time sleeping.

Unverified here: Windows execution; actual PyTorch training/export/reference
tests; visual browser layout; hardware fitting, BLE, actual three-flex model
accuracy, new narrowed hand HLS arithmetic, implemented timing closure, physical
FPGA timing and phone/game integration. No existing deployment is changed.

The seven experimental event-transport files and their unresolved Windows
transition-count assertion are outside this delivery and remain unmodified.
