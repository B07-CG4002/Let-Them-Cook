"""Plan and audit native three-flex trial collection; no BLE or model execution."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from src.hand_ai_flex3.core import CONFIG, LABELS, SPLITS, validate_ratios


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ValueError('Person/revision must use letters, digits, underscore or hyphen')
    return value


def split_counts(total):
    # Integer largest-remainder arithmetic, stable ties in train/validation/test order.
    weights = (75, 10, 15)
    result = [total * weight // 100 for weight in weights]
    order = sorted(range(3), key=lambda i: -(total * weights[i] % 100))
    for i in order[:total - sum(result)]:
        result[i] += 1
    return dict(zip(SPLITS, result))


def make_plan(people, revision, raw_root, per_person_total=27):
    people = [token(p) for p in people]
    token(revision)
    if not people or len(set(people)) != len(people):
        raise ValueError('Need distinct actual wearer IDs')
    if type(per_person_total) is not int or per_person_total < 10:
        raise ValueError('Use at least ten total trials per class/person')
    totals = split_counts(len(people) * per_person_total)
    if min(totals.values()) < len(people):
        raise ValueError('Not enough validation/test trials for each wearer')
    sessions = []
    for hand, device in (('LEFT', 1), ('RIGHT', 2)):
        for split in SPLITS:
            base, extra = divmod(totals[split], len(people))
            for index, person in enumerate(people):
                count = base + (index < extra)
                sessions.append(dict(person=person, hand=hand, device_id=device,
                                     session=f'{person}_{revision}_{split}_s1', split=split,
                                     per_gesture_target=count))
    plan = dict(format=1, contract=CONFIG, revision=revision, people=people,
                raw_root=str(Path(raw_root)), per_class_per_hand=totals, sessions=sessions,
                total_trials=2 * len(LABELS) * sum(totals.values()),
                evaluation_scope='Same listed wearers across separate sessions; not unseen-person evaluation',
                rules=['Session names describe genuinely separate collection/donning sessions.',
                       'Do not split one continuous collection by renaming its session.',
                       'Collect complete actions; annotate natural transitions in separate live validation.',
                       'Counts are allocated across the cohort before largest-remainder rounding.',
                       'No hardware readiness or final accuracy is established by this plan.'])
    check_plan(plan)
    return plan


def check_plan(plan):
    if plan.get('format') != 1 or plan.get('contract') != CONFIG:
        raise ValueError('Plan contract differs from the native twelve-feature pipeline')
    people = plan['people']
    if not people or len(set(people)) != len(people):
        raise ValueError('Invalid people')
    for person in people:
        token(person)
    seen = set()
    rows = {1: [], 2: []}
    for session in plan['sessions']:
        person, name, split, device = (session[k] for k in ('person','session','split','device_id'))
        token(name)
        if person not in people or split not in SPLITS or type(device) is not int or device not in rows:
            raise ValueError('Invalid session identity')
        if session['hand'] != {1:'LEFT',2:'RIGHT'}[device]:
            raise ValueError('Hand/device mismatch')
        key = device, person, name
        if key in seen:
            raise ValueError('Duplicate session')
        seen.add(key)
        n = session['per_gesture_target']
        if type(n) is not int or n < 1:
            raise ValueError('Invalid target count')
        for gesture in LABELS:
            rows[device].extend(dict(person=person, session=name, split=split,
                                     gesture=gesture) for _ in range(n))
    for device in rows:
        counts = validate_ratios(rows[device])
        if any(values != plan['per_class_per_hand'] for values in counts.values()):
            raise ValueError('Declared split totals differ from sessions')
        if {r['person'] for r in rows[device]} != set(people):
            raise ValueError('One hand is missing a wearer')
    if sum(map(len, rows.values())) != plan['total_trials']:
        raise ValueError('Declared trial total differs')
    return rows


def progress(plan, repo):
    """Read all trial CSVs under the explicit plan root; never modify raw files."""
    from src.hand_ai_lab.data import audit_recordings
    check_plan(plan)
    root = Path(plan['raw_root'])
    if not root.is_absolute():
        root = Path(repo) / root
    audit = (audit_recordings(root) if any(root.rglob('*.csv')) else
             dict(files=[], errors=[], warnings=[], features={}))
    errors = list(audit['errors'])
    # A sidecar without its CSV is not a completed recording.
    for path in sorted(root.rglob('*.json')):
        if path.name != 'session.json' and not path.with_suffix('.csv').is_file():
            errors.append(dict(path=str(path), error='JSON sidecar has no matching CSV; keep notes outside the selected root'))
    expected = {(s['device_id'], s['person'], s['session'], s['split']):s
                for s in plan['sessions']}
    counts = Counter()
    for item in audit['files']:
        if not item.get('valid'):
            continue
        meta = item['metadata']
        key = tuple(meta[k] for k in ('device_id','person','session','split'))
        if key not in expected:
            errors.append(dict(path=item['path'], error='Recording is outside the planned hand/person/session/split'))
            continue
        session = expected[key]
        folder = root / session['hand'].lower() / session['session']
        path = Path(item['path'])
        if path.parent.resolve() != folder.resolve():
            errors.append(dict(path=str(path), error='Folder does not match the GUI plan'))
            continue
        marker = folder / 'session.json'
        wanted = {k:meta[k] for k in ('person','session','split','device_id','recording_contract')}
        try:
            if read(marker) != wanted:
                raise ValueError('Session marker differs from trial metadata')
        except (ValueError, OSError) as error:
            errors.append(dict(path=str(marker), error=str(error)))
            continue
        counts[(*key, meta['gesture'])] += 1
    tasks = []
    for key, session in expected.items():
        for gesture in LABELS:
            have = counts[(*key, gesture)]
            target = session['per_gesture_target']
            tasks.append(dict(**session, gesture=gesture, valid_trials=have,
                              remaining=max(0,target-have), excess=max(0,have-target)))
    if any(t['excess'] for t in tasks):
        errors.append(dict(error='Extra selected recordings exceed plan; review exclusions or create a revised plan, never delete automatically'))
    remaining = sum(t['remaining'] for t in tasks)
    return dict(status='blocked' if errors else ('incomplete' if remaining else 'collection_complete'),
                scope='File integrity, identity and planned counts only; no physical sensor or gesture-quality certification',
                raw_root=str(root.resolve()), remaining_trials=remaining,
                errors=errors, warnings=audit['warnings'], features=audit['features'], tasks=tasks,
                next_step='Review sensor response and action quality, then run native prepare separately for each hand')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('plan')
    p.add_argument('--people', nargs='+', required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('--raw-root', required=True)
    p.add_argument('--per-person-total', type=int, default=27)
    p.add_argument('--out', required=True)
    p = sub.add_parser('progress')
    p.add_argument('--plan', required=True)
    p.add_argument('--repo', default='.')
    p.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    if args.command == 'plan':
        result = make_plan(args.people,args.revision,args.raw_root,args.per_person_total)
    else:
        result = progress(read(args.plan),args.repo)
        result['plan_sha256'] = hashlib.sha256(Path(args.plan).read_bytes()).hexdigest()
    write_new(args.out,result)
    if args.command == 'plan':
        print('PLAN SAVED:',args.out)
        print('Per class/per hand:',result['per_class_per_hand'])
        print('Total trials:',result['total_trials'])
        for s in result['sessions']:
            print(s['hand'],s['person'],s['split'],s['session'],':',s['per_gesture_target'],'per gesture')
    else:
        print('Status:',result['status'],'| Remaining:',result['remaining_trials'])
        for e in result['errors']:
            print('ERROR:',e)
        for t in result['tasks']:
            if t['remaining'] or t['excess']:
                print(t['hand'],t['person'],t['split'],t['gesture'],
                      'remaining=',t['remaining'],'excess=',t['excess'])
        print('Report:',args.out)
        if result['status'] == 'blocked':
            raise SystemExit(2)


if __name__ == '__main__':
    main()
