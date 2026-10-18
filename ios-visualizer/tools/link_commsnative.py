#!/usr/bin/env python3
"""Link the CommsNative Swift package into a freshly generated Unity iOS export.

Unity regenerates its Xcode project on every iOS build, so this runs after each
build. It adds a local Swift package reference to ios-visualizer/CommsNative
and links the CommsNative product into the UnityFramework target, which is
where IL2CPP resolves the game's [DllImport("__Internal")] Comms* calls.
Nothing else in the export changes: signing stays on the xcodebuild command
line. Reapplying is a no-op.

  link_commsnative.py --export <dir>          link the package
  link_commsnative.py --export <dir> --pin    after Xcode resolved packages:
                                              copy the tested versions from
                                              CommsNative/Package.resolved
"""
from pathlib import Path
import argparse
import json
import os
import re
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parents[1] / 'CommsNative'
PRODUCT = 'CommsNative'
# Fixed object IDs, chosen outside the hex ranges Unity's template uses.
BUILD_FILE = 'C0AA5E000000000000000001'
LOCAL_REF = 'C0AA5E000000000000000002'
PRODUCT_DEP = 'C0AA5E000000000000000003'
RESOLVED = 'Unity-iPhone.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved'


def _object_span(text, identifier):
    """Return (start, end) of the body of the one PBX object with this ID."""
    matches = list(re.finditer(r'^\s*' + identifier + r'\s*(?:/\*[^\n]*?\*/\s*)?=\s*\{(?=\s*isa\s*=)', text, re.M))
    if len(matches) != 1:
        raise ValueError('Unknown or duplicate Xcode object: ' + identifier)
    start = matches[0].end()
    depth, quoted, escaped = 1, False, False
    for position in range(start, len(text)):
        character = text[position]
        if quoted:
            if escaped:
                escaped = False
            elif character == '\\':
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == '{':
            depth += 1
        elif character == '}':
            depth -= 1
            if depth == 0:
                return start, position
    raise ValueError('Incomplete Xcode object: ' + identifier)


def _body(text, identifier):
    start, end = _object_span(text, identifier)
    return re.sub(r'/\*.*?\*/', '', text[start:end], flags=re.S)


def _list_ids(body, field):
    values = re.findall(r'\b' + field + r'\s*=\s*\((.*?)\);', body, flags=re.S)
    if len(values) > 1:
        raise ValueError('Duplicate Xcode list: ' + field)
    return re.findall(r'\b[A-F0-9]{24}\b', values[0]) if values else None


def _scalar(body, field):
    values = re.findall(r'\b' + field + r'\s*=\s*([^;]*);', body)
    return values[0].strip().strip('"') if len(values) == 1 else None


def _find(text):
    """Locate the project, the UnityFramework target and its Frameworks phase."""
    root = _scalar(re.sub(r'/\*.*?\*/', '', text, flags=re.S), 'rootObject')
    if root is None or _scalar(_body(text, root), 'isa') != 'PBXProject':
        raise ValueError('No PBXProject root object')
    targets = [t for t in _list_ids(_body(text, root), 'targets') or []
               if _scalar(_body(text, t), 'name') == 'UnityFramework'
               and _scalar(_body(text, t), 'isa') == 'PBXNativeTarget']
    if len(targets) != 1:
        raise ValueError('Expected exactly one UnityFramework target')
    target = targets[0]
    phases = [p for p in _list_ids(_body(text, target), 'buildPhases') or []
              if _scalar(_body(text, p), 'isa') == 'PBXFrameworksBuildPhase']
    if len(phases) != 1:
        raise ValueError('Expected exactly one Frameworks phase in UnityFramework')
    return root, target, phases[0]


