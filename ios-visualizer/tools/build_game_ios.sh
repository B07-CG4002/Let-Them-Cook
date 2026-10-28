#!/bin/bash
# Build the AR game in vis/ for iOS with CommsNative linked, and optionally
# install and launch it on a connected iPhone. Run on a Mac from anywhere.
#
#   ios-visualizer/tools/build_game_ios.sh --team <team ID> [--device <id>] [--install] [--launch]
#       [--export <dir>] [--copy-setup] [--bundle-id <id>]
#
# --copy-setup (with --install) copies the public Connect setup from the comms
# test app with copy_phone_setup.sh, so only the passwords are typed.
# --bundle-id sets the app's bundle identifier in the export (link_commsnative.py
# --bundle-id), for a team that Apple will not register the export's own to
# because another team holds it. The export keeps the new identifier; it is a
# separate app on the iPhone, with its own data.
# --device takes the identifier or the UDID that `xcrun devicectl list devices`
# reports. COMMS_CONTENT_PACKAGES=<dir> supplies local package tarballs that the
# game's Packages/manifest.json names with "file:../ContentPackages/..." but
# that are not in Git (vis/ContentPackages is used when it exists).
#
# With --export, Unity is skipped and the given Xcode project (a folder holding
# Unity-iPhone.xcodeproj, for example one exported by the game's owner with
# Build Profiles > iOS > Build and unzipped on this Mac) is linked and built in
# place. Without it the script needs Unity 6000.5.10f1 with iOS Build Support (signed in to Unity Hub so the
# editor is licensed) and Xcode. Nothing under vis/ is written: Unity builds a
# copy of the game's tracked files in .comms-local/game-ios/vis, so untracked
# files in vis/ (such as Finder's " 2" duplicates) never reach the build.
# Steps: copy the project, Unity batch build to .comms-local/game-ios/xcode,
# link CommsNative (link_commsnative.py) and, with --bundle-id, set the app's
# identifier, pin the tested package versions, sign and build with xcodebuild,
# check that UnityFramework defines CommsNextResult, CommsHandState and
# CommsNextHandEvent, then install and launch with devicectl.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"
WORK="$ROOT/.comms-local/game-ios"
VERSION="$(sed -n 's/^m_EditorVersion: //p' "$ROOT/vis/ProjectSettings/ProjectVersion.txt")"
UNITY="${UNITY:-/Applications/Unity/Hub/Editor/$VERSION/Unity.app/Contents/MacOS/Unity}"
TEAM="${DEVELOPMENT_TEAM:-}"
DEVICE=""
INSTALL=0
LAUNCH=0
COPY_SETUP=0
SKIP_UNITY=0
BUNDLE_ID=""
EXPORT="$WORK/xcode"
# Xcode's output stays out of ~/Documents: there the file provider tags new
# bundles with Finder info, which codesign refuses ("detritus not allowed").
DERIVED="${COMMS_DERIVED_DATA:-$HOME/Library/Developer/Xcode/DerivedData/LetThemCook-game-ios}"

while [ $# -gt 0 ]; do
    case "$1" in
        --team) TEAM="$2"; shift 2 ;;
        --device) DEVICE="$2"; shift 2 ;;
        --install) INSTALL=1; shift ;;
        --launch) INSTALL=1; LAUNCH=1; shift ;;
        --copy-setup) COPY_SETUP=1; shift ;;
        --bundle-id) [ -n "${2:-}" ] || { echo "--bundle-id needs a bundle identifier" >&2; exit 2; }
                     BUNDLE_ID="$2"; shift 2 ;;
        --skip-unity) SKIP_UNITY=1; shift ;;  # relink and rebuild the last export only
        --export) EXPORT="$(cd "$2" && pwd -P)"; SKIP_UNITY=1; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$TEAM" ] || { echo "Give the Apple development team with --team or DEVELOPMENT_TEAM" >&2; exit 2; }
if [ "$INSTALL" = 1 ] && [ -z "$DEVICE" ]; then
    echo "Give the iPhone with --device (xcrun devicectl list devices)" >&2; exit 2
