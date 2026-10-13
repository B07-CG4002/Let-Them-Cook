"""Build or supervise an owned, loopback-only OpenSSH local forward."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shlex
import subprocess


def tunnel_command(jump, target, local_port, remote_port, identity=None, batch=False):
    """Build both hops explicitly; identity applies only to the final target.

    Batch mode is required for supervision. Interactive printed commands may
    prompt normally while retaining strict known-host verification on both hops.
    Their target banner deadline also covers time spent entering a proxy password.
    Ultra96 exposes only SSH on TCP 22; application ports are tunnel destinations.
    """
    for host in (jump, target):
        if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9_.]+@[A-Za-z0-9][A-Za-z0-9.-]*", host):
            raise ValueError("SSH endpoint must be user@hostname with no options or whitespace")
    for port in (local_port, remote_port):
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("port must be 1..65535")
    trust_options = ["-o", "StrictHostKeyChecking=yes",
                     "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
                     "-o", "BatchMode=" + ("yes" if batch else "no")]
    proxy = ["ssh"] + trust_options + [
        "-o", "ConnectTimeout=" + ("10" if batch else "20"),
        "-W", "[%h]:%p", jump]
    quote_command = subprocess.list2cmdline if os.name == "nt" else shlex.join
    cmd = ["ssh", "-N", "-T"] + trust_options + [
           "-o", "Port=22",
           "-o", "ConnectTimeout=" + ("10" if batch else "60"),
           "-o", "ProxyCommand=" + quote_command(proxy),
           "-o", "ExitOnForwardFailure=yes",
           "-L", "127.0.0.1:%d:127.0.0.1:%d" % (local_port, remote_port)]
    if identity:
        cmd += ["-i", os.fspath(identity)]
    return cmd + [target]


async def supervise(command, stop, *, spawn=asyncio.create_subprocess_exec):
    """Key/agent-auth only, restart exited children, terminate only our child."""
    if "-J" in command:
        raise ValueError("supervision requires an explicit independently verified proxy")
    for option in command:
        if option.startswith("ProxyCommand=") and (
                "-o BatchMode=yes" not in option or "BatchMode=no" in option):
            raise ValueError("supervised proxy requires tunnel_command(..., batch=True)")
    delay = 0.5
    while not stop.is_set():
        options = dict(stdin=subprocess.DEVNULL)
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        child = await spawn(*command[:1], "-o", "BatchMode=yes", *command[1:], **options)
        child_wait = asyncio.create_task(child.wait())
        stop_wait = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((child_wait, stop_wait), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
            await asyncio.gather(stop_wait, return_exceptions=True)
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(child_wait), 2.0)
                except asyncio.TimeoutError:
                    child.kill()
                    await asyncio.wait_for(child_wait, 2.0)
            if not child_wait.done():
                child_wait.cancel()
            await asyncio.gather(child_wait, return_exceptions=True)
        if not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(5.0, delay * 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jump", default="yanjie@stujump.comp.nus.edu.sg")
    parser.add_argument("--target", default="xilinx@makerslab-fpga-35.ddns.comp.nus.edu.sg")
    parser.add_argument("--local-port", type=int, default=18888)
    parser.add_argument("--remote-port", type=int, default=8888)
    parser.add_argument("--identity")
    parser.add_argument("--run", action="store_true", help="supervise using existing key/agent authentication")
    args = parser.parse_args()
    command = tunnel_command(args.jump, args.target, args.local_port, args.remote_port,
                             args.identity, batch=args.run)
    if not args.run:
        print(subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command))
        return
    async def run():
        await supervise(command, asyncio.Event())
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
