# Three-flex gesture protocol — draft for team confirmation

Purpose: align new twelve-feature glove recordings with intended game interactions. This is not a replacement for team approval of gesture styles or the requirement to support natural variations.

All recordings: one hand per labelled sample; two-second raw capture at nominal 50 Hz, resampled to 50 positions. LEFT/RIGHT stay independent. Use the final sensor mounting, correct wearer/session/split and a fresh folder. Keep reset motions outside baseline captures. Vary action onset and speed; later collect intentionally labelled transitions/partial actions without splitting related windows across train/validation/test.

| Class | Pilot physical motion | Context handled by game / confirmation needed |
|---|---|---|
| PICKUP | Reach towards the intended object and close the hand to grasp; finish holding it. | Game checks reachable object and whether the hand can take it. Avoid recording only the static held pose as PICKUP. |
| DROP | From holding, lower the hand then open/release, as used in the revised DROP protocol. | Game releases the held object. Confirm whether release without lowering is also expected; if so add it intentionally as a variant. |
| THROW | Current pilot: chest-height forward throwing motion with release; include the complete action. | Current restricted pilot style is not evidence of support for overhand/underhand styles. Game allows solid ingredients only, determines destination from viewing direction, and handles off-camera tracking behaviour. |
| CHOP | Perform the intended repeated down/up chopping motion naturally, including complete cycles in the window. | Game requires three units of progress and caps it at three; knife remains held. Team must define whether each unit represents a physical stroke or a timed animation tick. Overlapping predictions cannot establish independent strokes. |
| WASH | Perform the intended rubbing/washing motion with both hands; record synchronized streams with WASH on each hand independently. | Game requires both hands at sink with valid concurrent WASH for three seconds; no plate-grabbing animation required. Agree whether interruption pauses or resets progress and how uncertainty/tracking loss expires the state. |
| POURING | Raise/position as needed and tilt the held imaginary pot towards the camera, following the demonstrated interaction. | Confirm pot hand requirements, trigger angle, whether sustained tilt is ongoing POURING, and exit behaviour. Camera-relative spatial checks stay in game/CV. |
| IDLE | Natural resting and holding poses without a target action; include realistic finger positions. | Confident IDLE differs from uncertainty. Do not automatically label any failed/low-confidence action as IDLE. |

Postprocessing proposal: require three consecutive same-class updates above 0.70 initially; discrete PICKUP/DROP/THROW events emit once with an agreed rearm rule; CHOP/WASH/POURING maintain per-hand states with freshness/exit rules. Three overlapping window updates are not independent evidence. Game validates held objects, stations, positions, timers and counts. CHOP counting and state interruption rules must be resolved before this proposal is treated as final.

Review status: based on screenshots supplied on 25 October 2026; final game videos, actual app build and training protocol still need hands-on confirmation.
