"""link_commsnative.py --bundle-id renames the app target only and is idempotent."""
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "ios-visualizer" / "tools" / "link_commsnative.py"
SPEC = importlib.util.spec_from_file_location("link_commsnative", TOOL)
link = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(link)


def target(identifier, name, product_type, configurations):
    return (
        f"\t\t{identifier} /* {name} */ = {{\n\t\t\tisa = PBXNativeTarget;\n"
        f"\t\t\tbuildConfigurationList = {configurations} /* Build configuration list for PBXNativeTarget \"{name}\" */;\n"
        f"\t\t\tbuildPhases = (\n\t\t\t);\n\t\t\tname = \"{name}\";\n"
        f"\t\t\tproductType = \"{product_type}\";\n\t\t}};\n"
    )


def configuration_list(identifier, configurations):
    items = "".join(f"\t\t\t\t{c} /* Release */,\n" for c in configurations)
    return f"\t\t{identifier} = {{\n\t\t\tisa = XCConfigurationList;\n\t\t\tbuildConfigurations = (\n{items}\t\t\t);\n\t\t}};\n"


def configuration(identifier, bundle_id):
    setting = f"\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = {bundle_id};\n" if bundle_id else ""
    return (
        f"\t\t{identifier} /* Release */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n"
        f"\t\t\t\tDEVELOPMENT_TEAM = Z2K6Z5246T;\n{setting}\t\t\t}};\n\t\t\tname = Release;\n\t\t}};\n"
    )


def project(app_ids=("com.CG4002.unityTutorial", "com.CG4002.unityTutorial"), second_app=False):
    targets = ["A00000000000000000000001", "A00000000000000000000002", "A00000000000000000000003"]
    body = target(targets[0], "Unity-iPhone", "com.apple.product-type.application", "B00000000000000000000001")
    body += target(targets[1], "UnityFramework", "com.apple.product-type.framework", "B00000000000000000000002")
    body += target(targets[2], "Unity-iPhone Tests", "com.apple.product-type.bundle.unit-test", "B00000000000000000000003")
    if second_app:
        targets.append("A00000000000000000000004")
        body += target(targets[3], "Other", "com.apple.product-type.application", "B00000000000000000000003")
    body += configuration_list("B00000000000000000000001", ["C00000000000000000000001", "C00000000000000000000002"])
    body += configuration_list("B00000000000000000000002", ["C00000000000000000000003"])
    body += configuration_list("B00000000000000000000003", ["C00000000000000000000004"])
    body += configuration("C00000000000000000000001", app_ids[0])
    body += configuration("C00000000000000000000002", app_ids[1])
    body += configuration("C00000000000000000000003", "com.unity3d.framework")
    body += configuration("C00000000000000000000004", '"com.unity3d.${PRODUCT_NAME:rfc1034identifier}"')
    listed = "".join(f"\t\t\t\t{t},\n" for t in targets)
    body += (
        f"\t\tD00000000000000000000001 /* Project object */ = {{\n\t\t\tisa = PBXProject;\n"
        f"\t\t\ttargets = (\n{listed}\t\t\t);\n\t\t}};\n"
    )
    return "// !$*UTF8*$!\n{\n\tobjects = {\n" + body + "\t};\n\trootObject = D00000000000000000000001 /* Project object */;\n}\n"


def test_only_the_app_target_changes():
    before = project()
    after, old = link.set_bundle_id(before, "com.CG4002.unityTutorial.olson")
    assert old == ["com.CG4002.unityTutorial"]
    assert after.count("PRODUCT_BUNDLE_IDENTIFIER = com.CG4002.unityTutorial.olson;") == 2
    assert "PRODUCT_BUNDLE_IDENTIFIER = com.unity3d.framework;" in after
    assert 'PRODUCT_BUNDLE_IDENTIFIER = "com.unity3d.${PRODUCT_NAME:rfc1034identifier}";' in after
    assert after.replace("com.CG4002.unityTutorial.olson", "com.CG4002.unityTutorial") == before
    assert link.set_bundle_id(after, "com.CG4002.unityTutorial.olson") == (after, ["com.CG4002.unityTutorial.olson"])


def test_quoted_and_differing_identifiers_are_all_replaced():
    after, old = link.set_bundle_id(project(('"com.a.app"', "com.b.app")), "com.c.app")
    assert old == ["com.a.app", "com.b.app"]
    assert after.count("PRODUCT_BUNDLE_IDENTIFIER = com.c.app;") == 2


@pytest.mark.parametrize("bundle_id", ["", "app", "com.CG4002.unity Tutorial", "com.x;y", "com..x", "com.x.$(Y)"])
def test_rejects_a_value_that_is_not_a_bundle_identifier(bundle_id):
    with pytest.raises(ValueError, match="Not a bundle identifier"):
        link.set_bundle_id(project(), bundle_id)


def test_refuses_an_ambiguous_or_incomplete_app_target():
    with pytest.raises(ValueError, match="exactly one application target"):
        link.set_bundle_id(project(second_app=True), "com.c.app")
    with pytest.raises(ValueError, match="one PRODUCT_BUNDLE_IDENTIFIER"):
        link.set_bundle_id(project(("com.a.app", None)), "com.c.app")


def test_command_line_writes_the_export_and_reports_the_change(tmp_path):
    pbxproj = tmp_path / "Unity-iPhone.xcodeproj" / "project.pbxproj"
    pbxproj.parent.mkdir()
    pbxproj.write_text(project(), encoding="utf-8")
    run = subprocess.run([sys.executable, str(TOOL), "--export", str(tmp_path), "--bundle-id", "com.c.app"],
                         capture_output=True, text=True, check=True)
    assert run.stdout.strip() == "App bundle identifier: com.CG4002.unityTutorial -> com.c.app"
    assert pbxproj.read_text(encoding="utf-8").count("PRODUCT_BUNDLE_IDENTIFIER = com.c.app;") == 2
    written = pbxproj.read_text(encoding="utf-8")
    for value in ("bad id", ""):
        bad = subprocess.run([sys.executable, str(TOOL), "--export", str(tmp_path), "--bundle-id", value],
                             capture_output=True, text=True)
        assert bad.returncode == 1 and bad.stderr.startswith("COMMSNATIVE_LINK_FAILED: Not a bundle identifier")
    both = subprocess.run([sys.executable, str(TOOL), "--export", str(tmp_path), "--pin", "--bundle-id", "com.d.app"],
                          capture_output=True, text=True)
    assert both.returncode == 2 and "not allowed with argument" in both.stderr
    assert pbxproj.read_text(encoding="utf-8") == written
