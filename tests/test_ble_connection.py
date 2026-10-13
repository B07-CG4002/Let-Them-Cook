import asyncio
import sys
import unittest
from unittest.mock import patch
from laptop.windows_pairing import prompt_pin
from laptop.ble_connection import make_ble_client


class ConnectionRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_windows_connection_forces_real_gatt_discovery(self):
        with patch("laptop.ble_connection.BleakClient") as client:
            make_ble_client("device", disconnected_callback=None)
            self.assertEqual(client.call_args.kwargs["winrt"]["use_cached_services"], False)

    @unittest.skipUnless(sys.platform == "win32", "Windows console")
    async def test_pairing_prompt_cancels_without_an_executor_thread(self):
        with patch("msvcrt.kbhit", return_value=False), patch("sys.stdin.isatty", return_value=True):
            task = asyncio.create_task(prompt_pin())
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, .2)
