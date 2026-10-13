"""Foreground Ultra96 status/start operations for the existing demo CLI."""
from pathlib import Path
import shlex
import subprocess

from tools.ssh_tunnel import tunnel_command


ROOT = Path(__file__).resolve().parents[1]
BOARD_ROOT = "/var/tmp/cg4002-week7-yanjie-20260907"
BOARD_SOURCE = BOARD_ROOT + "/source-co-v2-20260928T122047Z"


def ssh_command(script):
    # Keep both verified SSH hops; a remote command needs no local forwarding.
    tunnel = tunnel_command("yanjie@stujump.comp.nus.edu.sg",
        "xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg", 18889, 8888, batch=False)
    command = [tunnel[0], "-tt"]
    arguments = iter(tunnel[1:-1])
    for argument in arguments:
        if argument == "-L":
            next(arguments)
        elif argument not in ("-N", "-T"):
            command.append(argument)
    return command + [tunnel[-1], "sh -c " + shlex.quote(script)]


def start_script():
    # A failed listener query is an error, never evidence that the ports are free.
    return f'''set -eu
cd {shlex.quote(BOARD_SOURCE)}
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
exec /usr/bin/python3 -u -m ultra96.server --cert {BOARD_ROOT}/tls/server-cert.pem --key {BOARD_ROOT}/tls/server-key.pem --session-id week7-demo --ingest-port 8888 --gateway-port 9999
'''


def main(start=False) -> int:
    if start:
        print("Run 'python demo.py service' first. Reuse an existing healthy service.")
        print("Confirm ESP, board and iPhone versions match this deployment: " + BOARD_SOURCE)
        if input("Press Enter to start only if absent, or type q to cancel: ").strip():
            return 130
        script = start_script()
    else:
        # Read-only checks: no deployment, process termination or service restart.
        script = '''set -eu
ss -ltnp
ps -u "$(id -u)" -o pid=,args=
for pid in $(pgrep -u "$(id -u)" -f '[u]ltra96.server' || true); do
  printf '\\nServer PID %s, working directory: ' "$pid"
  readlink "/proc/$pid/cwd" || true
done
'''
    # Inherited stdio keeps authentication interactive and the server in this terminal.
    return subprocess.run(ssh_command(script), cwd=ROOT).returncode
