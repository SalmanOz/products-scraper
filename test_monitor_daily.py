from datetime import datetime
from unittest import TestCase
from unittest.mock import Mock

from monitor_daily import ZONE, daily_history, fingerprint, snapshot


class MonitorDailyTests(TestCase):
    def test_previous_day_cannot_contribute_to_current_snapshot(self):
        queue = Mock()
        queue.clock.return_value = datetime(2026, 11, 9, 14, tzinfo=ZONE).timestamp()
        queue.fresh_offers.return_value = []
        result = snapshot(queue, {'4711081951506'})
        queue.fresh_offers.assert_called_once_with(
            'monitor', since=int(datetime(2026, 11, 9, tzinfo=ZONE).timestamp()), now=int(queue.clock()))
        self.assertEqual(result['day'], '2026-11-09')
        self.assertEqual(result['paired'], 0)

    def test_daily_gate_rejects_gaps_and_missed_targets(self):
        gtins = {str(number) for number in range(100)}

        def report(day, count=80):
            return {'day': f'2026-11-{day:02}', 'captured_at': f'2026-11-{day:02}T22:00:00+03:00',
                    'cohort': fingerprint(gtins), 'models': 100, 'priced': 100,
                    'priced_gtins': sorted(gtins), 'paired': count, 'paired_gtins': sorted(gtins)[:count]}

        self.assertEqual(daily_history(gtins, [report(7), report(8), report(9)])['status'], 'daily_target_met')
        self.assertEqual(daily_history(gtins, [report(6), report(8), report(9)])['consecutive_days_at_target'], 2)
        self.assertEqual(daily_history(gtins, [report(7), report(8, 79), report(9)])['consecutive_days_at_target'], 1)
        with self.assertRaisesRegex(ValueError, 'distinct local dates'):
            daily_history(gtins, [report(9), report(9)])
