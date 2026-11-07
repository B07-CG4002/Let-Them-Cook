import unittest

from src.hand_ai_flex3.wide_training import validate_ratios
from src.hand_ai_flex3.core import LABELS


class CombinedSessionTrainingTests(unittest.TestCase):
    def rows(self, combined):
        result=[]
        for gesture in LABELS:
            for split,count in (('train',30),('validation',4),('test',6)):
                for trial in range(count):
                    result.append(dict(person='P02',session='same-guided-session',
                                       session_scope='combined',split=split,
                                       device_id=1,gesture=gesture,trial=trial+1))
        return result

    def test_combined_guided_session_can_use_all_splits(self):
        self.assertEqual(validate_ratios(self.rows(True))['PICKUP'],
                         {'train':30,'validation':4,'test':6})

    def test_unmarked_session_crossing_splits_is_still_rejected(self):
        rows=[dict(row,session_scope=None) for row in self.rows(False)]
        with self.assertRaisesRegex(ValueError,'crosses dataset splits'):
            validate_ratios(rows)


if __name__=='__main__':
    unittest.main()
