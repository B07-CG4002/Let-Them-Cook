# Independent-hand AI checkpoint — 31 October 2026

Provisional checkpoint; not deployment approval. Original working checkout and raw data are preserved.

AI branch: AI-repaired-flex-g2000-checkpoint-20261031.
Communications review branch: comms/gyro2000-review-20261031, stacked on AI.
The AI PR includes physical +/-2000 acquisition/flash support because its recorder and firmware regression tests need it. Shared transport batch2 experiments and the deployment staging guard are deferred to Jie Yan's PR. His v5 PR #41 is already on main; it is not removed or reverted.

P01 campaign data/hand_raw_flex3/repaired_drop_g2000_v1: 30/4/6 per seven gestures per hand, 560 recordings. Raw data are local and not committed. All twelve repaired features are enabled by default; faulty RIGHT-index exclusion is explicit legacy only.

Candidate: .comms-local/hand_repaired_drop_g2000_v1_candidate1.
Separate LEFT and RIGHT models selected seed 73; validation 28/28, macro-F1 1.0 each. No held-out test evaluation or new FPGA export confirmed. Selected weights and raw evidence are preserved in local candidate/data directories and uploaded hand_candidate1_diagnosis_20261031_205148.zip, not included in these source commits.

Continuous recognition remains unreliable: WASH/IDLE switching prevents three stable predictions; RIGHT THROW often becomes DROP; preparation/recovery can trigger extra PICKUP/THROW/DROP. Latest live raw capture was about 50 Hz each hand, no missing sequence numbers or reboot. User confirmed motions match collection. Next task: compare raw train/live patterns, absolute orientation/flex distributions and window alignment before changing training or confirmation rules. Uploaded evidence must be attached again if unavailable to a new account.

Live debugging: explicit BLE scan using discovered BLEDevice objects, PyTorch imported first, sys.coinit_flags=0 and Bleak uninitialize_sta/assert_mta worked in a temporary console script. These are NOT permanent changes in the committed live CLI. Original live command may still have discovery/import-order issues. Preserve logs and finish a targeted tested runtime fix later.

Windows BLE throughput preference, cancellation-safe queue waits, GUI shutdown fixes, range/headroom guards, guided notes/counts and reversible capture quarantine are included. Guided budgets: 30/4/6 or 20/3/4, separate split sessions. Existing manual P01 data stay on the legacy path; guided uses a new campaign.

See HAND_GUIDED_COLLECTION_20261031.md and tools/run_hand_workflow.ps1. Run PowerShell helpers through powershell.exe -NoProfile -ExecutionPolicy Bypass -File. Do not rerun Prepare/Train into existing candidate outputs. Older external fingerprint checkers predate these sources; use current Audit.

No changes to socket_worker_hand.py, phone/game event framing, current threshold/confirmation settings, or FPGA architecture. Existing compatible overlay can be reused for new weights after export numeric and physical-board checks. Coordinate all shared deployment with Jie Yan. He requested a PR for communications edits and will review/merge it himself.

Known package caveat: the guided collection README describes the full unsplit source version. Its staging guard test is included only on the stacked communications branch; the AI-only range test deliberately omits that one integration assertion. Optional batch2 environments also exist only on communications. Existing raw/saved artifact manifests are not rewritten.
