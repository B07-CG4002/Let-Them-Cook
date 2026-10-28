"""Foreground Ultra96 status/start operations for the existing demo CLI."""
import json
from pathlib import Path
import re
import shlex
import subprocess

from tools.ssh_tunnel import default_jump, tunnel_command
from ultra96.protocol import validate_session


ROOT = Path(__file__).resolve().parents[1]
BOARD_ROOT = "/var/tmp/cg4002-week7-yanjie-20260907"
# Fallback only: the 28 September snapshot. After python -m tools.deploy_service, the
# folder it deployed (recorded locally in DEPLOYMENTS) is used instead.
BOARD_SOURCE = BOARD_ROOT + "/source-co-v2-20260928T122047Z"
DEPLOYMENTS = ROOT / ".comms-local" / "deployments"


# The model runner that tools.deploy_service --runner started (as root, so ps -u misses it).
RUNNER_STATUS = f'''runner_record={BOARD_ROOT}/evidence/model-runner.json
if [ -f "$runner_record" ]; then
  # The recorded PID counts only while its start time matches (a reused PID is another process).
  runner_pid=$(/usr/bin/python3 -c 'import json, os, sys; r = json.load(open(sys.argv[1])); p = "/proc/%d/stat" % r["pid"]; f = open(p).read().rsplit(")", 1)[1].split() if os.path.exists(p) else ["Z"] * 20; print(r["pid"] if f[0] != "Z" and f[19] == str(r["start_ticks"]) else 0)' "$runner_record") || runner_pid=0
  if [ "$runner_pid" -gt 0 ]; then
    printf '\\nModel runner PID %s: ' "$runner_pid"
    ps -o user=,args= -p "$runner_pid" || echo 'not running'
  else
    printf '\\nModel runner: not running (the one deploy_service --runner started last has exited)\\n'
  fi
fi
'''


def runner_processes_script(proc="/proc"):
    """Read-only POSIX sh: every process of any user whose command line names socket_worker
    (Wen Ree's workers, socket_worker_hand among them), as PID:TICKS (start ticks: stat field 22)
    and its arguments, so a runner started by hand can be named in tools.deploy_service
    --replace-runner PID:TICKS.

    The patterns are bracketed, like pgrep's '[u]ltra96.server', so the shells that run this
    script (their command lines hold the script) never match themselves.
    """
    return f'''printf '\\nModel runner processes of any user, as PID:TICKS for deploy_service --replace-runner:\\n'
b07_runners=0
for b07_proc in {shlex.quote(proc)}/[0-9]*; do
  b07_args=$(tr '\\0' ' ' 2>/dev/null < "$b07_proc/cmdline") || continue
  case "$b07_args" in
    *socket[_]worker*) ;;
    *) continue ;;
  esac
  # Field 22 counts from after the parenthesised command name, which may hold spaces.
  b07_ticks=$(awk '{{sub(/.*[)] /, ""); print $20}}' "$b07_proc/stat" 2>/dev/null) || continue
  [ -n "$b07_ticks" ] || continue
  printf '  %s:%s %s\\n' "${{b07_proc##*/}}" "$b07_ticks" "${{b07_args% }}"
  b07_runners=$((b07_runners + 1))
done
[ "$b07_runners" -gt 0 ] || printf '  none\\n'
'''

_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


def _option(argv, name):
    return argv[argv.index(name) + 1] if name in argv[:-1] else None


