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
cleanup() { pkill -u "$(id -u)" -f '[u]ltra96.server' 2>/dev/null; pkill -u "$(id -u)" -f '[h]ttp.server 8888' 2>/dev/null; pkill -u "$(id -u)" -f '[m]odel_runner_example' 2>/dev/null; sleep 0.5; }

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
check "a v3 deploy starts the service without --sensor-version" '! tr "\0" " " < /proc/$NEWPID/cmdline | grep -q -- "--sensor-version"'
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

# --- 9. the bridge with a model runner (started directly: "sudo": false, so no password prompt) ---
cleanup
mkdir -p "$BASE/runner"; cp "$SRC/new/ultra96/model_runner_example.py" "$BASE/runner/"
runner_opts() {  # $1 script
  printf '{"event_log": false, "model": "ultra96.model_bridge", "model_path": null, "session_id": "ltc-comms", "start_timeout": 30, "runner": {"script": "%s", "python": "/usr/bin/python3", "args": ["--socket", "%s/model-runner.sock", "--every", "1"], "sudo": false}}' "$1" "$BASE"
}
newsrc source-co-v3-T7
echo "not-a-password" | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T7" T7 "$(runner_opts "$BASE/runner/model_runner_example.py")" > "$BASE/out7.json" 2> "$BASE/err7.txt"; code=$?
check "runner: deployed, exit 0" '[ $code -eq 0 ] && grep -q "\"status\": \"deployed\"" "$BASE/out7.json"'
check "runner: running and connected" 'tail -1 "$BASE/out7.json" | grep -q "\"status\": \"running\", \"connected\": true"'
RUN1=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$BASE/evidence/model-runner.json")
check "runner: alive in its own session" 'kill -0 "$RUN1" 2>/dev/null && [ "$(ps -o sid= -p "$RUN1" | tr -d " ")" = "$RUN1" ]'
check "runner: the password never reached the runner" '! grep -q not-a-password "$BASE/evidence/model-runner-T7.log"'
(cd "$BASE/source-co-v3-T7" && /usr/bin/python3 - "$SRC/pki-a/ca-cert.pem" > "$BASE/e2e7.txt" 2>&1 <<'PY'
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
    await write_frame(iw, SensorPacket(1, 5, 0, 0, DUMMY_FIXTURES[0]).to_message("ltc-comms"))
    validate_message(await read_frame(ir), "INGEST_ACK", "ltc-comms")
    res = validate_message(await asyncio.wait_for(read_frame(pr), 5), "GESTURE_RESULT", "ltc-comms")
    print("result", res["result_id"], res["gesture"])
    print("E2E_OK")
asyncio.run(main())
PY
)
check "runner: a sample's result comes back through the runner" 'grep -q "result 1:5:0 CHOP" "$BASE/e2e7.txt"'

# --- 10. redeploy with a runner: the previous runner is replaced ---
newsrc source-co-v3-T8
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T8" T8 "$(runner_opts "$BASE/runner/model_runner_example.py")" > "$BASE/out8.json" 2> "$BASE/err8.txt"; code=$?
RUN2=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$BASE/evidence/model-runner.json")
check "runner redeploy: exit 0, previous runner stopped" '[ $code -eq 0 ] && tail -1 "$BASE/out8.json" | grep -q "\"previous\": \"sigterm\""'
check "runner redeploy: old runner gone, new one alive" '! kill -0 "$RUN1" 2>/dev/null && kill -0 "$RUN2" 2>/dev/null && [ "$RUN1" != "$RUN2" ]'

# --- 11. a runner script that does not exist: the service stays deployed, exit 3 ---
newsrc source-co-v3-T9
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T9" T9 "$(runner_opts "$BASE/runner/missing.py")" > "$BASE/out9.json" 2> "$BASE/err9.txt"; code=$?
SVC9=$(cat "$BASE/evidence/server.pid")
check "missing runner: exit 3, service deployed and alive" '[ $code -eq 3 ] && grep -q "\"status\": \"deployed\"" "$BASE/out9.json" && kill -0 "$SVC9" 2>/dev/null'
check "missing runner: reported as failed" 'tail -1 "$BASE/out9.json" | grep -q "runner script not found"'
check "missing runner: the previous runner is untouched, alive and named" 'tail -1 "$BASE/out9.json" | grep -q "\"previous\": \"untouched\", \"previous_running\": $RUN2" && kill -0 "$RUN2" 2>/dev/null'