def _append_to_list(text, identifier, field, entry):
    """Add one entry to a list field of an object, creating the field if absent."""
    start, end = _object_span(text, identifier)
    body = text[start:end]
    match = re.search(r'(\n(\s*)' + field + r'\s*=\s*\()(.*?)(\);)', body, flags=re.S)
    if match:
        inner = match.group(3)
        if '\n' in inner:
            indent = match.group(2) + '\t'
            inner = inner.rstrip(' \t') + indent + entry + ',\n' + match.group(2)
        else:
            inner = inner.strip() + (' ' if inner.strip() else '') + entry + ', '
        body = body[:match.start(3)] + inner + body[match.end(3):]
    else:
        isa = re.search(r'\n(\s*)isa\s*=[^;]*;', body)
        if not isa:
            raise ValueError('Xcode object without isa: ' + identifier)
        body = body[:isa.end()] + '\n' + isa.group(1) + field + ' = (' + entry + ', );' + body[isa.end():]
    return text[:start] + body + text[end:]


def _insert_section(text, name, content):
    begin, finish = '/* Begin %s section */' % name, '/* End %s section */' % name
    if finish in text:
        return text.replace(finish, content + finish, 1)
    anchor = '/* End PBXProject section */'
    if text.count(anchor) != 1:
        raise ValueError('Unexpected Xcode project layout')
    return text.replace(anchor, anchor + '\n\n' + begin + '\n' + content + finish, 1)


def validate(text, relative, ids=(BUILD_FILE, LOCAL_REF, PRODUCT_DEP)):
    build_file, local_ref, product_dep = ids
    root, target, phase = _find(text)
    checks = [
        local_ref in (_list_ids(_body(text, root), 'packageReferences') or []),
        product_dep in (_list_ids(_body(text, target), 'packageProductDependencies') or []),
        build_file in (_list_ids(_body(text, phase), 'files') or []),
        _scalar(_body(text, build_file), 'productRef') == product_dep,
        _scalar(_body(text, local_ref), 'isa') == 'XCLocalSwiftPackageReference',
        _scalar(_body(text, local_ref), 'relativePath') == relative,
        _scalar(_body(text, product_dep), 'productName') == PRODUCT,
    ]
    if not all(checks):
        raise ValueError('CommsNative link is incomplete or points elsewhere')


def _objects(text, isa):
    """IDs of every object of one isa."""
    return [m.group(1) for m in re.finditer(r'^\s*([A-F0-9]{24})\s*(?:/\*[^\n]*?\*/\s*)?=\s*\{\s*isa\s*=\s*' + isa + r'\s*;', text, re.M)]


def adopt(text, relative):
    """Repair a CommsNative link someone added by hand in Xcode on another Mac.

    Xcode stores the package as a path relative to the export, so an export
    made elsewhere points at that Mac's checkout. Point it at this one and add
    any edge of the chain Xcode left out; keep the existing object IDs.
    """
    refs = [r for r in _objects(text, 'XCLocalSwiftPackageReference')
            if os.path.basename((_scalar(_body(text, r), 'relativePath') or '').rstrip('/')) == 'CommsNative']
    deps = [d for d in _objects(text, 'XCSwiftPackageProductDependency') if _scalar(_body(text, d), 'productName') == PRODUCT]
    if len(refs) != 1 or len(deps) != 1:
        return None
    ref, dep = refs[0], deps[0]
    files = [b for b in _objects(text, 'PBXBuildFile') if _scalar(_body(text, b), 'productRef') == dep]
    if len(files) > 1:
        raise ValueError('Several build files for CommsNative; inspect the export first')
    root, target, phase = _find(text)
    start, end = _object_span(text, ref)
    body = re.sub(r'(\brelativePath\s*=\s*)[^;]*;', lambda m: m.group(1) + '"' + relative + '";', text[start:end], count=1)
    text = text[:start] + body + text[end:]
    if not files:
        files = [BUILD_FILE]
        text = _insert_section(text, 'PBXBuildFile',
            '\t\t%s /* %s in Frameworks */ = {isa = PBXBuildFile; productRef = %s /* %s */; };\n' % (BUILD_FILE, PRODUCT, dep, PRODUCT))
    if files[0] not in (_list_ids(_body(text, phase), 'files') or []):
        text = _append_to_list(text, phase, 'files', '%s /* %s in Frameworks */' % (files[0], PRODUCT))
    if dep not in (_list_ids(_body(text, target), 'packageProductDependencies') or []):
        text = _append_to_list(text, target, 'packageProductDependencies', '%s /* %s */' % (dep, PRODUCT))
    if ref not in (_list_ids(_body(text, root), 'packageReferences') or []):
        text = _append_to_list(text, root, 'packageReferences', '%s /* XCLocalSwiftPackageReference "%s" */' % (ref, relative))
    validate(text, relative, (files[0], ref, dep))
    return text


