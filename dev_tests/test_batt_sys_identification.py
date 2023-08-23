import unittest
from batt_sys_identification.battery_identification import BatteryParams
import pandas as pd
print('imports done')

data = pd.read_csv('../batt_sys_identification/batt_iden_test_data_W10_2.csv')


class MyTestCase(unittest.TestCase):

    """
    Test module for battery system identification module.
    Must pass before any changes are pushed to main.
    """

    def test_initialization(self):
        module = BatteryParams(data)
        self.assertTrue(module is not None)

    def test_ga(self):
        module = BatteryParams(data)
        module.run_sys_identification()
        self.assertEqual(len(module.params), 7)
        self.assertEqual(len(module.params_uncorr), 7)


if __name__ == '__main__':
    unittest.main()