# --- 12. a runner started by hand next to --runner: the new runner is stopped, status conflict ---
(cd "$BASE/runner" && setsid /usr/bin/python3 model_runner_example.py --socket "$BASE/model-runner.sock" --every 1 > "$BASE/hand-runner.log" 2>&1 < /dev/null &)
sleep 2
HAND=$(pgrep -u "$(id -u)" -f '[m]odel_runner_example.py --socket' | while read -r p; do [ "$p" != "$RUN2" ] && echo "$p"; done | head -1)
newsrc source-co-v3-T10
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T10" T10 "$(runner_opts "$BASE/runner/model_runner_example.py")" > "$BASE/out10.json" 2> "$BASE/err10.txt"; code=$?
RUN3=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$BASE/evidence/model-runner.json")
check "hand-started runner: exit 3 with status conflict" '[ $code -eq 3 ] && tail -1 "$BASE/out10.json" | grep -q "\"status\": \"conflict\""'
check "hand-started runner: the recorded runner replaced, the new one stopped again" '! kill -0 "$RUN2" 2>/dev/null && ! kill -0 "$RUN3" 2>/dev/null'
check "hand-started runner: left running (not this tool's)" '[ -n "$HAND" ] && kill -0 "$HAND" 2>/dev/null'

# --- 13. a runner started as a module (python -m src.worker) from its own folder, as the model owner does ---
pkill -u "$(id -u)" -f '[m]odel_runner_example.py --socket' 2>/dev/null; sleep 0.5
mkdir -p "$BASE/ai/src"; cp "$SRC/new/ultra96/model_runner_example.py" "$BASE/ai/src/worker.py"
module_opts() {  # $1 module, $2 folder
  printf '{"event_log": false, "model": "ultra96.model_bridge", "model_path": null, "session_id": "ltc-comms", "start_timeout": 30, "runner": {"module": "%s", "cwd": "%s", "python": "/usr/bin/python3", "args": ["--socket", "%s/model-runner.sock", "--every", "1"], "sudo": false}}' "$1" "$2" "$BASE"
}
newsrc source-co-v3-T11
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T11" T11 "$(module_opts src.worker "$BASE/ai")" > "$BASE/out11.json" 2> "$BASE/err11.txt"; code=$?
RUN4=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$BASE/evidence/model-runner.json")
check "module runner: exit 0, running and connected" '[ $code -eq 0 ] && tail -1 "$BASE/out11.json" | grep -q "\"status\": \"running\", \"connected\": true"'
check "module runner: python -m src.worker in its folder" 'tr "\0" " " < /proc/$RUN4/cmdline | grep -q -- "-m src.worker --socket" && [ "$(readlink /proc/$RUN4/cwd)" = "$BASE/ai" ]'
newsrc source-co-v3-T12
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T12" T12 "$(module_opts src.missing "$BASE/ai")" > "$BASE/out12.json" 2> "$BASE/err12.txt"; code=$?
check "missing module: exit 3, reported, the previous runner untouched" '[ $code -eq 3 ] && tail -1 "$BASE/out12.json" | grep -q "runner module not found" && kill -0 "$RUN4" 2>/dev/null'
check "missing module: the service is deployed" 'grep -q "\"status\": \"deployed\"" "$BASE/out12.json"'
pkill -u "$(id -u)" -f '[s]rc.worker --socket' 2>/dev/null

# --- 14. a runner started by hand, named with --replace-runner PID:TICKS: stopped before the new one starts ---
ticks() { awk '{sub(/.*[)] /, ""); print $20}' "/proc/$1/stat"; }  # /proc/PID/stat field 22
replace_opts() {  # $1 the launcher's replace list (JSON)
  printf '{"event_log": false, "model": "ultra96.model_bridge", "model_path": null, "session_id": "ltc-comms", "start_timeout": 30, "runner": {"script": "%s", "python": "/usr/bin/python3", "args": ["--socket", "%s/model-runner.sock", "--every", "1"], "sudo": false, "replace": %s}}' "$BASE/runner/model_runner_example.py" "$BASE" "$1"
}
replaced() {  # $1 output file: each replaced runner as PID:TICKS:STOP
  tail -1 "$1" | /usr/bin/python3 -c 'import json, sys; r = json.loads(sys.stdin.read())["runner"]; print(" ".join("%s:%s:%s" % (i["pid"], i["start_ticks"], i["stop"]) for i in r.get("replaced", [])))'
}
recorded() { /usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["pid"])' "$BASE/evidence/model-runner.json"; }
(cd "$BASE/runner" && setsid /usr/bin/python3 model_runner_example.py --socket "$BASE/model-runner.sock" --every 1 > "$BASE/hand-runner2.log" 2>&1 < /dev/null &)
sleep 2
HAND2=$(pgrep -u "$(id -u)" -f '[m]odel_runner_example.py --socket' | head -1)
HAND2_TICKS=$(ticks "$HAND2")
newsrc source-co-v3-T13
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T13" T13 "$(replace_opts "[{\"pid\": $HAND2, \"start_ticks\": \"$HAND2_TICKS\"}]")" > "$BASE/out13.json" 2> "$BASE/err13.txt"; code=$?
RUN5=$(recorded)
check "replace-runner: exit 0, the new runner running and connected" '[ $code -eq 0 ] && tail -1 "$BASE/out13.json" | grep -q "\"status\": \"running\", \"connected\": true"'
check "replace-runner: the hand-started runner stopped with SIGTERM" '[ -n "$HAND2_TICKS" ] && [ "$(replaced "$BASE/out13.json")" = "$HAND2:$HAND2_TICKS:sigterm" ] && ! kill -0 "$HAND2" 2>/dev/null'
check "replace-runner: the new runner alive and recorded" '[ "$RUN5" != "$HAND2" ] && kill -0 "$RUN5" 2>/dev/null'