def link(text, relative):
    if LOCAL_REF in text:
        validate(text, relative)
        return text
    adopted = adopt(text, relative)
    if adopted is not None:
        return adopted
    if any(i in text for i in (BUILD_FILE, PRODUCT_DEP)) or 'XCLocalSwiftPackageReference' in text:
        raise ValueError('Export already has a different Swift package setup; inspect it first')
    root, target, phase = _find(text)
    text = _insert_section(text, 'PBXBuildFile',
        '\t\t%s /* %s in Frameworks */ = {isa = PBXBuildFile; productRef = %s /* %s */; };\n' % (BUILD_FILE, PRODUCT, PRODUCT_DEP, PRODUCT))
    text = _append_to_list(text, phase, 'files', '%s /* %s in Frameworks */' % (BUILD_FILE, PRODUCT))
    text = _append_to_list(text, target, 'packageProductDependencies', '%s /* %s */' % (PRODUCT_DEP, PRODUCT))
    text = _append_to_list(text, root, 'packageReferences', '%s /* XCLocalSwiftPackageReference "%s" */' % (LOCAL_REF, relative))
    text = _insert_section(text, 'XCLocalSwiftPackageReference',
        '\t\t%s /* XCLocalSwiftPackageReference "%s" */ = {\n\t\t\tisa = XCLocalSwiftPackageReference;\n\t\t\trelativePath = "%s";\n\t\t};\n' % (LOCAL_REF, relative, relative))
    text = _insert_section(text, 'XCSwiftPackageProductDependency',
        '\t\t%s /* %s */ = {\n\t\t\tisa = XCSwiftPackageProductDependency;\n\t\t\tproductName = %s;\n\t\t};\n' % (PRODUCT_DEP, PRODUCT, PRODUCT))
    validate(text, relative)
    return text


def pin(export):
    """Give the export's resolved pins the versions tested in CommsNative."""
    path = export / RESOLVED
    current = json.loads(path.read_text())
    tested = {p['identity']: p['state'] for p in json.loads((PACKAGE / 'Package.resolved').read_text())['pins']}
    changed = []
    for entry in current['pins']:
        state = tested.get(entry['identity'])
        if state and entry['state'] != state:
            changed.append('%s %s -> %s' % (entry['identity'], entry['state'].get('version'), state.get('version')))
            entry['state'] = state
    missing = set(tested) - {p['identity'] for p in current['pins']}
    if missing:
        raise ValueError('Export resolution lacks tested packages: ' + ', '.join(sorted(missing)))
    if changed:
        _write(path, (json.dumps(current, indent=2) + '\n').encode())
    return changed


def _write(path, data):
    with tempfile.NamedTemporaryFile(prefix='.comms-link-', dir=path.parent, delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    temporary.chmod(path.stat().st_mode & 0o777)
    os.replace(temporary, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--export', type=Path, required=True, help='Unity iOS build folder (holds Unity-iPhone.xcodeproj)')
    parser.add_argument('--pin', action='store_true', help='pin resolved package versions to CommsNative/Package.resolved')
    args = parser.parse_args()
    export = args.export.resolve()
    try:
        if args.pin:
            changed = pin(export)
            print('Pinned: ' + ('; '.join(changed) if changed else 'already the tested versions'))
            return
        project = export / 'Unity-iPhone.xcodeproj/project.pbxproj'
        before = project.read_text(encoding='utf-8')
        relative = os.path.relpath(PACKAGE, export)
        after = link(before, relative)
        if after != before:
            _write(project, after.encode('utf-8'))
            print('Linked %s into UnityFramework (%s)' % (PRODUCT, relative))
        else:
            print('%s already linked (%s)' % (PRODUCT, relative))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        print('COMMSNATIVE_LINK_FAILED: ' + str(error), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