fi
mkdir -p "$WORK"
UDID=""
if [ -n "$DEVICE" ]; then
    # xcodebuild wants the hardware UDID; devicectl accepts either form.
    xcrun devicectl list devices --json-output "$WORK/devices.json" > /dev/null 2>&1 || true
    UDID="$(python3 -c 'import json, sys
for d in json.load(open(sys.argv[1]))["result"]["devices"]:
    udid = d["hardwareProperties"].get("udid", "")
    if sys.argv[2] in (d["identifier"], udid):
        print(udid)' "$WORK/devices.json" "$DEVICE" 2>/dev/null || true)"
    [ -n "$UDID" ] || { echo "No device $DEVICE in xcrun devicectl list devices" >&2; exit 1; }
fi
[ -f "$EXPORT/Unity-iPhone.xcodeproj/project.pbxproj" ] || [ "$SKIP_UNITY" = 0 ] ||
    { echo "No Unity-iPhone.xcodeproj in $EXPORT" >&2; exit 1; }

if [ "$SKIP_UNITY" = 0 ]; then
    [ -x "$UNITY" ] || { echo "Unity $VERSION not found at $UNITY (set UNITY=...)" >&2; exit 1; }

    # Fresh copy of the tracked project files; Library/ is kept as a cache.
    PROJECT="$WORK/vis"
    mkdir -p "$PROJECT"
    find "$PROJECT" -mindepth 1 -maxdepth 1 ! -name Library ! -name Logs ! -name UserSettings -exec rm -rf {} +
    git -C "$ROOT/vis" ls-files -z |
        rsync -a --from0 --files-from=- "$ROOT/vis/" "$PROJECT/"

    # Local tarballs the manifest needs but Git does not carry.
    CONTENT="${COMMS_CONTENT_PACKAGES:-$ROOT/vis/ContentPackages}"
    if [ -d "$CONTENT" ]; then
        mkdir -p "$PROJECT/ContentPackages"
        cp -R "$CONTENT"/. "$PROJECT/ContentPackages/"
    fi
    python3 - "$PROJECT" <<'PY' || exit 1
import json, os, sys
project = sys.argv[1]
packages = os.path.join(project, "Packages")
missing = [v for v in json.load(open(os.path.join(packages, "manifest.json")))["dependencies"].values()
           if v.startswith("file:") and not os.path.exists(os.path.normpath(os.path.join(packages, v[5:])))]
if missing:
    sys.exit("The game's Packages/manifest.json needs files that are not in Git: " + ", ".join(missing)
             + ". Get them from the game's owner and set COMMS_CONTENT_PACKAGES=<folder holding them>.")
PY

    # The batch build entry point lives only in the copy.
    mkdir -p "$PROJECT/Assets/Editor/CommsBuild"
    cat > "$PROJECT/Assets/Editor/CommsBuild/CommsIOSBuild.cs" <<'CS'
using System.Linq;
using UnityEditor;
using UnityEditor.Build.Reporting;

public static class CommsIOSBuild
{
    public static void Build()
    {
        var output = System.Environment.GetEnvironmentVariable("COMMS_IOS_EXPORT");
        var scenes = EditorBuildSettings.scenes.Where(s => s.enabled).Select(s => s.path).ToArray();
        var report = BuildPipeline.BuildPlayer(new BuildPlayerOptions
        {
            scenes = scenes,
            locationPathName = output,
            target = BuildTarget.iOS,
            targetGroup = BuildTargetGroup.iOS,
            options = BuildOptions.None,
        });
        UnityEngine.Debug.Log("CommsIOSBuild: " + report.summary.result + ", " + report.summary.totalErrors + " errors, scenes " + string.Join(", ", scenes));
        EditorApplication.Exit(report.summary.result == BuildResult.Succeeded ? 0 : 1);
    }
}
CS

    rm -rf "$EXPORT"
    echo "Unity batch build (log: $WORK/unity.log)"
    COMMS_IOS_EXPORT="$EXPORT" "$UNITY" -batchmode -nographics -projectPath "$PROJECT" \
        -buildTarget iOS -executeMethod CommsIOSBuild.Build -logFile "$WORK/unity.log" ||
        { echo "Unity build failed; see $WORK/unity.log" >&2; exit 1; }
