"""Replay a live predictions.jsonl through temporal and object gameplay gates."""
import argparse
import json
import sys
from pathlib import Path

# When this file is invoked as `python tools/replay_gameplay_gate.py`, Python
# puts `tools/` on sys.path rather than the repository root. Add the root so
# the existing `src...` import works both as a script and with `python -m`.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.hand_ai_flex3.gameplay_postprocess import ObjectActionGate, TemporalPostProcessor


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--predictions', required=True)
    p.add_argument('--object', required=True,
                   choices=['POT', 'INGREDIENT_WHOLE', 'INGREDIENT_CHOPPED',
                            'PLATE_CLEAN', 'PLATE_WITH_FOOD', 'DIRTY_PLATE_STACK', 'KNIFE'])
    p.add_argument('--initial-state', choices=['target', 'held'], required=True)
    p.add_argument('--two-hand-wash', action='store_true')
    a = p.parse_args()

    temporal = TemporalPostProcessor()
    gate = ObjectActionGate()
    for device in (1, 2):
        if a.initial_state == 'target':
            gate.set_target(device, a.object)
        else:
            gate.set_held(device, a.object)

    counts = {'predictions': 0, 'temporal_events': 0, 'accepted': 0, 'rejected': 0}
    source = Path(a.predictions)
    if not source.is_file():
        raise SystemExit(f'Missing predictions file: {source}')
    for line in source.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get('type') != 'prediction':
            continue
        counts['predictions'] += 1
        for event in temporal.update(row['device_id'], row['gesture'],
                                     row['confidence'], row['elapsed_s']):
            counts['temporal_events'] += 1
            if event['gesture'] == 'WASH' and a.two_hand_wash:
                # Both hands must produce WASH within the gate's synchronization window.
                decision = gate.accept(event['device_id'], 'WASH', row['elapsed_s'],
                                       event.get('confidence'))
            else:
                decision = gate.accept(event['device_id'], event['gesture'],
                                       row['elapsed_s'], event.get('confidence'))
            key = 'accepted' if decision.accepted else 'rejected'
            counts[key] += 1
            print(json.dumps(dict(time=row['elapsed_s'], hand=event['device_id'],
                                  gesture=event['gesture'], accepted=decision.accepted,
                                  reason=decision.reason, object=decision.object_name),
                             sort_keys=True))
    print(json.dumps(dict(status='finished', counts=counts), indent=2))


if __name__ == '__main__':
    main()
