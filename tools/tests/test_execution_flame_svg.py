import importlib.util
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('flame_svg', Path(__file__).parents[2]/'etm/tools/flame_svg.py')
flame = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flame)


def instruction(seq, path):
    return {'kind':'instruction','seq':seq,'callPath':path,'function':path[-1], 'executionState':'executed'}


class ExactFlameTests(unittest.TestCase):
    def test_weighted_layout_preserves_ratios_and_escapes_full_names(self):
        analysis={'flame':{'name':'root','value':100,'children':[
            {'name':'long_child<&>','value':1,'children':[]},
            {'name':'large','value':99,'children':[]}]}}
        svg, _ = flame.weighted_document(analysis,'test')
        ns={'s':'http://www.w3.org/2000/svg'}
        groups=ET.fromstring(svg).findall('.//s:g[@data-frame]',ns)
        widths={g.find('s:text',ns).text:float(g.find('s:rect',ns).attrib['width'])+2 for g in groups}
        self.assertAlmostEqual(widths['large']/widths['long_child<&>'],99,places=4)
        self.assertAlmostEqual(widths['root'],widths['large']+widths['long_child<&>'],places=3)
        zoom,_=flame.weighted_document(analysis,'test',root_path='0')
        self.assertEqual(len(ET.fromstring(zoom).findall('.//s:g[@data-frame]',ns)),1)
        with self.assertRaises(ValueError):flame.weighted_document(analysis,'test','cycles')
        with self.assertRaises(ValueError):flame.weighted_document(analysis,'test',root_path='-1')

    def test_export_uses_exact_events_and_rejects_invalid_ranges_and_frame_overflow(self):
        class View:
            def overview(self):return {'eventCount':3}
            def iter_events(self,start,end):
                self.bounds=(start,end)
                yield instruction(start,['main','exact<&>'])
            def window(self,*_):raise AssertionError('timeline buckets must not be consulted')
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'out.svg';view=View()
            info=flame.export_svg(view,path,1,2)
            self.assertEqual(view.bounds,(1,2));self.assertEqual(info['depth'],2)
            self.assertIn('exact&lt;&amp;&gt;',path.read_text(encoding='utf8'))
            for bounds in ((-1,1),(1,None),(1,3),(2,1)):
                with self.assertRaises(ValueError):flame.export_svg(view,path,*bounds)
            with patch.object(flame,'MAX_FRAMES',1):
                with self.assertRaises(ValueError):flame.export_svg(view,path,1,2)

    def test_changed_ancestor_cannot_merge_equal_child_names(self):
        events = [instruction(0,['main','A','shared']), instruction(1,['main','B','shared']),
                  instruction(2,['main','B','shared'])]
        frames, counts, depth = flame.make_frames(events)
        shared = [f for f in frames if f['name']=='shared']
        self.assertEqual([(f['start'],f['end']) for f in shared],[(0,0),(1,2)])
        self.assertEqual((counts['instructions'],depth),(3,3))

    def test_event_breaks_continuity_without_filling_a_fake_path(self):
        events = [instruction(7,['main','A']),{'kind':'event','seq':8},instruction(9,['main','A'])]
        frames, _, _ = flame.make_frames(events)
        self.assertEqual([(f['start'],f['end']) for f in frames if f['name']=='main'],[(7,7),(9,9)])
        with self.assertRaises(ValueError):
            flame.make_frames([instruction(0,['main','多段调用（放大查看）'])])

    def test_minimum_uniform_scale_fits_every_full_name_and_preserves_nesting(self):
        events = [instruction(0,['main','wide_long_function_name']),instruction(1,['main']),instruction(2,['main','x'])]
        frames, _, depth = flame.make_frames(events)
        svg, info = flame.svg_document('test',0,2,frames,depth,lambda s:len(s)*8)
        self.assertEqual(info['pixelsPerSeq'],len('wide_long_function_name')*8+16)
        root = ET.fromstring(svg)
        ns = {'s':'http://www.w3.org/2000/svg'}
        groups = root.findall('.//s:g[@data-frame]',ns)
        self.assertEqual(len(groups),len(frames))
        for f,g in zip(frames,groups):
            text = g.find('s:text',ns)
            rect = g.find('s:rect',ns)
            self.assertEqual(text.text,f['name'])
            self.assertLessEqual(float(text.attrib['textLength'])+12,float(rect.attrib['width']))
        main = groups[0].find('s:rect',ns)
        child = groups[1].find('s:rect',ns)
        self.assertEqual(main.attrib['x'],child.attrib['x'])
        self.assertGreater(float(main.attrib['width']),float(child.attrib['width']))


if __name__ == '__main__':
    unittest.main()
