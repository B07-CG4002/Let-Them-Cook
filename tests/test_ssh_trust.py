"""Verify real OpenSSH effective settings for both destination and proxy hops."""
import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.ssh_tunnel import supervise, tunnel_command


class TunnelTrustTests(unittest.TestCase):
    def test_jump_command_has_its_own_strict_host_verification(self):
        command = tunnel_command("user@jump.invalid", "user@target.invalid", 18888, 8888)
        proxies = [value for value in command if value.startswith("ProxyCommand=")]
        self.assertEqual(len(proxies), 1, "jump subprocess needs an explicit independent trust policy")
        self.assertIn("StrictHostKeyChecking=yes", proxies[0])
        self.assertNotIn("-J", command)

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH effective-config verification")
    def test_interactive_target_allows_password_entry_at_the_jump(self):
        command = tunnel_command("user@jump.invalid", "user@target.invalid", 18888, 8888,
                                 batch=False)
        # The target's banner deadline includes the proxy's password prompt.
        # -G expands only configuration, without executing either SSH hop.
        with tempfile.TemporaryDirectory(prefix="comms-ssh-interactive-") as folder:
            config_file = Path(folder) / "unsafe-defaults.conf"
            config_file.write_text("Host *\n  StrictHostKeyChecking no\n  BatchMode yes\n"
                                   "  ConnectTimeout 777\n  ServerAliveInterval 0\n"
                                   "  ServerAliveCountMax 99\n  Port 2222\n")

            def effective(args):
                output = subprocess.check_output(
                    [args[0], "-G", "-F", str(config_file)] + args[1:],
                    text=True, stderr=subprocess.PIPE, timeout=10)
                return dict(line.split(" ", 1) for line in output.splitlines() if " " in line)

            outer = effective(command)
            proxy = [part.replace("%h", outer["hostname"]).replace("%p", outer["port"])
                     for part in shlex.split(outer["proxycommand"])]
            inner = effective(proxy)

        self.assertEqual(outer["connecttimeout"], "60")
        self.assertEqual(outer["port"], "22")
        self.assertEqual(inner["connecttimeout"], "20")
        for config in (outer, inner):
            self.assertIn(config["stricthostkeychecking"], ("true", "yes"))
            self.assertEqual(config["batchmode"], "no")
            self.assertEqual(config["serveraliveinterval"], "15")
            self.assertEqual(config["serveralivecountmax"], "3")
        self.assertEqual(inner["hostname"], "jump.invalid")
        self.assertEqual(outer["hostname"], "target.invalid")

    @unittest.skipUnless(shutil.which("ssh"), "OpenSSH effective-config verification")
    def test_both_hops_override_unsafe_defaults_in_supervised_mode(self):
        command = tunnel_command("user@jump.invalid", "user@target.invalid", 18888, 8888, batch=True)
        # -G only expands configuration; neither invocation opens a network connection.
        with tempfile.TemporaryDirectory(prefix="comms-ssh-config-") as folder:
            config_file = Path(folder) / "unsafe-defaults.conf"
            config_file.write_text("Host *\n  StrictHostKeyChecking no\n  BatchMode no\n  ConnectTimeout 777\n  Port 2222\n")
            def effective(args):
                output = subprocess.check_output([args[0], "-G", "-F", str(config_file)] + args[1:],
                                                 text=True, stderr=subprocess.PIPE)
                return dict(line.split(" ", 1) for line in output.splitlines() if " " in line)
            outer = effective(command)
            proxy = [part.replace("%h", outer["hostname"]).replace("%p", outer["port"])
                     for part in shlex.split(outer["proxycommand"])]
            inner = effective(proxy)
        for config in (outer, inner):
            self.assertIn(config["stricthostkeychecking"], ("true", "yes"))
            self.assertEqual(config["batchmode"], "yes")
            self.assertEqual(config["connecttimeout"], "10")
            self.assertEqual(config["serveraliveinterval"], "15")
            self.assertEqual(config["serveralivecountmax"], "3")
        self.assertEqual(inner["hostname"], "jump.invalid")
        self.assertEqual(outer["hostname"], "target.invalid")
        self.assertEqual(outer["port"], "22")
        self.assertIn("127.0.0.1:18888:127.0.0.1:8888", command)

    def test_identity_path_stays_on_destination_only(self):
        identity = "C:/keys with spaces/final-target.pem"
        command = tunnel_command("user@jump.invalid", "user@target.invalid", 19999, 9999,
                                 identity=identity, batch=True)
        proxy = next(value for value in command if value.startswith("ProxyCommand="))
        self.assertNotIn(identity, proxy)
        self.assertEqual(command[command.index("-i") + 1], identity)


class SupervisorTrustTests(unittest.IsolatedAsyncioTestCase):
    async def test_supervisor_refuses_an_interactive_proxy_before_spawning(self):
        async def forbidden_spawn(*args, **kwargs):
            raise AssertionError("interactive proxy must never be spawned")
        command = tunnel_command("user@jump.invalid", "user@target.invalid", 18888, 8888)
        with self.assertRaises(ValueError):
            await supervise(command, asyncio.Event(), spawn=forbidden_spawn)