fi

if [ "$EXPORT" != "$WORK/xcode" ]; then
    # A zipped export carries extended attributes (quarantine, Finder info)
    # that codesign rejects as "detritus", and can lose executable bits.
    xattr -cr "$EXPORT" 2>/dev/null || true
    find "$EXPORT" -name .DS_Store -delete
    find "$EXPORT" -maxdepth 1 \( -name '*.sh' -o -name 'usymtool*' \) -exec chmod +x {} +
    find "$EXPORT/Il2CppOutputProject" -type f ! -perm -u+x -size +1k -print0 2>/dev/null |
        xargs -0 file 2>/dev/null | sed -n 's/: *Mach-O.*//p' | while IFS= read -r binary; do chmod +x "$binary"; done
fi
python3 "$ROOT/ios-visualizer/tools/link_commsnative.py" --export "$EXPORT"
if [ -n "$BUNDLE_ID" ]; then
    python3 "$ROOT/ios-visualizer/tools/link_commsnative.py" --export "$EXPORT" --bundle-id "$BUNDLE_ID"
fi
XCODE=(xcodebuild -project "$EXPORT/Unity-iPhone.xcodeproj" -scheme Unity-iPhone -configuration Release
       -derivedDataPath "$DERIVED" -clonedSourcePackagesDirPath "$DERIVED/SourcePackages")
resolve() {
    "${XCODE[@]}" -resolvePackageDependencies > "$WORK/resolve.log" 2>&1 ||
        { tail -20 "$WORK/resolve.log" >&2; echo "Package resolution failed; see $WORK/resolve.log" >&2; exit 1; }
}
resolve
python3 "$ROOT/ios-visualizer/tools/link_commsnative.py" --export "$EXPORT" --pin
resolve
python3 "$ROOT/ios-visualizer/tools/link_commsnative.py" --export "$EXPORT" --pin

DESTINATION="generic/platform=iOS"
[ -n "$UDID" ] && DESTINATION="id=$UDID"
echo "xcodebuild (log: $WORK/xcodebuild.log)"
"${XCODE[@]}" -destination "$DESTINATION" -allowProvisioningUpdates \
    CODE_SIGN_STYLE=Automatic DEVELOPMENT_TEAM="$TEAM" build > "$WORK/xcodebuild.log" 2>&1 ||
    { tail -30 "$WORK/xcodebuild.log" >&2; echo "xcodebuild failed; see $WORK/xcodebuild.log" >&2; exit 1; }

APP="$DERIVED/Build/Products/Release-iphoneos/$(sed -n 's/^  productName: //p' "$ROOT/vis/ProjectSettings/ProjectSettings.asset").app"
[ -d "$APP" ] || APP="$(find "$DERIVED/Build/Products/Release-iphoneos" -maxdepth 1 -name '*.app' | head -1)"
echo "Built $APP"
nm -gU "$APP/Frameworks/UnityFramework.framework/UnityFramework" > "$WORK/symbols.txt"
for symbol in CommsNextResult CommsHandState CommsNextHandEvent; do
    if ! grep -q " _$symbol\$" "$WORK/symbols.txt"; then
        echo "UnityFramework does not define $symbol; CommsNative is not linked or is older" >&2; exit 1
    fi
done

if [ "$INSTALL" = 1 ]; then
    xcrun devicectl device install app --device "$DEVICE" "$APP"
fi
if [ "$INSTALL" = 1 ] && [ "$COPY_SETUP" = 1 ]; then
    BUNDLE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Info.plist")"
    "$ROOT/ios-visualizer/tools/copy_phone_setup.sh" --device "$DEVICE" --to "$BUNDLE"
fi
if [ "$LAUNCH" = 1 ]; then
    BUNDLE="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Info.plist")"
    xcrun devicectl device process launch --device "$DEVICE" --terminate-existing "$BUNDLE"
fi
