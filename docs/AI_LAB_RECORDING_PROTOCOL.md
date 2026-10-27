# Recording checklist for the fitted three-flex gloves

## Before the first final recording

- Hardware fit must be settled first. Mount the IMU securely in a documented
  orientation; flex sensors must bend consistently across the intended joints.
- Verify actual physical hands against the GUI: LEFT device 1, RIGHT device 2.
  Preserve index=GPIO34/flex1, middle=GPIO35/flex2, thumb=GPIO39/flex3.
- Use Bluetooth for the recording data. USB can provide power/serial, but COM4
  is not the BLE identity and a COM port can change. Close competing BLE clients.
- Check all channels at rest, opening/closing fingers and rotating the wrist.
  A raw flex fraction is ADC/4095, not percent bend. A small span alone does not
  establish a broken sensor; inspect fit, wiring and repeatability together.
- Refit/recalibrate as required by firmware, then hold still during startup
  calibration. Record orientation photos and a hardware revision note.
- Do a three-trial pilot, audit it, inspect plots, then collect the larger set.
  Stop if IDs, rates, timestamps or finger responses are wrong.

## Record complete actions and natural context

Keep these seven labels: PICKUP, DROP, CHOP, WASH, THROW, POURING, IDLE. FIREEXT
is excluded from this independent-hand contract. Keep the agreed physical
definitions consistent across people. Decide whether throw variants are allowed
before collection; document any restricted chest-height protocol explicitly.

- Trial GUI: fit one complete action inside the two-second recording. Vary
  where it begins, but do not clip onset or completion simply to imitate a
  rolling window. Keep representative pauses/reset context within the window.
- Continuous evaluation: record preparation, complete actions, natural resets,
  pauses, off-task movements and both hands doing different actions. Use video
  where possible. Annotation later distinguishes actions/background/unknown.
- PICKUP/DROP: use clearly distinct object-hand interaction with natural hand
  movement; do not solve class confusion only by exaggerating a movement that
  will not be performed during gameplay. Agree the actual protocol with the team.
- CHOP/WASH/POURING: include starting, sustained and stopping behavior. Annotate
  sustained action separately from preparing/returning. Gameplay controls repetition.
- THROW: record the full agreed throw and reset; test realistic timing variation.
- IDLE: include several genuine resting/holding postures. Unrelated movement is
  not automatically IDLE. Keep uncertain background out of forced class labels.
- Both hands: each model sees its own sensor stream. Train LEFT using LEFT
  examples and RIGHT using RIGHT examples; concurrent different actions belong
  in independent-hand evaluation even if initial training is done one hand at a time.

## Person, session and splits

Person identifies the actual wearer. Session identifies a real collection
session/donning condition. A disconnect/reconnect alone does not create a new
independent session, and renaming a folder does not remove leakage.

For the existing trial pipeline, 20/3/4 per class is a practical first pilot.
Use distinct train/validation/test sessions and record them deliberately. That
means 189 trials per hand for seven classes. It is a starting point, not a
guarantee of accuracy. Expand styles/people according to measured failures.

For a generalization claim, hold out a wearer (when enough people are available)
as well as using separate sessions. Same people in all splits measures a
different, narrower setting. Exact 75/10/15 proportions are secondary to valid
group separation; the continuous lab preparation does not force those ratios.

## Re-recording and provenance

- Keep an untouched backup of raw CSV plus sidecar JSON. Record bad-trial reasons
  in a review note and exclude/move them out of the selected dataset as a reviewed
  operation. The audit never renames or deletes files automatically.
- Do not replace a file by renaming a trial from another person/session without
  retaining its actual metadata and provenance. Filenames are not ground truth.
- Keep GPIO/IMU orientation/hardware revision, wearer, session, software version
  and protocol version with the collection notes.
- Freeze validation choices before final test. If a test result informs a new
  protocol or model choice, treat it as development evidence and collect fresh
  final held-out recordings.

## Acceptance checklist for your AI handover

1. Input contract: 50 positions × 12 ordered physical-unit features per hand,
   declared duration, source timestamps and proper angle interpolation.
2. Per-hand selected weights, normalization, class names and configuration agree.
3. Recording-level and continuous event metrics are reported separately.
4. Raw probabilities and accepted action records remain available for diagnosis.
5. Confidence threshold, confirmations, timeout and rearm semantics are frozen.
6. Float/fixed/HLS/RTL/board agreement is scoped to the actual selected weights.
7. Hardware inference time and gesture-to-acceptance time are reported separately.
8. Comms receives the agreed output fields and owns delivery/reconnect/phone
   integration; game owner checks action validity and ongoing-state repetition.