# --- 15. --replace-runner naming a process that is not a model runner: refused, nothing stopped or started ---
(setsid sleep 300 > /dev/null 2>&1 < /dev/null &)
sleep 0.5
SLEEP=$(pgrep -u "$(id -u)" -f '^sleep 300$' | head -1)
newsrc source-co-v3-T14
echo x | /usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T14" T14 "$(replace_opts "[{\"pid\": $SLEEP, \"start_ticks\": \"$(ticks "$SLEEP")\"}]")" > "$BASE/out14.json" 2> "$BASE/err14.txt"; code=$?
check "refused replace-runner: exit 3, refused, the previous runner untouched and named" '[ $code -eq 3 ] && tail -1 "$BASE/out14.json" | grep -q "refused, it is not a Python program" && tail -1 "$BASE/out14.json" | grep -q "\"previous\": \"untouched\", \"previous_running\": $RUN5"'
check "refused replace-runner: nothing was signalled" 'kill -0 "$SLEEP" 2>/dev/null && kill -0 "$RUN5" 2>/dev/null'
check "refused replace-runner: no new runner was started" '[ "$(recorded)" = "$RUN5" ]'
kill "$SLEEP" 2>/dev/null
pkill -u "$(id -u)" -f '[m]odel_runner_example.py --socket' 2>/dev/null; sleep 0.5

# --- 16. an opt-in v4 deploy: --sensor-version 4 in argv; INGEST_ACK at v=4, GESTURE_RESULT at v=3 ---
V4OPTS='{"event_log": true, "model": null, "model_path": null, "sensor_version": 4, "session_id": "ltc-comms", "start_timeout": 30}'
newsrc source-co-v4-T15
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v4-T15" T15 "$V4OPTS" > "$BASE/out15.json" 2> "$BASE/err15.txt"; code=$?
V4PID=$(cat "$BASE/evidence/server.pid")
check "v4 deploy: exit 0, deployed from the new source" '[ $code -eq 0 ] && grep -q "\"status\": \"deployed\"" "$BASE/out15.json" && [ "$(readlink /proc/$V4PID/cwd)" = "$BASE/source-co-v4-T15" ]'
check "v4 deploy: argv ends with --sensor-version 4" 'tr "\0" " " < /proc/$V4PID/cmdline | grep -q -- "--sensor-version 4 $"'
(cd "$BASE/source-co-v4-T15" && /usr/bin/python3 - "$SRC/pki-a/ca-cert.pem" > "$BASE/e2e15.txt" 2>&1 <<'PY'
import asyncio, sys
from common.tls import client_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame
from common.sensor_v4 import SensorPacket, DUMMY_FIXTURES
from ultra96.protocol import validate_message
ca = sys.argv[1]
async def main():
    pr, pw = await asyncio.open_connection("127.0.0.1", 9999, ssl=client_context(ca), server_hostname=TLS_SERVER_NAME)
    await write_frame(pw, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
    validate_message(await read_frame(pr), "SUBSCRIBED", "ltc-comms")
    ir, iw = await asyncio.open_connection("127.0.0.1", 8888, ssl=client_context(ca), server_hostname=TLS_SERVER_NAME)
    for seq in range(3):
        await write_frame(iw, SensorPacket(1, 78, seq, seq * 20, DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)]).to_message("ltc-comms"))
        ack = validate_message(await read_frame(ir), "INGEST_ACK", "ltc-comms", sensor_version=4)
        res = validate_message(await read_frame(pr), "GESTURE_RESULT", "ltc-comms", sensor_version=4)
        assert ack["v"] == 4 and res["v"] == 3, (ack, res)
        print("ack", ack["v"], ack["status"], "result", res["v"], res["result_id"], res["gesture"])
    print("E2E_V4_OK")
