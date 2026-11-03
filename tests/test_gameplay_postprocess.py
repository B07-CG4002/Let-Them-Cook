import unittest

from src.hand_ai_flex3.gameplay_postprocess import ObjectActionGate, TemporalPostProcessor


class TemporalTests(unittest.TestCase):
    def test_pulse_requires_idle_then_two_of_three(self):
        p = TemporalPostProcessor()
        self.assertEqual(p.update(1, 'IDLE', .99, .0), [])
        self.assertTrue(p.update(1, 'IDLE', .99, .2))
        self.assertEqual(p.update(1, 'PICKUP', .99, .4), [])
        events = p.update(1, 'PICKUP', .99, .6)
        self.assertEqual(events[0]['gesture'], 'PICKUP')

    def test_pulse_cooldown_suppresses_duplicate(self):
        p = TemporalPostProcessor(pulse_cooldown=.5)
        p.update(1, 'IDLE', .99, 0)
        p.update(1, 'IDLE', .99, .2)
        p.update(1, 'PICKUP', .99, .4)
        self.assertTrue(p.update(1, 'PICKUP', .99, .6))
        p.update(1, 'IDLE', .99, .8)
        p.update(1, 'IDLE', .99, 1.0)
        self.assertEqual(p.update(1, 'PICKUP', .99, 1.1), [])


class ObjectTests(unittest.TestCase):
    def test_pot_cannot_be_thrown_but_can_be_poured(self):
        g = ObjectActionGate()
        g.set_target(1, 'pot')
        self.assertTrue(g.accept(1, 'PICKUP', 0).accepted)
        self.assertFalse(g.accept(1, 'THROW', .2).accepted)
        self.assertTrue(g.accept(1, 'POURING', .4).accepted)

    def test_ingredients_can_be_thrown(self):
        g = ObjectActionGate()
        g.set_target(1, 'whole ingredient')
        self.assertTrue(g.accept(1, 'PICKUP', 0).accepted)
        self.assertTrue(g.accept(1, 'THROW', .2).accepted)
        self.assertIsNone(g.held[1])

    def test_drop_requires_held_object(self):
        g = ObjectActionGate()
        self.assertFalse(g.accept(1, 'DROP', 0).accepted)

    def test_chop_requires_held_knife(self):
        g = ObjectActionGate()
        g.set_held(1, 'POT')
        self.assertFalse(g.accept(1, 'CHOP', .1).accepted)
        g.set_held(1, 'KNIFE')
        decision = g.accept(1, 'CHOP', .2)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reason, 'knife_chop_allowed')

    def test_wash_requires_both_hands_on_dirty_stack(self):
        g = ObjectActionGate(two_hand_window=.5)
        g.set_held(1, 'dirty plate stack')
        g.set_held(2, 'dirty plate stack')
        self.assertFalse(g.accept(1, 'WASH', 1.0).accepted)
        self.assertTrue(g.accept(2, 'WASH', 1.1).accepted)


if __name__ == '__main__':
    unittest.main()
