import copy
import unittest

from powermill_com_helper.core import HelperError
from powermill_com_helper.robot_input import prepare_external_axis_input


def contract():
    return {'schema': 1, 'axes': [
        {'address':'A','type':'rotary','origin':[0,0,0],'direction':[0,0,1],'limits':[-180,180]},
        {'address':'R','type':'rotary','origin':[10,20,0],'direction':[0,0,-1],'limits':[0,360]}],
        'robot_chain':['A'], 'table_chain':['R'],
        'head':{'tcp':[10,20,0],'x':[1,0,0],'z':[0,0,1]},
        'source_frame':{'origin':[10,20,2],'basis':[[1,0,0],[0,1,0],[0,0,1]]},
        'region':{'origin':[10,20,0],'undo_axes':[],'min':[-10,-10],'max':[10,10]},
        'orientation':{'reference_X':[1,0,0],'max_X_degrees':5}}


ATTACH = {'origin':[10,20,0], 'x':[1,0,0], 'z':[0,0,1]}
SOURCE = [0,-5,3,0,0,1]


class RobotInputTests(unittest.TestCase):
    def test_offset_and_R_are_applied_before_table_attach_conversion(self):
        c = contract(); original = copy.deepcopy(c)
        result = prepare_external_axis_input(c, SOURCE, {'R':90}, ATTACH, motion_type='12')
        for value, expected in zip(result['world_TCP'], [5,20,5]):
            self.assertAlmostEqual(value, expected)
        for value, expected in zip(result['native_pose'][:3], [-5,0,5]):
            self.assertAlmostEqual(value, expected)
        self.assertIn('EXTERNALABS R90.000000000000', result['command'])
        self.assertTrue(result['native_validation_required'])
        self.assertEqual(c, original)

    def test_rotated_attach_basis_converts_vectors_and_position(self):
        attach = {'origin':[10,20,0], 'x':[0,1,0], 'z':[0,0,1]}
        result = prepare_external_axis_input(contract(), SOURCE, {'R':90}, attach, motion_type='12')
        for value, expected in zip(result['native_pose'], [0,5,5,0,0,1,0,-1,0]):
            self.assertAlmostEqual(value, expected)

    def test_exact_external_axis_set_and_limits(self):
        for values in ({}, {'R':90,'A':0}, {'R':-1}, {'R':361}, {'R':float('nan')}, {'R':True}):
            with self.subTest(values=values), self.assertRaises(HelperError):
                prepare_external_axis_input(contract(), SOURCE, values, ATTACH, motion_type='12')

    def test_geometry_regions_and_X_tolerance_fail_closed(self):
        for pose, attach in (([0,-50,3,0,0,1],ATTACH), ([0,-5,3,1,0,0],ATTACH),
                             (SOURCE,{'origin':[10,20,0],'x':[2,0,0],'z':[0,0,1]})):
            with self.subTest(pose=pose,attach=attach), self.assertRaises(HelperError):
                prepare_external_axis_input(contract(), pose, {'R':0}, attach, motion_type='12')
        c=contract();c['motion_regions']=[{'name':'cut','movetypes':['12'],'min':[-1,-1],'max':[1,1]}]
        for move in ('12','unknown'):
            with self.subTest(move=move), self.assertRaises(HelperError):
                prepare_external_axis_input(c, SOURCE, {'R':90}, ATTACH, motion_type=move)


if __name__ == '__main__':
    unittest.main()
