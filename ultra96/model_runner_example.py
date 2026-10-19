"""Example runner for ultra96.model_bridge: the smallest program that speaks its format.

It connects to the service's model socket, reads samples, and after every
--every samples of a glove sends one result naming that glove's latest sample.
When the service is not there yet, or restarts, it waits and connects again.
The model owner's real runner replaces the marked part with the model. It
imports nothing from this repository and uses the standard library only, so it
can be copied anywhere and run under any Python 3, including PYNQ's:

    sudo <pynq python> model_runner_example.py
    python3 -m ultra96.model_runner_example --socket /path/to/model-runner.sock
"""
import argparse
import json
import socket
import sys
import time

# The deployment folder of the board service (tools/demo_service.py BOARD_ROOT).
DEFAULT_SOCKET = "/var/tmp/cg4002-week7-yanjie-20260907/model-runner.sock"


def run(sock, every=50, label="CHOP", out=sys.stdout):
    counts = {}
    reader = sock.makefile("r", encoding="utf-8")
    for line in reader:
        message = json.loads(line)
        if message["type"] == "hello":
            print("connected, channels:", ",".join(message["channels"]), file=out, flush=True)
            continue
        if message["type"] != "sample":
            continue  # ignore message types this runner does not know
        device = message["device_id"]
        counts[device] = counts.get(device, 0) + 1
        if counts[device] % every:
            continue
        # The model goes here: message["values"] holds the 11 floats of this sample.
        result = dict(type="result", gesture=label, confidence=1.0, device_id=device,
                      boot_id=message["boot_id"], seq=message["seq"])
        sock.sendall((json.dumps(result) + "\n").encode("utf-8"))


def connect(path, out=sys.stdout):
    """Connect to path, waiting while the service is not listening."""
    waiting = False
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(path)
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            if not waiting:
                print("waiting for the service on", path, file=out, flush=True)
                waiting = True
            time.sleep(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="the service's model socket")
    parser.add_argument("--every", type=int, default=50, help="one result per this many samples of a glove")
    parser.add_argument("--label", default="CHOP")
    args = parser.parse_args(argv)
    try:
        while True:
            with connect(args.socket) as sock:
                try:
                    run(sock, args.every, args.label)
                except (ConnectionError, BrokenPipeError):
                    pass  # the service stopped while a result was being sent
            # A second runner on the socket replaces this one; waiting keeps the two from
            # taking the connection from each other many times a second.
            print("disconnected; connecting again in 1 s", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
