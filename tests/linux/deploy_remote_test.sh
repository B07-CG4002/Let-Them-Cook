#!/usr/bin/env bash
# Exercise tools/deploy_service.py REMOTE_PROGRAM on Linux (WSL; the Ultra96 runs Python 3.10).
# Run through tests/test_deploy_service.py (B07_LINUX_DEPLOY_TEST=1). It stops this user's
# ultra96.server processes inside Linux, so never run it on the Ultra96 itself.
set -u
SRC="$1"  # material folder prepared by tests/test_deploy_service.py
BASE=/tmp/b07-deploy-test-$$
PROG="$(cat "$SRC/remote_program.py")"
OPTS='{"event_log": true, "model": null, "model_path": null, "session_id": "ltc-comms", "start_timeout": 30}'
pass=0; fail=0
check() { if eval "$2"; then echo "PASS: $1"; pass=$((pass+1)); else echo "FAIL: $1"; fail=$((fail+1)); fi; }
cleanup() { pkill -u "$(id -u)" -f '[u]ltra96.server' 2>/dev/null; pkill -u "$(id -u)" -f '[h]ttp.server 8888' 2>/dev/null; sleep 0.5; }

cleanup; rm -rf "$BASE"; mkdir -p "$BASE/tls" "$BASE/evidence"
cp "$SRC/pki-a/server-cert.pem" "$SRC/pki-a/server-key.pem" "$BASE/tls/"; chmod 600 "$BASE/tls/server-key.pem"
newsrc() {  # $1 folder name; copies the v3 working-tree source
  mkdir -p "$BASE/$1"; cp -r "$SRC/new/common" "$SRC/new/ultra96" "$BASE/$1/"
}

# --- 1. old (version 2) service running in the background, as on the board ---
mkdir -p "$BASE/source-old"; cp -r "$SRC/old/common" "$SRC/old/ultra96" "$BASE/source-old/"
(cd "$BASE/source-old" && setsid /usr/bin/python3 -u -m ultra96.server --cert "$BASE/tls/server-cert.pem" \
   --key "$BASE/tls/server-key.pem" --session-id week7-demo --event-log-dir "$BASE/evidence/observer-old" \
   > "$BASE/evidence/old.log" 2>&1 < /dev/null &)
sleep 2
OLDPID=$(pgrep -u "$(id -u)" -f '[u]ltra96.server' | head -1)
check "old service started" '[ -n "$OLDPID" ]'

# --- 2. deploy v3 with a renewed certificate ---
newsrc source-co-v3-T1; mkdir -p "$BASE/source-co-v3-T1/tls-incoming"
cp "$SRC/pki-b/server-cert.pem" "$SRC/pki-b/server-key.pem" "$BASE/source-co-v3-T1/tls-incoming/"
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T1" T1 "$OPTS" > "$BASE/out1.json" 2> "$BASE/err1.txt"; code=$?
check "deploy exit 0" '[ $code -eq 0 ]'
check "status deployed" 'grep -q "\"status\": \"deployed\"" "$BASE/out1.json"'
check "old process gone" '! kill -0 "$OLDPID" 2>/dev/null'
NEWPID=$(cat "$BASE/evidence/server.pid")
check "new process alive in new source" '[ "$(readlink /proc/$NEWPID/cwd)" = "$BASE/source-co-v3-T1" ]'
check "new certificate installed" 'cmp -s "$BASE/tls/server-cert.pem" "$SRC/pki-b/server-cert.pem"'
check "key mode 600" '[ "$(stat -c %a "$BASE/tls/server-key.pem")" = 600 ]'
check "old certificate backed up" 'cmp -s "$BASE/tls/backup-T1/server-cert.pem" "$SRC/pki-a/server-cert.pem"'
check "incoming folder removed" '[ ! -e "$BASE/source-co-v3-T1/tls-incoming" ]'
check "rollback copy made" '[ -f "$BASE/rollback-co-T1/ultra96/server.py" ]'
check "session ltc-comms in argv" 'tr "\0" " " < /proc/$NEWPID/cmdline | grep -q "session-id ltc-comms"'
check "model_ready printed" 'grep -q model_ready "$BASE/evidence/server-co-T1.log"'

