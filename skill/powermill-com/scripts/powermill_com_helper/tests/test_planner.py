from pathlib import Path
import itertools,json,random,tempfile,unittest
from powermill_com_helper.planner import plan_reorder,apply_move,create_bundle,target_order,validate_order_check,check_observed_order,capture_sort_state,replan_bundle
from powermill_com_helper.core import Executor,HelperError,NeedsReconciliation,digest
from .test_executor import GUARD,Fake
import copy,re,xml.etree.ElementTree as ET

def catalog(uids):return [{'uid':i,'position':[float(i),0.,0.],'layer':1,'lp':80} for i in uids]
def layer(initial,target):return {'name':'TEST_SORT','entity_id':'OWNED_ID','initial':initial,'target':target,'catalog':catalog(initial)}

class OrderFake(Fake):
    def __init__(self,order,refs):super().__init__();self.order=list(order);self.refs={x['uid']:copy.deepcopy(x) for x in refs};self.partial=False
    def read_xml(self,expressions,guard):
        result=[]
        for expression in expressions:
            if expression.endswith('.ID'):result.append('<string>OWNED_ID</string>');continue
            if expression.startswith('toolpath_component_count'):result.append('<int>'+str(len(self.order))+'</int>');continue
            indices=[int(x) for x in re.findall(r'\),(\d+),0\)',expression)]
            root=ET.Element('array')
            for index in indices:
                ref=self.refs[self.order[index]];m=ET.SubElement(root,'map')
                if 'segment_get_point' in expression:
                    for field,values in [('Position',ref['position']),('ToolAxis',ref.get('axis',[0,0,1]))]:
                        vector=ET.SubElement(m,field)
                        for value in values:ET.SubElement(vector,'real').text=str(value)
                else:
                    for key,value in ref.get('head_pp',{'_LAYER':ref['layer'],'udp_LaserPower':ref['lp']}).items():ET.SubElement(m,key).text=str(value)
            result.append(ET.tostring(root,encoding='unicode'))
        return result
    def execute(self,op,guard):
        self.calls.append(op['id']);self.order=list(op['postchecks'][0]['expected'])
        if self.partial:raise RuntimeError('Interrupted after reorder, before form accept/receipt')
        receipt=op['postchecks'][1];Path(receipt['path']).write_text(receipt['token'])
        return {'code':0,'dispatched':True,'output':op['completion_marker'],'seconds':.01}

class PlannerTests(unittest.TestCase):
    def test_all_small_permutations_and_random_larger_orders(self):
        target=list(range(5))
        for initial in itertools.permutations(target):
            current=list(initial)
            for a,b in plan_reorder(current,target):current=apply_move(current,a,b)
            self.assertEqual(current,target)
        rng=random.Random(2023)
        for n in (10,50,166,300):
            original=list(range(n));rng.shuffle(original);target=list(range(n));rng.shuffle(target)
            current=original
            for a,b in plan_reorder(original,target):current=apply_move(current,a,b)
            self.assertEqual(current,target)
    def test_already_sorted_needs_no_moves(self):self.assertEqual(plan_reorder([1,2],[1,2]),[])
    def test_missing_or_ambiguous_identities_are_rejected(self):
        with self.assertRaises(HelperError):plan_reorder([1,1],[1,2])
        with self.assertRaises(HelperError):plan_reorder([1,2],[1,3])
        c={'kind':'order','name':'X','entity_id':'ID','expected':[1,2],'catalog':[{'uid':x,'position':[0,0,0],'lp':80,'layer':1} for x in (1,2)]}
        with self.assertRaises(HelperError):validate_order_check(c)
    def test_bundle_has_continuous_checkpoints_and_receipts(self):
        with tempfile.TemporaryDirectory() as d:
            destination=Path(d)/'bundle';summary=create_bundle([layer([4,3,2,1,0],list(range(5)))],GUARD,destination,max_moves=1)
            plan=json.loads((destination/'plan.json').read_text())
            self.assertTrue(plan['offline_only']);previous=[4,3,2,1,0]
            for op in plan['operations']:
                self.assertEqual(op['prechecks'][0]['expected'],previous);previous=op['postchecks'][0]['expected']
                body=Path(op['artifact']['path']).read_text(encoding='cp1251')
                self.assertLess(body.index('FORM ACCEPT TPLIST'),body.index('FILE WRITE'))
                self.assertFalse(Path(op['postchecks'][1]['path']).exists())
            self.assertEqual(previous,list(range(5)))
            with self.assertRaises(HelperError):Executor(plan,Path(d)/'run',Fake()).run(allow_write=True)
    def test_explicit_policy_closed_first_roles_and_pair_order(self):
        records=[{'uid':'rib2','kind':'open','layer':1,'rotation':0,'priority':4,'pair':1,'sequence':2},
                 {'uid':'closed','kind':'closed','layer':1,'z':2},
                 {'uid':'rib1','kind':'open','layer':1,'rotation':0,'priority':4,'pair':1,'sequence':1}]
        self.assertEqual(target_order(records,{'closed_keys':['z'],'open_keys':['rotation','priority','pair','sequence']}),['closed','rib1','rib2'])
    def test_observed_order_and_changed_pp_or_entity_rejected(self):
        l=layer([3,1,2],[1,2,3]);c={'kind':'order','name':l['name'],'entity_id':l['entity_id'],'catalog':l['catalog'],'expected':l['initial']}
        fake=OrderFake(l['initial'],l['catalog']);self.assertTrue(check_observed_order(c,fake,GUARD))
        fake.order=[1,2,3];self.assertFalse(check_observed_order(c,fake,GUARD))
        c['expected']=fake.order;fake.refs[2]['lp']=82
        with self.assertRaises(HelperError):check_observed_order(c,fake,GUARD)
    def test_no_reconcile_without_receipt_even_if_target_order_is_visible(self):
        with tempfile.TemporaryDirectory() as d:
            l=layer([3,2,1],[1,2,3]);destination=Path(d)/'bundle';create_bundle([l],GUARD,destination,enable_execution=True)
            p=json.loads((destination/'plan.json').read_text());fake=OrderFake(l['initial'],l['catalog']);fake.partial=True
            e=Executor(p,Path(d)/'run',fake,lock_dir=Path(d)/'locks')
            with self.assertRaises(RuntimeError):e.run(allow_write=True)
            with self.assertRaises(NeedsReconciliation):e.run(allow_write=True)
            self.assertEqual(len(fake.calls),1)
    def test_partial_sort_can_be_replanned_from_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            l=layer([4,3,2,1,0],list(range(5)));fake=OrderFake([3,2,1,4,0],l['catalog']);data={'schema':1,'guard':GUARD,'layers':[l]}
            observation=capture_sort_state(data,fake)
            destination=Path(d)/'continuation';replan_bundle(data,observation,destination,enable_execution=True,max_moves=1)
            p=json.loads((destination/'plan.json').read_text());e=Executor(p,Path(d)/'run',fake,lock_dir=Path(d)/'locks')
            e.run(allow_write=True);self.assertEqual(fake.order,list(range(5)))
            calls=len(fake.calls);e.run(allow_write=True);self.assertEqual(len(fake.calls),calls)