def _deployment(path):
    """(folder, model, model_path, sensor_version) of one record's running service, or None."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        folder, argv = record["current"]["cwd"], record["current"]["argv"]
        model, model_path = _option(argv, "--model"), _option(argv, "--model-path")
        sensor_version = _option(argv, "--sensor-version")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None  # unreadable, or nothing was left running
    if not (isinstance(folder, str) and folder.startswith(BOARD_ROOT + "/")
            and folder.isprintable()):
        return None
    if model is not None and not (isinstance(model, str) and _MODULE.fullmatch(model)):
        return None
    if model_path is not None and not (isinstance(model_path, str) and model_path.startswith("/")
                                       and model_path.isprintable()):
        return None
    # A service started without --sensor-version speaks v3.
    if sensor_version not in (None, "3", "4"):
        return None
    return folder, model, model_path, 3 if sensor_version is None else int(sensor_version)


def newest_deployment():
    """(folder, model, model_path, sensor_version) of the service the newest local deployment
    record left running, read from that process's own arguments; (BOARD_SOURCE, None, None, 3)
    without one."""
    try:
        records = sorted(DEPLOYMENTS.glob("deployment-*.json"))
    except OSError:
        records = []
    for path in reversed(records):
        deployment = _deployment(path)
        if deployment is not None:
            return deployment
    return BOARD_SOURCE, None, None, 3


def board_source():
    """The working folder of the service the newest local deployment record left running."""
    return newest_deployment()[0]


def ssh_command(script, jump=None):
    # Keep both verified SSH hops; a remote command needs no local forwarding.
    tunnel = tunnel_command(jump or default_jump(),
        "xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg", 18889, 8888, batch=False)
    command = [tunnel[0], "-tt"]
    arguments = iter(tunnel[1:-1])
    for argument in arguments:
        if argument == "-L":
            next(arguments)
        elif argument not in ("-N", "-T"):
            command.append(argument)
    return command + [tunnel[-1], "sh -c " + shlex.quote(script)]


def start_script(session_id):
    validate_session(session_id)
    folder, model, model_path, sensor_version = newest_deployment()
    # Restart what was deployed: the same folder, the same model and the same sensor version
    # (a v3 service is started without --sensor-version, as before).
    model_arguments = "".join(" " + flag + " " + shlex.quote(value)
                              for flag, value in (("--model", model), ("--model-path", model_path))
                              if value is not None)
    if sensor_version == 4:
        model_arguments += " --sensor-version 4"
    # A failed listener query is an error, never evidence that the ports are free.
    return f'''set -eu
cd {shlex.quote(folder)}
command -v ss >/dev/null
b07_listeners=$(ss -ltnH) || {{
  echo 'Could not inspect service ports. Nothing was started.' >&2
  exit 2
}}
while read -r b07_state b07_recv_q b07_send_q b07_local b07_peer; do
  case "$b07_local" in
    *:8888|*:9999)
      echo 'Service ports are already in use. Nothing was stopped or started.'
      ss -ltnp
      exit 2
      ;;
  esac
done <<EOF
$b07_listeners
EOF
exec /usr/bin/python3 -u -m ultra96.server --cert {BOARD_ROOT}/tls/server-cert.pem --key {BOARD_ROOT}/tls/server-key.pem --session-id {shlex.quote(session_id)} --ingest-port 8888 --gateway-port 9999{model_arguments}
'''


def main(start=False, session_id="ltc-comms", jump=None) -> int:
    if start:
        script = start_script(session_id)
        print("Run 'python demo.py service' first. Reuse an existing healthy service.")
        folder, model, _, sensor_version = newest_deployment()
        print("Confirm ESP, board and iPhone versions match this deployment: " + folder)
        print("Model: " + (model or "the built-in dummy model"))
        print("Sensor protocol: v{} (the gloves and the laptop bridge must use the same one; "
              "results stay at v=3)".format(sensor_version))
        print("Session: " + session_id + " (the capture and the phone must use the same one)")
        if input("Press Enter to start only if absent, or type q to cancel: ").strip():
            return 130
    else:
        # Read-only checks: no deployment, process termination or service restart.
        script = '''set -eu
ss -ltnp
ps -u "$(id -u)" -o pid=,args=
for pid in $(pgrep -u "$(id -u)" -f '[u]ltra96.server' || true); do
  printf '\\nServer PID %s, working directory: ' "$pid"
  readlink "/proc/$pid/cwd" || true
done
''' + RUNNER_STATUS + runner_processes_script()
    # Inherited stdio keeps authentication interactive and the server in this terminal.
    return subprocess.run(ssh_command(script, jump), cwd=ROOT).returncode
