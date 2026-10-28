import unittest
from tools.hand_tonight import LiveConsole


def row(device=1,confidence=.9,elapsed=2.,gesture='PICKUP',kind='prediction'):
    return dict(type=kind,device_id=device,confidence=confidence,elapsed_s=elapsed,gesture=gesture,inference_ms=1.23)


class ConsoleTests(unittest.TestCase):
    def test_strict_threshold_and_no_rounding_before_decision(self):
        lines=[];console=LiveConsole(.7,lines.append)
        for confidence in (.2,.69999,.7):console.show(row(confidence=confidence))
        self.assertEqual(lines,[])
        console.show(row(confidence=.70001))
        self.assertEqual(len(lines),1)

    def test_hands_and_gesture_lengths_keep_columns_fixed(self):
        lines=[];console=LiveConsole(.7,lines.append)
        console.show(row(1,gesture='WASH'))
        console.show(row(2,gesture='POURING'))
        self.assertEqual(len(lines[0]),len(lines[1]))
        self.assertEqual(len(lines[0]),len(console.HEADER))
        for text in lines:
            self.assertEqual(text.index('PREDICT'),console.HEADER.index('TYPE'))
            self.assertNotIn('{',text)
        self.assertEqual(lines[0].index('0.9000'),lines[1].index('0.9000'))

    def test_rate_limit_is_per_hand_and_low_confidence_does_not_consume_slot(self):
        lines=[];console=LiveConsole(.7,lines.append)
        console.show(row(confidence=.1))
        console.show(row(confidence=.95))
        console.show(row(2,elapsed=2.1))
        console.show(row(1,elapsed=2.5))
        self.assertEqual(len(lines),2)
        console.show(row(1,elapsed=3.))
        self.assertEqual(len(lines),3)

    def test_confirmed_events_are_immediate_and_still_require_high_confidence(self):
        lines=[];console=LiveConsole(.7,lines.append)
        console.show(row())
        console.show(row(elapsed=2.1,kind='action'))
        console.show(row(elapsed=2.2,kind='state_start',gesture='WASH'))
        console.show(row(elapsed=2.3,kind='idle',gesture='IDLE'))
        self.assertEqual(len(lines),4)
        self.assertIn('ACCEPT',lines[1]);self.assertIn('START',lines[2]);self.assertIn('IDLE',lines[3])
        console.show(row(kind='action',confidence=.7));self.assertEqual(len(lines),4)

    def test_lifecycle_status_has_no_invented_confidence_or_idle(self):
        lines=[];console=LiveConsole(.7,lines.append)
        event=dict(type='state_end',device_id=1,elapsed_s=5.,gesture='WASH',reason='prediction_timeout')
        console.show(event)
        self.assertIn('END',lines[0]);self.assertIn('WASH',lines[0]);self.assertNotIn('IDLE',lines[0]);self.assertIn('-',lines[0])
        self.assertNotIn('confidence',event)

    def test_rows_are_never_modified_including_hidden_predictions(self):
        lines=[];console=LiveConsole(.7,lines.append)
        for kind,c in [('prediction',.2),('prediction',.9),('action',.9)]:
            event=row(kind=kind,confidence=c);previous=dict(event)
            console.show(event);self.assertEqual(event,previous)

    def test_explicit_threshold_still_matches_inference_policy(self):
        lines=[];console=LiveConsole(.8,lines.append)
        console.show(row(confidence=.75));self.assertEqual(lines,[])
        console.show(row(confidence=.81));self.assertEqual(len(lines),1)


if __name__=='__main__':unittest.main()
