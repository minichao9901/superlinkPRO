"""周期计数聚合的单测（合成事件，不需要硬件）。

覆盖：
  1. 自身周期 / 包含周期 / 占比 / CPI 的计算；
  2. 排序与 top 列表按周期降序；
  3. 热点行带周期；
  4. **没有周期数据时的降级**（ETMv3/F103 或未启用）——必须 timeFlameAvailable=False 且给出原因。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from flow_analysis import analyze_calls  # noqa: E402


def ins(pc, fn, cycles=0, executed=True, line=1):
    return dict(kind='instruction', seq=0, offset=0, pc=pc, size=2, executed=executed,
                function=fn, file='a.c', line=line, cycleCount=cycles)


class CycleAggregation(unittest.TestCase):
    def test_self_and_total_cycles(self):
        events = [ins(0x100, 'a', 0), ins(0x102, 'a', 100), ins(0x200, 'b', 50)]
        analysis = analyze_calls(events)
        timing = analysis['timing']

        self.assertTrue(timing['timeFlameAvailable'])
        self.assertEqual(timing['totalCycles'], 150)
        self.assertEqual(timing['cpi'], 50)          # 150 周期 / 3 条指令
        functions = {f['function']: f for f in analysis['functions']}
        self.assertEqual(functions['a']['selfCycles'], 100)
        self.assertEqual(functions['b']['selfCycles'], 50)
        self.assertAlmostEqual(functions['a']['cyclePercent'], 100 * 100 / 150, places=6)
        self.assertAlmostEqual(functions['b']['cyclePercent'], 100 * 50 / 150, places=6)

    def test_top_list_sorted_by_cycles(self):
        events = [ins(0x100, 'small', 10), ins(0x200, 'big', 900), ins(0x300, 'mid', 90)]
        timing = analyze_calls(events)['timing']
        self.assertEqual([f['function'] for f in timing['functions']], ['big', 'mid', 'small'])
        self.assertAlmostEqual(timing['functions'][0]['cyclePercent'], 90.0, places=6)

    def test_hotspot_carries_cycles(self):
        events = [ins(0x100, 'a', 7, line=11), ins(0x100, 'a', 5, line=11)]
        hotspots = analyze_calls(events)['hotspots']
        self.assertEqual(hotspots[0]['cycles'], 12)
        self.assertEqual(hotspots[0]['records'], 2)

    def test_flame_nodes_carry_cycles(self):
        events = [ins(0x100, 'a', 30), ins(0x104, 'a', 20)]
        flame = analyze_calls(events)['flame']

        def find(node, name):
            if node.get('name') == name:
                return node
            for child in node.get('children') or []:
                hit = find(child, name)
                if hit is not None:
                    return hit
            return None

        # 注意：栈底会有"采集前调用栈未知"这类合成帧，所以按名字**递归**找（serialize 后 children 是 list）
        node = find(flame, 'a')
        self.assertIsNotNone(node, '火焰图里应能找到函数 a 的节点')
        self.assertEqual(node['selfCycles'], 50)
        self.assertEqual(node['valueCycles'], 50)

    def test_degrades_without_cycles(self):
        """ETMv3 / 未启用周期计数：必须明确"没有时间基准"，不能编出 0 周期的时间。"""
        events = [ins(0x100, 'a', 0), ins(0x200, 'b', 0)]
        timing = analyze_calls(events)['timing']
        self.assertFalse(timing['timeFlameAvailable'])
        self.assertEqual(timing['totalCycles'], 0)
        self.assertIsNone(timing['cpi'])
        self.assertEqual(timing['functions'], [])
        self.assertIn('没有周期计数', timing['basis'])

    def test_unexecuted_instructions_not_counted_in_cpi(self):
        events = [ins(0x100, 'a', 40), ins(0x102, 'a', 0, executed=False)]
        timing = analyze_calls(events)['timing']
        self.assertEqual(timing['totalCycles'], 40)
        # CPI 的分母是"已证实执行"的指令数
        self.assertEqual(timing['cpi'], 40)


if __name__ == '__main__':
    unittest.main()