# --- 3. the deployed service speaks version 3 end to end (TLS, ACK, dummy-model result) ---
(cd "$BASE/source-co-v3-T1" && /usr/bin/python3 - "$SRC/pki-a/ca-cert.pem" > "$BASE/e2e.txt" 2>&1 <<'PY'
import asyncio, sys
from common.tls import client_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame
from common.sensor import SensorPacket, DUMMY_FIXTURES
from ultra96.protocol import validate_message
ca = sys.argv[1]
async def main():
    pr, pw = await asyncio.open_connection("127.0.0.1", 9999, ssl=client_context(ca), server_hostname=TLS_SERVER_NAME)
    await write_frame(pw, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
    validate_message(await read_frame(pr), "SUBSCRIBED", "ltc-comms")
    ir, iw = await asyncio.open_connection("127.0.0.1", 8888, ssl=client_context(ca), server_hostname=TLS_SERVER_NAME)
    for seq in range(5):
        await write_frame(iw, SensorPacket(2, 77, seq, seq * 20, DUMMY_FIXTURES[seq % 4]).to_message("ltc-comms"))
        ack = validate_message(await read_frame(ir), "INGEST_ACK", "ltc-comms")
        res = validate_message(await read_frame(pr), "GESTURE_RESULT", "ltc-comms")
        print("ack", ack["v"], ack["status"], "result", res["result_id"], res["gesture"], res["confidence"])
    print("E2E_OK")
asyncio.run(main())
PY
)
check "v3 end to end through the deployed service" 'grep -q E2E_OK "$BASE/e2e.txt"'

# --- 4. broken new source: rolled back to the running v3 service, certificate restored ---
newsrc source-co-v3-T2; mkdir -p "$BASE/source-co-v3-T2/tls-incoming"
cp "$SRC/pki-a/server-cert.pem" "$SRC/pki-a/server-key.pem" "$BASE/source-co-v3-T2/tls-incoming/"
sed -i 's/^def main():/def main():\n    raise SystemExit(3)/' "$BASE/source-co-v3-T2/ultra96/server.py"
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T2" T2 "$OPTS" > "$BASE/out2.json" 2> "$BASE/err2.txt"; code=$?
check "broken deploy exits nonzero" '[ $code -ne 0 ]'
check "status rolled_back" 'grep -q "\"status\": \"rolled_back\"" "$BASE/out2.json"'
RBPID=$(cat "$BASE/evidence/server.pid")
check "previous service restarted from rollback copy" '[ "$(readlink /proc/$RBPID/cwd)" = "$BASE/rollback-co-T2" ]'
check "certificate restored after rollback" 'cmp -s "$BASE/tls/server-cert.pem" "$SRC/pki-b/server-cert.pem"'
check "rolled-back service listens on 8888 and 9999" '[ "$(ss -ltnH 2>/dev/null | grep -c -E "127\.0\.0\.1:(8888|9999) ")" -eq 2 ]'

# --- 5. mismatched certificate and key: refused before anything stops ---
newsrc source-co-v3-T3; mkdir -p "$BASE/source-co-v3-T3/tls-incoming"
cp "$SRC/pki-a/server-cert.pem" "$BASE/source-co-v3-T3/tls-incoming/server-cert.pem"
cp "$SRC/pki-b/server-key.pem" "$BASE/source-co-v3-T3/tls-incoming/server-key.pem"
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T3" T3 "$OPTS" > "$BASE/out3.json" 2> "$BASE/err3.txt"; code=$?
check "mismatched pair refused" '[ $code -ne 0 ]'
check "running service untouched" 'kill -0 "$RBPID" 2>/dev/null'
check "certificate unchanged" 'cmp -s "$BASE/tls/server-cert.pem" "$SRC/pki-b/server-cert.pem"'

# --- 6. nothing running, ports busy by another program: refused ---
cleanup
(cd /tmp && setsid /usr/bin/python3 -m http.server 8888 --bind 127.0.0.1 > /dev/null 2>&1 < /dev/null &)
sleep 1
newsrc source-co-v3-T4
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T4" T4 "$OPTS" > "$BASE/out4.json" 2> "$BASE/err4.txt"; code=$?
check "busy port refused" '[ $code -ne 0 ] && grep -q "in use by another process" "$BASE/err4.txt"'
check "nothing started" '! pgrep -u "$(id -u)" -f "[u]ltra96.server" >/dev/null'
pkill -u "$(id -u)" -f '[h]ttp.server 8888'; sleep 0.5

# --- 7. nothing running, ports free: first start ---
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T4" T5 "$OPTS" > "$BASE/out5.json" 2> "$BASE/err5.txt"; code=$?
check "first start deployed" '[ $code -eq 0 ] && grep -q "\"status\": \"deployed\"" "$BASE/out5.json" && grep -q "\"previous\": null" "$BASE/out5.json"'

# --- 8. an old service that closes its ports on SIGTERM but never exits: SIGKILL ---
cleanup
mkdir -p "$BASE/source-stubborn/ultra96" "$BASE/source-stubborn/common"
touch "$BASE/source-stubborn/ultra96/__init__.py" "$BASE/source-stubborn/common/__init__.py"
cat > "$BASE/source-stubborn/ultra96/server.py" <<'PY'
import signal, socket, time
listeners = []
for port in (8888, 9999):
    sock = socket.socket(); sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port)); sock.listen(); listeners.append(sock)
def term(*_):
    for sock in listeners: sock.close()   # ports released, process lingers like a stuck model thread
signal.signal(signal.SIGTERM, term)
while True: time.sleep(1)
PY
(cd "$BASE/source-stubborn" && setsid /usr/bin/python3 -u -m ultra96.server --cert x --key y \
   > "$BASE/evidence/stubborn.log" 2>&1 < /dev/null &)
sleep 1.5
STUBPID=$(pgrep -u "$(id -u)" -f '[u]ltra96.server' | head -1)
newsrc source-co-v3-T6
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T6" T6 "$OPTS" > "$BASE/out6.json" 2> "$BASE/err6.txt"; code=$?
check "stubborn old service: deployed" '[ $code -eq 0 ] && grep -q "\"status\": \"deployed\"" "$BASE/out6.json"'
check "stubborn old service: stopped with SIGKILL" 'grep -q "\"old_stop\": \"sigkill\"" "$BASE/out6.json"'
check "stubborn old service: process gone" '! kill -0 "$STUBPID" 2>/dev/null'

cleanup
echo "RESULT pass=$pass fail=$fail"
for f in err1 err2 err3 err4 err5 err6; do echo "--- $f"; tail -3 "$BASE/$f.txt"; done
echo "--- e2e"; cat "$BASE/e2e.txt"
rm -rf "$BASE"
[ "$fail" -eq 0 ] && [ "$pass" -eq 27 ]
