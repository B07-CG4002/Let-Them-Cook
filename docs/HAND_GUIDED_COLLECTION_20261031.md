# Guided collection and model workflow — 31 October 2026

Prepared here, not installed on Wen Ree's Windows laptop, committed, merged or deployed.
Current GitHub handoff/AI CLI were read before preparing this patch. The local installed packages contain newer uncommitted range, BLE and shutdown fixes; installer fingerprints check those versions. No root AGENTS.md was returned by GitHub.

## What changes

New tools/hand_guided_collection.py: one guided window across separate train/validation/test sessions, prefilled notes, counts, paired gesture, locked root/session, automatic next incomplete variant after a saved batch, Previous/Next, Next split with break/refit prompt, review/redo selected captures. Never starts recording automatically. Default batches of 3 rotate through variants within a split. Start again with the SAME person/date/campaign/budget to resume. Closing does not open an edge GUI.

New tools/hand_capture_review.py: read-only CSV hash/session/pair/count inventory plus explicit reversible quarantine by capture ID. Both hands and both CSV/JSON files are preserved outside the raw root before removing active copies. Session markers stay. Reasons and exact backup hashes are recorded. Use this for old manual campaigns too. It does NOT guess whether a movement was correct or relabel raw data.

src/hand_ai_flex3/wide_training.py: ratio validation is per wearer before counts are aggregated; rounding combined people previously could reject valid individual allocations. Existing preprocessing, models, optimizer, normalization and assigned splits are unchanged. All 12 repaired features stay enabled by default. No firmware, BLE, worker communications, FPGA or event-filter changes.

New tests/test_hand_guided_collection.py covers budgets, corrected THROW descriptions, exact-note resume, paired quarantine/dry run, tamper refusal and per-person split rounding. run.ps1 and this guide remain in the downloaded kit; installer only writes the four explicit payload files.

## Install and verify (repository-root PowerShell)

Close recorder GUIs first. Keep .venv active or use its explicit executable as below. Never rerun old package installers over newer sources. Installer accepts LF/CRLF, refuses unfamiliar versions, preflights all payloads, backs up changed source and rolls back on install failure. No staging, commit, push or deployment.

```powershell
$destination = "$env:USERPROFILE\Downloads\hand_guided_collection_v1_package"
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\hand_guided_collection_v1.zip" -DestinationPath $destination
$kit = Join-Path $destination 'hand_guided_collection_v1'
git status --short --branch
.\.venv\Scripts\python.exe "$kit\install.py" --repo .
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed.' }
.\.venv\Scripts\python.exe "$kit\install.py" --repo . --apply
if ($LASTEXITCODE -ne 0) { throw 'Installation failed.' }
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hand_guided_collection.py -v
if ($LASTEXITCODE -ne 0) { throw 'New checks failed.' }
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hand_tonight.py -v
if ($LASTEXITCODE -ne 0) { throw 'Pipeline checks failed.' }
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hand_range2000.py -v
if ($LASTEXITCODE -ne 0) { throw 'Range checks failed.' }
```

Smoke-check the new GUI on Windows before scheduling friends. Use a NEW disposable campaign, no recording required: `python -m tools.hand_guided_collection --person CHECK --date 20261031 --campaign guided_ui_check --print-plan` prints only; remove --print-plan to inspect UI, Previous/Next, fixed fields, connect/close. Do not use this check campaign for training. Read the palm definitions and confirm them with a reference photo. Physical +/-2000 readbacks and repaired flex response remain hardware prerequisites; BLE acknowledgement alone does not verify them.

## Next for your EXISTING completed P01 30/4/6 set

No recollection or edge clips are required to train. Preserve the corrected recordings and backups. This command sequence audits counts/pairs/hashes, runs the deeper native timing/range audit, prepares inputs, trains separate hands with validation seed/epoch selection, and runs local live inference. Stop after ANY failed step. The wrapper throws on nonzero Python exit.

```powershell
$kit = "$env:USERPROFILE\Downloads\hand_guided_collection_v1_package\hand_guided_collection_v1"
$work = '.comms-local/hand_repaired_drop_g2000_v1_candidate1'
& "$kit\run.ps1" -Step Audit
& "$kit\run.ps1" -Step Prepare -Work $work
& "$kit\run.ps1" -Step Train -Work $work
& "$kit\run.ps1" -Step Validation -Work $work
& "$kit\run.ps1" -Step Live -Work $work
```

