import unittest

from src.hand_ai_flex3.gameplay_postprocess import ObjectActionGate, TemporalPostProcessor


class TemporalTests(unittest.TestCase):
    def test_first_pulse_and_rearm_sequence(self):
        p = TemporalPostProcessor(pulse_cooldown=.5)
        self.assertEqual(p.update(1, "PICKUP", .99, 0.0), [])
        self.assertEqual(p.update(1, "PICKUP", .99, 0.2)[0]["gesture"], "PICKUP")
        p.update(1, "IDLE", .99, 0.4)
        p.update(1, "IDLE", .99, 0.6)
        self.assertEqual(p.update(1, "DROP", .99, 0.8), [])
        self.assertEqual(p.update(1, "DROP", .99, 1.0)[0]["gesture"], "DROP")
        p.update(1, "IDLE", .99, 1.2)
        p.update(1, "IDLE", .99, 1.4)
        self.assertEqual(p.update(1, "PICKUP", .99, 1.6), [])
        self.assertEqual(p.update(1, "PICKUP", .99, 1.8)[0]["gesture"], "PICKUP")

    def test_cooldown_alone_blocks_second_pulse(self):
        p = TemporalPostProcessor(pulse_cooldown=.5, pulse_votes=1, idle_votes=1)
        self.assertEqual(p.update(1, "PICKUP", .99, 0.0)[0]["gesture"], "PICKUP")
        p.update(1, "IDLE", .99, 0.1)
        self.assertEqual(p.update(1, "DROP", .99, 0.2), [])
        self.assertEqual(p.update(1, "DROP", .99, 0.6)[0]["gesture"], "DROP")


class ObjectTests(unittest.TestCase):
    def test_pot_pours_into_clean_plate_but_clean_plate_cannot_pour(self):
        g = ObjectActionGate()
        g.set_held(1, "POT")
        self.assertTrue(g.accept(1, "POURING", .1, target_object="PLATE_CLEAN").accepted)
        g.set_held(1, "PLATE_CLEAN")
        decision = g.accept(1, "POURING", .2)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "clean_plate_is_pour_target")

    def test_unknown_object_is_rejected_without_crashing(self):
        g = ObjectActionGate()
        with self.assertRaises(ValueError):
            g.set_held(1, "EMPTY_BOWL")
        decision = g.accept(1, "DROP", .1, object_name="EMPTY_BOWL")
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, "unknown_object")

    def test_chop_requires_held_knife(self):
        g = ObjectActionGate()
        g.set_held(1, "POT")
        self.assertFalse(g.accept(1, "CHOP", .1).accepted)
        g.set_held(1, "KNIFE")
        decision = g.accept(1, "CHOP", .2)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, "knife_chop_allowed")

    def test_two_hand_wash_requires_both_hands(self):
        g = ObjectActionGate(require_two_hand_wash=True)
        g.set_held(1, "DIRTY_PLATE_STACK")
        g.set_held(2, "DIRTY_PLATE_STACK")
        self.assertFalse(g.accept(1, "WASH", 1.0).accepted)
        self.assertTrue(g.accept(2, "WASH", 1.1).accepted)

    def test_single_hand_wash_can_be_used_for_replay_only(self):
        g = ObjectActionGate()
        g.set_held(1, "DIRTY_PLATE_STACK")
        self.assertTrue(g.accept(1, "WASH", 1.0).accepted)


if __name__ == "__main__":
    unittest.main()

