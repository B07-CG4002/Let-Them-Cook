import asyncio
import os
import unittest
from types import SimpleNamespace
from laptop.windows_pairing import pair_custom, validate_pin, check_bond


class PairingTests(unittest.IsolatedAsyncioTestCase):
    def test_pin_is_six_digits_only(self):
        self.assertEqual(validate_pin("000123"), "000123")
        for pin in ["12345", "1234567", "12 345", None, 123456]:
            with self.assertRaises(ValueError): validate_pin(pin)

    def test_unpaired_or_weak_bond_rejected(self):
        for paired, level in [(False, 3), (True, 2), (True, 0)]:
            with self.assertRaises(RuntimeError):
                check_bond(SimpleNamespace(is_paired=paired, protection_level=level))
        check_bond(SimpleNamespace(is_paired=True, protection_level=3))

    @unittest.skipUnless(os.name == "nt", "WinRT pairing boundary")
    async def test_provide_pin_requires_authenticated_protection_and_removes_handler(self):
        from winrt.windows.devices.enumeration import DevicePairingKinds, DevicePairingProtectionLevel, DevicePairingResultStatus
        class Args:
            pairing_kind = DevicePairingKinds.PROVIDE_PIN
            accepted = None
            def get_deferral(self): return SimpleNamespace(complete=lambda: None)
            def accept_with_pin(self, value): self.accepted = value
        event = Args()
        class Custom:
            handler = None
            removed = False
            def add_pairing_requested(self, h): self.handler = h; return 7
            def remove_pairing_requested(self, token): self.removed = token == 7
            async def pair_with_protection_level_async(self, kind, level):
                self.kind, self.level = kind, level
                self.handler(self, event)
                for _ in range(10):
                    await asyncio.sleep(0)
                    if event.accepted: break
                return SimpleNamespace(status=DevicePairingResultStatus.PAIRED)
        custom = Custom()
        await pair_custom(custom, lambda: asyncio.sleep(0, result="000123"))
        self.assertEqual(event.accepted, "000123")
        self.assertEqual(custom.kind, DevicePairingKinds.PROVIDE_PIN)
        self.assertEqual(custom.level, DevicePairingProtectionLevel.ENCRYPTION_AND_AUTHENTICATION)
        self.assertTrue(custom.removed)
