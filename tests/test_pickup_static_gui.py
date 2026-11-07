import unittest

from tools.pickup_static_gui import STATIC_VARIANTS, variant_note


class PickupStaticGuiTests(unittest.TestCase):
    def test_both_variants_keep_pickup_as_model_class(self):
        self.assertEqual(set(STATIC_VARIANTS), {"thumb straight", "thumb bent"})
        for choice in STATIC_VARIANTS:
            self.assertTrue(variant_note(choice).startswith("PICKUP;"))
            self.assertIn("stationary no-lift closure", variant_note(choice))

    def test_unknown_variant_is_rejected(self):
        with self.assertRaises(ValueError):
            variant_note("unknown")


if __name__ == "__main__":
    unittest.main()