Wear the gloves; close other BLE/serial clients. Live is 180 seconds, CPU only, threshold >0.70, same existing confirmation/filter policy. Complete logs are retained; Ctrl+C interruption is not by itself a crash. No game commands are sent. File-exists failures protect old outputs: resume at the next unfinished step; do not rerun Prepare or Train into completed directories. A repeat experiment gets a NEW -Work such as candidate2, preserving candidate1. Work names are stable, live subfolders timestamped.

Read `$work/models/selected/selection.json`, each hand's validation.json and `$work/validation.json`. Numbers do not establish continuous action accuracy or unseen-person performance. Training 3 seeds may take time; do not equate synthetic smoke-test scores with gesture accuracy. Never pass --exclude-right-index for the repaired-sensor run.

If Audit reports count differences, investigate per wearer/hand/class. If timing, source identity, range, hashes or pairing fail, review those trials before preparation. The older external check_collection.py fingerprint list predates the training-source update and will reject that source; use this kit's Audit instead. The native progress command may report incomplete instead of exiting nonzero; Prepare revalidates and refuses invalid inputs. Label/variant semantic mistakes cannot be detected from hashes. The archive preserves originals but active dataset counts should return to exact targets after replacements.

## Live comparison and keep/reject decision

Use a fixed observed action script, same fit, natural speeds and several repetitions: all gestures, DROP vs THROW, pickup/hold/recovery, drop/retract, throw/retract, one hand moving while the other rests, mixed simultaneous actions. Record/video actual movements and event times. Compare missed intended actions, extra ACCEPT/START events, DROP/THROW swaps, latency and background errors. Console candidate counts alone are not event accuracy. Report hands separately.

Keep the new candidate only if it improves the observed behaviour that matters without unacceptable regressions. Preserve selected weights, matching normalization/config, validation report, raw provenance and annotated live evidence. Models remain provisional. Do not overwrite models/hand_v5_20261029 or the checked-in FPGA bundle.

Old models were collected at +/-1000 with faulty RIGHT index masking; new models use repaired sensors at +/-2000. A live old/new comparison on current hardware is an operational baseline with these confounds, not a controlled claim that one change improved accuracy. Do not force an old model through new prepared-data configuration checks. Future candidates can be compared on the same untouched compatible benchmark. Prefer a validation/development continuous recording for policy tuning; reserve a separate final assessment run.

When model/filter choices are frozen, evaluate the held-out isolated test ONCE:

```powershell
& "$kit\run.ps1" -Step EvaluateTest -Work $work -AllowTestUse
& "$kit\run.ps1" -Step Export -Work $work
```

Test report: `$work/heldout_test.json`. FPGA bundle: `$work/fpga_bundle`. Repeated tuning against test results makes that test a development set; collect a fresh final test session if it is used that way. Coordinate bundle/runtime and physical-board verification with Jie Yan. Existing architecture/overlay can be reused if the numeric hardware contract still matches; no weight-only Vivado rebuild.

## New wearers: fewer clicks and an optional smaller budget

Do NOT reopen completed P01/v1 data in the new guided mode. Its notes/plan tracking is deliberately strict and refuses adopting pre-existing manual data. Continue legacy resume if P01 correction is needed.

Use a new campaign for guided sessions:

```powershell
& "$kit\run.ps1" -Step Guided -Person P02 -Date 20261031 -Campaign repaired_drop_g2000_guided_v2 -TrainTarget 20 -Stage train
```

Resume later with the exact SAME arguments; choose -Stage validation or -Stage test to go straight there. The date identifies the session; it need not change after midnight. Add P03/P04 using their own Person IDs. Choose budget BEFORE collection; a saved plan refuses changed arguments. Use -TrainTarget 30 for supervised full collection. 20 means 20/3/4 (189 paired recording windows/person total); 30 means 30/4/6 (280 windows). These budgets are an experiment, not a guarantee that 20 will match 30. GUI automation removes copy/paste time but not the 4-second physical prep/capture minimum or breaks. Smaller validation/test samples make estimates less stable.

For 20, movement allocations are apportioned deterministically with at least one training example per defined variant. For validation 3, not every four-way combination fits; test 4 covers all four for action gestures. IDLE has fewer evaluation categories. For 30, all agreed 30/4/6 allocations and corrected THROW orientations are preserved.

