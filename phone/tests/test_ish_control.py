"""Temporary iSH launcher tests; subprocesses never contact a device."""
import base64
import contextlib
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    control = importlib.import_module("phone.ish_control")
except ModuleNotFoundError:
    control = None


def public_key():
    kind = b"ssh-ed25519"
    blob = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + bytes(range(32))
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def metadata():
    return {"id": "week7-20260908", "remote_dir": "/home/xilinx/week7-control/week7-20260908",
            "client_public_key": public_key()}


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(control, "the scoped iSH helper is not implemented")

    def test_metadata_rejects_shell_paths_and_malformed_keys(self):
        self.assertEqual(control.validate_metadata(metadata()), metadata())
        bad = [("remote_dir", "/tmp/a;touch-x"), ("remote_dir", "/tmp/../root"),
               ("remote_dir", "/tmp/a\nb"), ("id", "../../root"),
               ("client_public_key", "ssh-ed25519 AAAA"),
               ("client_public_key", public_key() + "\n" + public_key())]
        for name, value in bad:
            data = metadata(); data[name] = value
            with self.subTest(name=name, value=value), self.assertRaises(control.ControlError):
                control.validate_metadata(data)

    def test_locked_account_refusal_does_not_expose_hash(self):
        for secret in ["!", "!!", "!hidden-hash"]:
            with self.subTest(secret=secret), self.assertRaisesRegex(control.ControlError, "PHONE_CONTROL_ROOT_LOCKED") as caught:
                control.check_root_account(lambda name: SimpleNamespace(sp_pwdp=secret))
            self.assertNotIn("hidden-hash", str(caught.exception))
        control.check_root_account(lambda name: SimpleNamespace(sp_pwdp="$6$opaque"))
        control.check_root_account(lambda name: SimpleNamespace(sp_pwdp="*"))

    def test_exclusive_probe_refuses_an_occupied_loopback_port(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0)); listener.listen(1)
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(control.ControlError, "PORT_BUSY"):
                control.check_port_free(port)
        control.check_port_free(port)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as after:
            after.bind(("127.0.0.1", port))

    def test_existing_runtime_directory_mode_is_preserved_or_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "sshd"; directory.mkdir()
            with patch.object(control.os, "lstat", return_value=SimpleNamespace(st_mode=0o40755, st_uid=0)), \
                 patch.object(control.os, "chmod") as chmod:
                control.ensure_runtime_directory(directory)
                chmod.assert_not_called()
            for mode, uid in [(0o40777, 0), (0o120777, 0), (0o40755, 1000)]:
                with patch.object(control.os, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=uid)), \
                     self.assertRaises(control.ControlError):
                    control.ensure_runtime_directory(directory)

    def test_missing_master_aborts_before_creating_scope(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(control, "check_root_account"), \
             patch.object(control, "require_root"), \
             patch.object(control.subprocess, "run", return_value=SimpleNamespace(returncode=255, stdout="", stderr="private diagnostic")):
            with self.assertRaisesRegex(control.ControlError, "MASTER_UNAVAILABLE"):
                control.start_control(metadata(), "/tmp/master.sock", Path(tmp))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def launch(self, fail=None):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        calls = []
        process = SimpleNamespace(pid=34567, returncode=None)
        process.poll = lambda: process.returncode
        def terminate():
            calls.append(["terminate", str(process.pid)])
            process.returncode = 0
        process.terminate = terminate
        process.wait = lambda timeout: 0
        def run(args, **kwargs):
            calls.append(args)
            operation = args[args.index("-O") + 1] if "-O" in args else None
            if args[0] == "ssh-keygen" and "-t" in args:
                key_path = Path(args[args.index("-f") + 1])
                key_path.write_text("PRIVATE TEST KEY", encoding="ascii")
                Path(str(key_path) + ".pub").write_text(public_key() + "\n", encoding="ascii")
            if args[0] == "ssh-keygen" and "-l" in args:
                return SimpleNamespace(returncode=0, stdout="256 SHA256:publicfingerprint test (ED25519)\n", stderr="")
            if (fail is not None and operation == fail) or (fail == "publish" and "input" in kwargs) or (fail == "config" and "-t" in args and args[0].endswith("sshd")):
                return SimpleNamespace(returncode=1, stdout="", stderr="sensitive diagnostic")
            if fail == "remote" and args[0] == "ssh" and "-O" not in args and "input" not in kwargs:
                return SimpleNamespace(returncode=1, stdout="", stderr="Address already in use")
            if "input" in kwargs:
                calls.append(["published", kwargs["input"]])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        stack.enter_context(patch.object(control.os, "geteuid", return_value=0, create=True))
        real_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            if path == home / "week7-control":
                return SimpleNamespace(st_mode=0o40700, st_uid=0)
            if home / "week7-control" in path.parents:
                info = real_lstat(path, *args, **kwargs)
                return SimpleNamespace(st_mode=0o40700 if info.st_mode & 0o40000 else 0o100600, st_uid=0)
            return real_lstat(path, *args, **kwargs)
        stack.enter_context(patch.object(Path, "lstat", lstat))
        for name in ["require_root", "check_root_account", "check_port_free", "ensure_runtime_directory", "wait_for_listener"]:
            stack.enter_context(patch.object(control, name))
        stack.enter_context(patch.object(control.subprocess, "run", side_effect=run))
        stack.enter_context(patch.object(control.subprocess, "Popen", return_value=process))
        return home, calls

    def test_started_scope_is_key_only_and_published_without_authentication_claim(self):
        home, calls = self.launch()
        status = control.start_control(metadata(), "/tmp/master.sock", home)
        directory = Path(status["directory"])
        self.assertEqual(directory.parent, home / "week7-control")
        config = dict(line.split(None, 1) for line in (directory / "sshd_config").read_text().splitlines())
        for name, value in {"ListenAddress": "127.0.0.1", "Port": "2222", "PermitRootLogin": "prohibit-password",
                            "PasswordAuthentication": "no", "KbdInteractiveAuthentication": "no",
                            "ChallengeResponseAuthentication": "no",
                            "AuthenticationMethods": "publickey", "UsePAM": "no", "DisableForwarding": "yes",
                            "PermitTTY": "no", "X11Forwarding": "no", "PermitUserEnvironment": "no", "StrictModes": "yes"}.items():
            self.assertEqual(config.get(name), value, name)
        self.assertEqual(shlex.split(config["AuthorizedKeysFile"]), [str(directory / "authorized_keys")])
        self.assertEqual((directory / "authorized_keys").read_text().strip(), public_key())
        self.assertFalse((home / ".ssh" / "authorized_keys").exists())
        forward = next(args for args in calls if "forward" in args)
        self.assertEqual(forward, ["ssh", "-F", "/dev/null", "-S", "/tmp/master.sock", "-o", "BatchMode=yes", "-O", "forward", "-R", "127.0.0.1:22222:127.0.0.1:2222", "127.0.0.1"])
        published = json.loads(next(args[1] for args in calls if args[0] == "published"))
        self.assertEqual(published["state"], "sshd_started_reverse_requested")
        self.assertFalse(published["root_authentication_verified"])
        self.assertEqual(published["host_public_key"], public_key())
        self.assertNotIn("PRIVATE", json.dumps(status))
        self.assertTrue((directory / "state.json").is_file())

    @unittest.skipUnless(os.name == "posix", "requires local POSIX OpenSSH tools")
    def test_sshd_effective_policy_disables_interactive_authentication(self):
        # ISH_TEST_SSHD allows this regression to run against OpenSSH 8.6p1:
        # before 8.7, challenge-response defaults to yes and overrides KbdInteractiveAuthentication.
        sshd = os.environ.get("ISH_TEST_SSHD") or shutil.which("sshd") or control.SSHD
        keygen = shutil.which("ssh-keygen")
        if not shutil.which(sshd) or keygen is None:
            self.skipTest("local sshd and ssh-keygen are required; set ISH_TEST_SSHD for an older build")
        with tempfile.TemporaryDirectory(prefix="ish policy ") as tmp:
            directory = Path(tmp)
            subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(directory / "host_ed25519")],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
            config = directory / "sshd_config"
            config.write_text(control.config_text(directory), encoding="utf-8")
            result = subprocess.run([sshd, "-T", "-f", str(config)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            effective = dict(line.split(None, 1) for line in result.stdout.splitlines())
            self.assertEqual(effective["kbdinteractiveauthentication"], "no")
            self.assertEqual(effective["passwordauthentication"], "no")
            self.assertEqual(effective["authenticationmethods"], "publickey")
            # OpenSSH 8.7+ omits the deprecated alias from its effective output.
            if "challengeresponseauthentication" in effective:
                self.assertEqual(effective["challengeresponseauthentication"], "no")

    def test_failed_publication_cancels_only_added_reverse_and_terminates_owned_child(self):
        home, calls = self.launch("publish")
        with self.assertRaisesRegex(control.ControlError, "PUBLISH_FAILED"):
            control.start_control(metadata(), "/tmp/master.sock", home)
        cancel = next(args for args in calls if "cancel" in args)
        self.assertEqual(cancel[1:5], ["-F", "/dev/null", "-S", "/tmp/master.sock"])
        self.assertIn("127.0.0.1:22222:127.0.0.1:2222", cancel)
        self.assertIn(["terminate", "34567"], calls)
        self.assertFalse(any("exit" in args for args in calls))

    def test_failed_forward_does_not_cancel_an_unowned_forward(self):
        home, calls = self.launch("forward")
        with self.assertRaisesRegex(control.ControlError, "FORWARD_FAILED"):
            control.start_control(metadata(), "/tmp/master.sock", home)
        self.assertFalse(any("cancel" in args for args in calls))
        self.assertIn(["terminate", "34567"], calls)
        cleanup = json.loads(next((home / "week7-control").glob("*/cleanup.json")).read_text())
        self.assertEqual(cleanup["reverse_cancel"], "outcome_unknown_board_observation_required")

    def test_board_preflight_failure_creates_no_local_daemon_or_scope(self):
        home, calls = self.launch("remote")
        with self.assertRaisesRegex(control.ControlError, "REMOTE_UNAVAILABLE"):
            control.start_control(metadata(), "/tmp/master.sock", home)
        self.assertEqual(list(home.iterdir()), [])
        self.assertFalse(any("forward" in args or "cancel" in args for args in calls))

    def test_invalid_sshd_config_never_spawns_a_daemon(self):
        home, calls = self.launch("config")
        with self.assertRaisesRegex(control.ControlError, "CONFIG_INVALID"):
            control.start_control(metadata(), "/tmp/master.sock", home)
        self.assertFalse(any("forward" in args or "terminate" in args for args in calls))

    def test_proc_identity_uses_executable_scoped_log_and_session_not_ish_zero_start_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp); process = proc / "123"; process.mkdir()
            scope = Path("/root/week7-control/run")
            (process / "stat").write_text("123 (sshd) " + " ".join(["S", "1", "123", "123"] + ["0"] * 18))
            expected_links = {str(process / "exe"): "/usr/sbin/sshd", str(process / "fd" / "2"): str(scope / "sshd.log")}
            real_readlink = control.os.readlink

            def readlink(path):
                # Only emulate /proc entries. realpath must still resolve host
                # symlinks, including macOS /var -> /private/var.
                if str(path) in expected_links:
                    return expected_links[str(path)]
                return real_readlink(path)

            with patch.object(control.os, "readlink", side_effect=readlink):
                control.verify_scoped_process(123, scope, proc)
                expected_links[str(process / "fd" / "2")] = "/var/log/another-sshd.log"
                with self.assertRaisesRegex(control.ControlError, "PROCESS_UNVERIFIED"):
                    control.verify_scoped_process(123, scope, proc)
                expected_links[str(process / "fd" / "2")] = str(scope / "sshd.log")
                (process / "stat").write_text("123 (sshd) " + " ".join(["S", "1", "120", "120"] + ["0"] * 18))
                with self.assertRaisesRegex(control.ControlError, "PROCESS_UNVERIFIED"):
                    control.verify_scoped_process(123, scope, proc)

    def test_stop_checks_scope_and_pid_identity_before_signalling(self):
        home, calls = self.launch()
        status = control.start_control(metadata(), "/tmp/master.sock", home)
        state = Path(status["directory"]) / "state.json"
        with patch.object(control, "verify_scoped_process", side_effect=control.ControlError("PID differs")), \
             patch.object(control.os, "kill") as kill:
            with self.assertRaises(control.ControlError):
                control.stop_control(state, home)
            kill.assert_not_called()
        with patch.object(control, "verify_scoped_process"), patch.object(control.os, "kill") as kill:
            stopped = control.stop_control(state, home)
            kill.assert_called_once_with(34567, signal.SIGTERM)
        self.assertEqual(stopped["reverse_cancel"], "requested_board_observation_pending")
        self.assertEqual(stopped["sshd_stop"], "signal_requested")
        self.assertFalse(any("exit" in args for args in calls))

    def test_stop_refuses_a_state_outside_its_control_root(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(control, "require_root"), \
             patch.object(control.os, "kill") as kill:
            path = Path(tmp) / "state.json"; path.write_text("{}")
            with self.assertRaises(control.ControlError):
                control.stop_control(path, Path(tmp))
            kill.assert_not_called()

    def test_stop_rejects_malformed_saved_state_before_signalling(self):
        home, calls = self.launch()
        status = control.start_control(metadata(), "/tmp/master.sock", home)
        path = Path(status["directory"]) / "state.json"; path.write_text("[]")
        with patch.object(control.os, "kill") as kill:
            with self.assertRaises(control.ControlError):
                control.stop_control(path, home)
            kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