asyncio.run(main())
PY
)
check "v4 end to end: INGEST_ACK v=4, GESTURE_RESULT v=3" 'grep -q E2E_V4_OK "$BASE/e2e15.txt"'

# --- 17. a broken v3 deploy over the v4 service: rolled back with the v4 service's own argv ---
newsrc source-co-v3-T16
sed -i 's/^def main():/def main():\n    raise SystemExit(3)/' "$BASE/source-co-v3-T16/ultra96/server.py"
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v3-T16" T16 "$OPTS" > "$BASE/out16.json" 2> "$BASE/err16.txt"; code=$?
RB16=$(cat "$BASE/evidence/server.pid")
check "v4 rollback: rolled back, still --sensor-version 4" '[ $code -ne 0 ] && grep -q "\"status\": \"rolled_back\"" "$BASE/out16.json" && tr "\0" " " < /proc/$RB16/cmdline | grep -q -- "--sensor-version 4"'

# --- 18. an opt-in v5 deploy over the v4 service: --sensor-version 5; INGEST_ACK v=5, results v=3 ---
V5OPTS='{"event_log": true, "model": null, "model_path": null, "sensor_version": 5, "session_id": "ltc-comms", "start_timeout": 30}'
newsrc source-co-v5-T17
/usr/bin/python3 -c "$PROG" "$BASE" "$BASE/source-co-v5-T17" T17 "$V5OPTS" > "$BASE/out17.json" 2> "$BASE/err17.txt"; code=$?
V5PID=$(cat "$BASE/evidence/server.pid")
check "v5 deploy: exit 0, deployed from the new source" '[ $code -eq 0 ] && grep -q "\"status\": \"deployed\"" "$BASE/out17.json" && [ "$(readlink /proc/$V5PID/cwd)" = "$BASE/source-co-v5-T17" ]'
check "v5 deploy: argv ends with --sensor-version 5" 'tr "\0" " " < /proc/$V5PID/cmdline | grep -q -- "--sensor-version 5 $"'
(cd "$BASE/source-co-v5-T17" && /usr/bin/python3 - "$SRC/pki-a/ca-cert.pem" > "$BASE/e2e17.txt" 2>&1 <<'PY'
import asyncio, sys
from common.tls import client_context, TLS_SERVER_NAME
from common.wire import read_frame, write_frame
from common.sensor_v5 import SensorPacket, DUMMY_FIXTURES
from ultra96.protocol import validate_message
ca = sys.argv[1]
async def main():
    pr, pw = await asyncio.open_connection("127.0.0.1", 9999, ssl=client_context(ca), server_hostname=TLS_SERVER_NAME)
    await write_frame(pw, dict(v=1, type="SUBSCRIBE", session_id="ltc-comms"))
    validate_message(await read_frame(pr), "SUBSCRIBED", "ltc-comms")
    ir, iw = await asyncio.open_connection("127.0.0.1", 8888, ssl=client_context(ca), server_hostname=TLS_SERVER_NAME)
    for seq in range(3):
        await write_frame(iw, SensorPacket(1, 79, seq, seq * 20, DUMMY_FIXTURES[seq % len(DUMMY_FIXTURES)]).to_message("ltc-comms"))
        ack = validate_message(await read_frame(ir), "INGEST_ACK", "ltc-comms", sensor_version=5)
        res = validate_message(await read_frame(pr), "GESTURE_RESULT", "ltc-comms", sensor_version=5)
        assert ack["v"] == 5 and res["v"] == 3, (ack, res)
        print("ack", ack["v"], ack["status"], "result", res["v"], res["result_id"], res["gesture"])
    print("E2E_V5_OK")
asyncio.run(main())
PY
)
check "v5 end to end: INGEST_ACK v=5, GESTURE_RESULT v=3" 'grep -q E2E_V5_OK "$BASE/e2e17.txt"'

cleanup
echo "RESULT pass=$pass fail=$fail"
for f in err1 err2 err3 err4 err5 err6 err7 err8 err9 err10 err11 err12 err13 err14 err15 err16 err17; do echo "--- $f"; tail -3 "$BASE/$f.txt"; done
echo "--- e2e"; cat "$BASE/e2e.txt"; echo "--- e2e7"; cat "$BASE/e2e7.txt"; echo "--- e2e15"; cat "$BASE/e2e15.txt"; echo "--- e2e17"; cat "$BASE/e2e17.txt"
rm -rf "$BASE"
[ "$fail" -eq 0 ] && [ "$pass" -eq 58 ]
