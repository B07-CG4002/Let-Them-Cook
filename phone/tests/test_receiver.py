"""Standalone Android receiver tests; no BLE, SSH or external services."""
import asyncio
import json
import contextlib
import io
from pathlib import Path
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from phone.receiver import ProtocolError, encode_frame, read_frame, receive, tls_context, validate_result, validate_subscribed, validate_session


MODEL_LABELS = ("PICKUPHOLD", "PLACEDOWN", "CHOP", "WASH", "THROW", "FIREEXT")


def result():
    # A version-3 model result: request_id null, a model label and a real confidence.
    return dict(v=3, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1,
                boot_id=4294967295, seq=3, request_id=None, result_id="1:4294967295:3",
                gesture="PICKUPHOLD", confidence=0.73)


def command_result():
    return dict(result(), request_id=3, result_id="cmd:1:4294967295:3")


class ContractTests(unittest.IsolatedAsyncioTestCase):
    def assert_rejected(self, item, **context):
        with self.subTest(**context), self.assertRaises(ProtocolError):
            validate_result(item, "ltc-comms")

    def test_result_and_subscription_contracts(self):
        self.assertEqual(validate_session("a" * 128), "a" * 128)
        for invalid in ["", "a" * 129, "line\nbreak", "\ud800"]:
            with self.assertRaises(ProtocolError):
                validate_session(invalid)
        self.assertEqual(validate_result(result(), "ltc-comms"), result())
        validate_subscribed(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms"), "ltc-comms")
        # The subscription envelopes did not move to version 3.
        for version in (2, 3, True):
            with self.subTest(subscribed=version), self.assertRaises(ProtocolError):
                validate_subscribed(dict(v=version, type="SUBSCRIBED", session_id="ltc-comms"), "ltc-comms")
        for field, value in [("v", True), ("v", 3.0), ("seq", -1), ("boot_id", 2**32),
                             ("device_id", 0), ("device_id", 3),
                             ("result_id", "1:2:3"), ("result_id", "cmd:1:4294967295:3"),
                             ("session_id", "elsewhere"), ("type", "SUBSCRIBED"), ("extra", 1)]:
            self.assert_rejected(dict(result(), **{field: value}), field=field, value=value)
        for field in result():
            item = result(); del item[field]
            self.assert_rejected(item, missing=field)

    def test_only_version_3_results_are_accepted(self):
        for version in (0, 1, 2, 4):
            self.assert_rejected(dict(result(), v=version), version=version)
        # The exact version-1 and version-2 dummy results of the earlier protocol.
        old = dict(v=1, type="GESTURE_RESULT", session_id="ltc-comms", device_id=1, boot_id=4294967295,
                   seq=3, result_id="1:4294967295:3", gesture="POINT", confidence=1.0)
        self.assert_rejected(old, shape="version 1")
        self.assert_rejected(dict(old, v=2, request_id=None), shape="version 2")

    def test_any_well_formed_label_and_real_confidence_is_accepted(self):
        for gesture in MODEL_LABELS + ("REST", "FIST", "OPEN", "POINT", "a", "Z" * 32, "left_chop-2"):
            item = dict(result(), gesture=gesture)
            with self.subTest(gesture=gesture):
                self.assertEqual(validate_result(item, "ltc-comms"), item)
        for confidence in (0, 1, 0.0, 1.0, 0.5, 1e-05, 0.999999, -0.0):
            item = dict(result(), confidence=confidence)
            with self.subTest(confidence=confidence):
                self.assertEqual(validate_result(item, "ltc-comms"), item)

    def test_gesture_rule(self):
        for gesture in ["", "A" * 33, "PICK UP", "PICK.UP", "PICKUP\n", "\nPICKUP", "CHOP\t",
                        "caf\u00e9", "\uff21", "\u0661", None, 5, ["CHOP"]]:
            self.assert_rejected(dict(result(), gesture=gesture), gesture=gesture)

    def test_confidence_rule(self):
        for confidence in [-0.01, 1.01, 1.5, -1, 2, float("inf"), float("-inf"), float("nan"),
                           True, False, None, "0.5", 10**12]:
            self.assert_rejected(dict(result(), confidence=confidence), confidence=confidence)

    def test_command_echo_correlation(self):
        self.assertEqual(validate_result(command_result(), "ltc-comms"), command_result())
        self.assertEqual(validate_result(dict(command_result(), request_id=4294967295, seq=4294967295,
                                              result_id="cmd:1:4294967295:4294967295"), "ltc-comms")["seq"],
                         4294967295)
        for request in [True, 0, -1, 2, 3.0, 2**32, "3"]:
            self.assert_rejected(dict(command_result(), request_id=request), request_id=request)
        # A command result without the cmd: prefix, and a model result with it.
        self.assert_rejected(dict(command_result(), result_id="1:4294967295:3"), prefix="missing")
        self.assert_rejected(dict(result(), result_id="cmd:1:4294967295:3"), prefix="unexpected")

    async def test_wire_numbers_reach_the_confidence_rule(self):
        def frame(confidence):
            body = ('{"v":3,"type":"GESTURE_RESULT","session_id":"ltc-comms","device_id":2,"boot_id":7,'
                    '"seq":9,"request_id":null,"result_id":"2:7:9","gesture":"CHOP","confidence":'
                    + confidence + '}').encode("utf-8")
            stream = asyncio.StreamReader()
            stream.feed_data(len(body).to_bytes(4, "big") + body); stream.feed_eof()
            return stream
        for token, value in [("0.73", 0.73), ("7.3e-1", 0.73), ("1", 1), ("0", 0), ("1e-05", 1e-05)]:
            with self.subTest(token=token):
                message = validate_result(await read_frame(frame(token)), "ltc-comms")
                self.assertEqual(message["confidence"], value)
        # 1e999 overflows to Infinity and NaN is not JSON: the frame itself is refused.
        # true decodes to a boolean, which the confidence rule refuses.
        for token in ["1e999", "-1e999", "NaN", "Infinity", "true"]:
            with self.subTest(token=token), self.assertRaises(ProtocolError):
                validate_result(await read_frame(frame(token)), "ltc-comms")
        for token in ["1.01", "-0.5", "\"0.5\"", "null"]:
            with self.subTest(token=token), self.assertRaises(ProtocolError):
                validate_result(await read_frame(frame(token)), "ltc-comms")

    async def test_split_and_coalesced_frames(self):
        stream = asyncio.StreamReader()
        data = encode_frame(result())
        async def feed():
            for value in data:
                stream.feed_data(bytes([value]))
                await asyncio.sleep(0)
            stream.feed_data(data)
        task = asyncio.create_task(feed())
        self.assertEqual(await read_frame(stream), result())
        self.assertEqual(await read_frame(stream), result())
        await task

    async def test_reject_bad_frames(self):
        payloads = [b'{"v":1,"v":1}', b'{"v":NaN}', b'{"v":1e999}', b'[]', b'{} trailing', b'\xff']
        frames = [len(data).to_bytes(4, "big") + data for data in payloads]
        frames += [b'\0\0\0\0', (16385).to_bytes(4, "big"), b'\0\0', b'\0\0\0\x02{']
        for data in frames:
            stream = asyncio.StreamReader(); stream.feed_data(data); stream.feed_eof()
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                await read_frame(stream)

    async def test_incomplete_frame_deadline(self):
        stream = asyncio.StreamReader(); stream.feed_data(b'\0')
        with self.assertRaises(asyncio.TimeoutError):
            await read_frame(stream, timeout=0.02)

    async def test_startup_grace_ends_at_first_byte_of_partial_prefix_or_body(self):
        for partial in [b'\0', b'\0\0\0\x02{']:
            stream = asyncio.StreamReader(); stream.feed_data(partial)
            with self.subTest(partial=partial):
                started = asyncio.get_running_loop().time()
                with self.assertRaises(asyncio.TimeoutError):
                    await read_frame(stream, timeout=0.05, first_byte_timeout=0.5)
                self.assertLess(asyncio.get_running_loop().time() - started, 0.3)

    async def test_prefix_and_body_share_one_deadline_after_startup_byte(self):
        stream = asyncio.StreamReader(); stream.feed_data(b'\0')
        loop = asyncio.get_running_loop()
        prefix = loop.call_later(0.12, stream.feed_data, b'\0\0\x02')
        body = loop.call_later(0.28, stream.feed_data, b'{}')
        try:
            with self.assertRaises(asyncio.TimeoutError):
                await read_frame(stream, timeout=0.2, first_byte_timeout=0.5)
        finally:
            prefix.cancel(); body.cancel()

    async def test_startup_idle_budget_is_bounded(self):
        stream = asyncio.StreamReader()
        with self.assertRaises(asyncio.TimeoutError):
            await read_frame(stream, timeout=0.5, first_byte_timeout=0.05)

    async def test_startup_eof_remains_a_protocol_error(self):
        for partial in [b'', b'\0', b'\0\0\0\x02{']:
            stream = asyncio.StreamReader(); stream.feed_data(partial); stream.feed_eof()
            with self.subTest(partial=partial), self.assertRaises(ProtocolError):
                await read_frame(stream, first_byte_timeout=0.5)

    async def test_cancellation_propagates_during_startup_idle_or_partial_frame(self):
        for partial in [b'', b'\0']:
            stream = asyncio.StreamReader(); stream.feed_data(partial)
            task = asyncio.create_task(read_frame(stream, first_byte_timeout=0.5))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.subTest(partial=partial), self.assertRaises(asyncio.CancelledError):
                await task

    async def test_excessive_integer_is_a_protocol_error_on_every_supported_python(self):
        body = b'{"v":' + b'9' * 5000 + b'}'
        stream = asyncio.StreamReader()
        stream.feed_data(len(body).to_bytes(4, "big") + body)
        with self.assertRaises(ProtocolError):
            await read_frame(stream)


class ReceiverCloseTests(unittest.IsolatedAsyncioTestCase):
    async def receiver_with_writer(self, writer):
        stream = asyncio.StreamReader()
        stream.feed_data(encode_frame(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")))
        stream.feed_data(encode_frame(result()))
        args = SimpleNamespace(ca="unused", port=19999, session="ltc-comms", count=1, duration=None)
        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", AsyncMock(return_value=(stream, writer))), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return await receive(args)

    async def test_stalled_tls_shutdown_aborts_socket_after_deadline(self):
        writer = ClosingWriter(stall=True)
        summary = await self.receiver_with_writer(writer)
        self.assertEqual(summary["received"], 1)
        self.assertTrue(writer.closed)
        self.assertTrue(writer.aborted, "timed-out close must release its socket")

    async def test_cancellation_during_tls_shutdown_aborts_socket_and_propagates(self):
        writer = ClosingWriter(stall=True)
        task = asyncio.create_task(self.receiver_with_writer(writer))
        await asyncio.wait_for(writer.closing.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(writer.aborted, "cancelled close must release its socket")

    async def test_clean_tls_shutdown_preserves_graceful_close(self):
        writer = ClosingWriter(stall=False)
        self.assertEqual((await self.receiver_with_writer(writer))["received"], 1)
        self.assertTrue(writer.closed)
        self.assertFalse(writer.aborted)


class ClosingWriter:
    """TLS shutdown boundary: no network mock participates in schema validation."""
    def __init__(self, stall):
        self.stall = stall
        self.closed = self.aborted = False
        self.closing = asyncio.Event()
        self.transport = SimpleNamespace(abort=self.abort)

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    def abort(self):
        self.aborted = True

    async def wait_closed(self):
        self.closing.set()
        if self.stall:
            await asyncio.Event().wait()


class ReceiverStartupTests(unittest.IsolatedAsyncioTestCase):
    @contextlib.asynccontextmanager
    async def running_receiver(self, streams, count=1):
        """Keep framing real; scale its budgets and replace only TLS transport."""
        writers = [ClosingWriter(stall=False) for _ in streams]
        output, errors = io.StringIO(), io.StringIO()
        statistics = dict(received=0, reconnects=0)
        args = SimpleNamespace(ca="unused", port=19999, session="ltc-comms", count=count, duration=None)

        async def fast_frame(reader, timeout=5.0, first_byte_timeout=None, allow_idle=False):
            options = {} if first_byte_timeout is None else dict(first_byte_timeout=first_byte_timeout * 0.02)
            if allow_idle:
                options['allow_idle'] = True
            return await read_frame(reader, timeout=timeout * 0.02, **options)

        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", AsyncMock(side_effect=list(zip(streams, writers)))), \
             patch("phone.receiver.read_frame", fast_frame), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            task = asyncio.create_task(receive(args, statistics))
            try:
                yield task, statistics, output, errors, writers
            finally:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    def subscribed_stream(self):
        stream = asyncio.StreamReader()
        stream.feed_data(encode_frame(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")))
        return stream

    async def test_cold_start_waits_beyond_frame_deadline_without_reconnecting(self):
        stream = self.subscribed_stream()
        async with self.running_receiver([stream]) as (task, statistics, output, errors, _):
            # Scaled five-second frame deadline is 0.1 s; BLE is still starting.
            await asyncio.sleep(0.25)
            self.assertEqual(statistics["reconnects"], 0, errors.getvalue())
            stream.feed_data(encode_frame(result()))
            self.assertEqual(await asyncio.wait_for(task, 0.3), dict(received=1, reconnects=0))
            self.assertEqual(json.loads(output.getvalue()), result())

    async def test_later_clean_idle_resumes_on_same_subscription(self):
        stream = self.subscribed_stream(); stream.feed_data(encode_frame(result()))
        async with self.running_receiver([stream], count=2) as (task, statistics, output, _, _):
            await asyncio.sleep(0.25)
            self.assertEqual(statistics, dict(received=1, reconnects=0))
            next_result = dict(result(), seq=4, result_id='1:4294967295:4', gesture='CHOP', confidence=0.41)
            stream.feed_data(encode_frame(next_result))
            await asyncio.wait_for(task, 0.3)
            self.assertEqual([json.loads(line) for line in output.getvalue().splitlines()], [result(), next_result])

    async def test_first_result_clean_idle_has_no_frame_deadline(self):
        stream = self.subscribed_stream()
        async with self.running_receiver([stream]) as (_, statistics, _, _, writers):
            await asyncio.sleep(0.7)
            self.assertFalse(writers[0].closing.is_set())
            self.assertEqual(statistics, dict(received=0, reconnects=0))

    async def test_partial_result_still_expires_after_long_clean_idle(self):
        stream = self.subscribed_stream()
        async with self.running_receiver([stream]) as (_, statistics, _, _, writers):
            await asyncio.sleep(0.25)
            stream.feed_data(b'\x00')
            await asyncio.wait_for(writers[0].closing.wait(), 0.3)
            self.assertEqual(statistics, dict(received=0, reconnects=1))

    async def test_subscribed_response_retains_frame_deadline(self):
        stream = asyncio.StreamReader()
        async with self.running_receiver([stream]) as (_, statistics, _, _, writers):
            await asyncio.wait_for(writers[0].closing.wait(), 0.4)
            self.assertEqual(statistics, dict(received=0, reconnects=1))

    async def test_invalid_result_does_not_consume_startup_grace(self):
        first = self.subscribed_stream()
        # A result from a service still on protocol version 2.
        invalid = result(); invalid["v"] = 2
        first.feed_data(encode_frame(invalid))
        second = self.subscribed_stream()
        async with self.running_receiver([first, second]) as (task, statistics, output, errors, _):
            # The invalid result forces reconnect. The replacement subscription
            # is still allowed to wait for its first valid result beyond 0.1 s.
            await asyncio.sleep(0.8)
            self.assertEqual(statistics, dict(received=0, reconnects=1), errors.getvalue())
            second.feed_data(encode_frame(result()))
            self.assertEqual(await asyncio.wait_for(task, 0.3), dict(received=1, reconnects=1))
            self.assertEqual(json.loads(output.getvalue()), result())

    async def test_reconnect_after_valid_result_also_allows_clean_idle(self):
        first = self.subscribed_stream(); first.feed_data(encode_frame(result())); first.feed_eof()
        second = self.subscribed_stream()
        async with self.running_receiver([first, second], count=0) as (_, statistics, _, _, writers):
            # First connection ends at EOF; the replacement remains subscribed.
            await asyncio.sleep(0.85)
            self.assertFalse(writers[1].closing.is_set())
            self.assertEqual(statistics, dict(received=1, reconnects=1))


class ReceiverDurationTests(unittest.TestCase):
    def test_cli_duration_bounds_first_result_idle_and_closes_connection(self):
        from phone.receiver import main
        writers = []

        async def open_stream(*args, **kwargs):
            stream = asyncio.StreamReader()
            stream.feed_data(encode_frame(dict(v=1, type="SUBSCRIBED", session_id="ltc-comms")))
            writer = ClosingWriter(stall=False)
            writers.append(writer)
            return stream, writer

        errors = io.StringIO()
        with patch("phone.receiver.tls_context", return_value=object()), \
             patch("phone.receiver.asyncio.open_connection", open_stream), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
            exit_code = main(["--ca", "unused", "--count", "1", "--duration", "0.05"])
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(errors.getvalue().splitlines()[-1]), dict(received=0, reconnects=0))
        self.assertEqual(len(writers), 1)
        self.assertTrue(writers[0].closed)


class TlsReceiverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        from tools.generate_comms_pki import generate_pki
        cls.directory = tempfile.TemporaryDirectory(prefix="comms-phone-test-")
        cls.folder = Path(cls.directory.name)
        generate_pki(cls.folder / "pki")
        generate_pki(cls.folder / "wrong")
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cls.folder / "pki/server-cert.pem", cls.folder / "pki/server-key.pem")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    async def test_actual_tls_receiver_recovers_and_prints_result(self):
        attempts = []
        handlers = set()
        async def handle(reader, writer):
            task = asyncio.current_task(); handlers.add(task)
            try:
                attempts.append(await read_frame(reader))
                # SUBSCRIBED stays at version 1; a version-3 envelope forces one reconnect.
                writer.write(encode_frame(dict(v=3 if len(attempts) == 1 else 1, type="SUBSCRIBED", session_id="ltc-comms")))
                if len(attempts) > 1:
                    writer.write(encode_frame(result()))
                await writer.drain()
                await reader.read()
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass
                handlers.discard(task)
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=self.server_context)
        args = SimpleNamespace(ca=self.folder / "pki/ca-cert.pem", port=server.sockets[0].getsockname()[1],
                               session="ltc-comms", count=1, duration=4)
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                summary = await asyncio.wait_for(receive(args), 5)
            self.assertEqual(json.loads(output.getvalue()), result())
            self.assertEqual(summary, dict(received=1, reconnects=1))
            self.assertEqual(attempts, [dict(v=1, type="SUBSCRIBE", session_id="ltc-comms")] * 2)
        finally:
            server.close(); await server.wait_closed()
            if handlers:
                await asyncio.gather(*handlers)

    async def test_actual_tls_rejects_wrong_ca_and_wrong_name(self):
        async def handle(reader, writer):
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=self.server_context)
        port = server.sockets[0].getsockname()[1]
        try:
            for ca, identity in [("wrong", "ultra96.week7.internal"), ("pki", "wrong-host.internal")]:
                with self.subTest(ca=ca, identity=identity), self.assertRaises(ssl.SSLCertVerificationError):
                    await asyncio.open_connection("127.0.0.1", port, ssl=tls_context(self.folder / ca / "ca-cert.pem"), server_hostname=identity)
        finally:
            server.close(); await server.wait_closed()


if __name__ == "__main__":
    unittest.main()
