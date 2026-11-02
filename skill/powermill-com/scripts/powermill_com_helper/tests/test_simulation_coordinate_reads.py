"""Documented pose getters may be read without entering PRINT command context."""
from pathlib import Path
import re
import unittest

from powermill_com_helper.core import HelperError, READ_FUNCTIONS, validate_read_expression


class SimulationCoordinateReadTests(unittest.TestCase):
    def test_only_documented_pose_getters_are_accepted(self):
        for name in ('tool_tip_position', 'tool_axis_direction', 'orientation_vector_direction'):
            for argument in ('', "entity('workplane','sim 0 0 -20')"):
                expression = name + '(' + argument + ')'
                with self.subTest(expression=expression):
                    self.assertEqual(validate_read_expression(expression), expression)
        for expression in ('tool_tip_position(point_add_parameter(1,2))',
                           'tool_tip_position();evil()', 'tool_tip_position_unknown()',
                           'machine_set_position(1)',
                           'tool_axis_direction(select_components(1))'):
            with self.subTest(expression=expression):
                with self.assertRaises(HelperError):
                    validate_read_expression(expression)

    def test_python_and_worker_allowlists_still_match(self):
        worker = Path(__file__).resolve().parents[1] / 'worker.ps1'
        declaration = re.search(r"\$allowed=@\((.*?)\)", worker.read_text('utf-8'))[1]
        names = re.findall(r"'([^']+)'", declaration)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), READ_FUNCTIONS)


if __name__ == '__main__':
    unittest.main()
