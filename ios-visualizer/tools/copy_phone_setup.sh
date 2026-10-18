#!/bin/bash
# Copy the public Communications setup (the two user names, the jump-host
# switch and the verified CA) from one installed app on the iPhone to another,
# so the new app's Connect form opens filled in and only the passwords are
# typed. Passwords are never stored on the phone, so none is copied.
#
#   ios-visualizer/tools/copy_phone_setup.sh --device <id> [--from <bundle id>] [--to <bundle id>]
#
# Defaults: from the Week 7 / comms test app (com.CookingCompany.unityTutorial)
# to the game (com.CG4002.unityTutorial). Both must be development-signed
# builds installed on the device. The source may keep the files in
# Application Support/Comms or in the older Application Support/Week7.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd -P)"
DEVICE=""
FROM="com.CookingCompany.unityTutorial"
TO="com.CG4002.unityTutorial"
while [ $# -gt 0 ]; do
    case "$1" in
        --device) DEVICE="$2"; shift 2 ;;
        --from) FROM="$2"; shift 2 ;;
        --to) TO="$2"; shift 2 ;;
        *) echo "Unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -n "$DEVICE" ] || { echo "Give the iPhone with --device (xcrun devicectl list devices)" >&2; exit 2; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
container() {  # container <copy from|copy to> <bundle> <args...>
    local verb="$1" bundle="$2"; shift 2
    xcrun devicectl device copy "$verb" --device "$DEVICE" --domain-type appDataContainer \
        --domain-identifier "$bundle" "$@" > "$TMP/devicectl.log" 2>&1
}

found=""
for pair in "Comms public-settings.json comms-ca.pem" "Week7 public-settings.json week7-ca.pem"; do
    set -- $pair
    if container from "$FROM" --source "Library/Application Support/$1/$2" --destination "$TMP/settings.json" &&
       container from "$FROM" --source "Library/Application Support/$1/$3" --destination "$TMP/ca.pem"; then
        found="$1"; break
    fi
done
[ -n "$found" ] || { echo "No saved Communications setup in $FROM on the device" >&2; exit 1; }

# Only the public fields, and only the enrolled CA.
python3 - "$TMP/settings.json" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
if set(data) - {"boardUser", "jumpUser", "useJump"}:
    sys.exit("Unexpected fields in public-settings.json: " + ", ".join(sorted(set(data))))
PY
ENROLLED="$(sed -n 's/.*static let caSHA256 = "\([0-9a-f]*\)".*/\1/p' "$ROOT/ios-visualizer/CommsNative/Sources/CommsBridge/Configuration.swift")"
ACTUAL="$(openssl x509 -in "$TMP/ca.pem" -outform der | shasum -a 256 | cut -d' ' -f1)"
[ "$ACTUAL" = "$ENROLLED" ] || { echo "The CA in $FROM is not the enrolled Communications CA" >&2; exit 1; }

# devicectl creates every folder on a destination path as root (mode 0755),
# and the app then cannot save into it ("NSCocoaErrorDomain 513"). Folders
# inside a copied source folder keep the app's owner, so send Comms/ inside a
# folder copied over Application Support: only Application Support itself is
# root-owned, and the app can still write in Comms/. This replaces the target
# app's Application Support.
mkdir -p "$TMP/support/Comms"
cp "$TMP/settings.json" "$TMP/support/Comms/public-settings.json"
cp "$TMP/ca.pem" "$TMP/support/Comms/comms-ca.pem"
container to "$TO" --source "$TMP/support" --destination "Library/Application Support" --remove-existing-content true ||
    { cat "$TMP/devicectl.log" >&2; exit 1; }
xcrun devicectl device info files --device "$DEVICE" --domain-type appDataContainer --domain-identifier "$TO" \
    --subdirectory Library --json-output "$TMP/files.json" > /dev/null 2>&1
python3 - "$TMP/files.json" <<'PY'
import json, sys
files = {f["relativePath"]: f["metadata"] for f in json.load(open(sys.argv[1]))["result"]["files"]}
comms = files.get("Application Support/Comms")
if not comms or comms["ownerUid"] == 0:
    sys.exit("Application Support/Comms on the phone is missing or owned by root; the app could not save there")
PY
echo "Copied the public setup (Application Support/$found) from $FROM to $TO; enter the passwords in the app"