Next split keeps separate sessions and prompts a break/refit. It does NOT merge validation and test into the same batch. That would weaken their session separation. One gesture drives both hands; mixed gestures remain in the separate edge tool. All fields are fixed by the launch plan; root is campaign-based and split/person sessions are subfolders. Previous/Next changes the displayed plan row, not saved data. Full rows cannot be overfilled. Redo uses the review window: choose exact capture, enter reason, preserve BOTH hands, return to affected plan row to record replacement. Review shows observed descriptions only as metadata; select carefully.

Audit a guided campaign with its wearer budget, then prepare/train in a different Work directory. The simple Audit count target applies to all wearers in that campaign: use separate campaigns if budgets differ, or inspect the per-wearer native progress and deeper Prepare validation. To combine separate campaigns, do not flatten or copy selected files arbitrarily; preserve their session directories and validate a combined snapshot first. This kit does not automatically assemble multi-campaign training data.

## Corrections for existing manual recordings

Read-only inventory/counts:

```powershell
.\.venv\Scripts\python.exe -m tools.hand_capture_review --root data/hand_raw_flex3/repaired_drop_g2000_v1
```

Dry-run a specific capture ID (replace CAPTURE_ID and reason):

```powershell
.\.venv\Scripts\python.exe -m tools.hand_capture_review --root data/hand_raw_flex3/repaired_drop_g2000_v1 --capture CAPTURE_ID --reason 'Wrong movement'
```

Repeat with --apply to preserve/quarantine the pair. This frees its count for legacy resume. It never just edits a label to make a wrong movement appear correct. To find IDs for manual campaigns, use your saved GUI log or the earlier JSON listing command. No Stage All, no deletion of recordings/backups.

## Edge checks and timeout concerns

Edge clips are NOT part of the isolated supervised training inputs. Busy volunteers may prioritize gesture sets and skip the edge session. Wen Ree can initially do continuous development checks alone. Before claiming performance for other people, include at least brief continuous checks on representative other wearers; own-only testing cannot establish their reliability. Three-second clips are useful short examples, but longer sequences are needed for repeated actions and warmed-up sliding windows.

Optional existing edge GUI:

```powershell
& "$kit\run.ps1" -Step Edges -Person P01 -Date 20261031
```

Leading/trailing spaces in old note fields were stripped before saving and do not enter the model input. Timeout means a packet wait deadline expired, not evidence that the gyro exceeded its range. Earlier completed saves stay; an incomplete current trial does not become a completed pair. Audit before training. Reconnect and resume using the same session; do not disable gap/headroom checks or silently interpolate missing source data to make collection pass. This kit does not claim to fix every underlying BLE timeout.

THROW versus DROP still needs evidence: THROW has a deliberate forward propulsion before release; DROP opens from a held position with little arm motion. Hold starting grip, then release within capture; do not turn DROP into a slow throw. A threshold cannot resolve identical sensor trajectories. Check actual traces and mistakes before adding direction rules, cooldowns or new training. Those post-processing changes are intentionally deferred until annotated continuous evidence is available.

## Validation scope

See TEST_RESULTS.txt. Python logic/integrity/shutdown tests were run here. Windows GUI interaction, WinRT BLE, physical range/flex response, full PyTorch training/export and FPGA hardware must be tested on the appropriate environment. No claim of new model accuracy, deployment or Windows GUI success is made by preparing this package.

Example full workflow for a NEW reduced-budget campaign after collection:

```powershell
$campaign = 'repaired_drop_g2000_guided_v2'
$work = '.comms-local/hand_guided_v2_candidate1'
& "$kit\run.ps1" -Step Audit -Campaign $campaign -TrainTarget 20
& "$kit\run.ps1" -Step Prepare -Campaign $campaign -Work $work
& "$kit\run.ps1" -Step Train -Campaign $campaign -Work $work
& "$kit\run.ps1" -Step Validation -Campaign $campaign -Work $work
& "$kit\run.ps1" -Step Live -Campaign $campaign -Work $work
```

Before recording more wearers, time a small guided practice session including a break and redo. If the locked GUI or close behaviour fails on Windows, stop and retain the traceback; the old manual recorder is still available and unmodified. No new firmware flashing is required for this package.
