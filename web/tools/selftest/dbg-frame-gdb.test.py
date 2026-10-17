"""Test GDB collector helpers with API doubles; this is not a live GDB run."""
import pathlib
import json
import sys
import types
import unittest


class GdbError(Exception):
    pass


fake = types.ModuleType('gdb')
fake.error = GdbError
for i, name in enumerate(['ARRAY', 'STRUCT', 'UNION', 'INT', 'ENUM', 'PTR', 'BOOL']):
    setattr(fake, 'TYPE_CODE_' + name, i)
sys.modules['gdb'] = fake
namespace = {}
source = pathlib.Path(__file__).with_name('dbg-frame-gdb.py').read_text()
exec(compile(source.rsplit('\nmain()', 1)[0], 'dbg-frame-gdb.py', 'exec'), namespace)


class Type:
    def __init__(self, code, fields=(), bounds=(0, 1)):
        self.code, self.members, self.bounds = code, fields, bounds

    def strip_typedefs(self):
        return self

    def fields(self):
        return [types.SimpleNamespace(name=n) for n in self.members]

    def range(self):
        return self.bounds


class Value:
    def __init__(self, value, typ=None, optimized=False, address=None):
        self.value = value
        self.type = typ or Type(fake.TYPE_CODE_INT)
        self.is_optimized_out, self.address = optimized, address

    def __int__(self):
        return int(self.value)

    def __getitem__(self, key):
        return self.value[key.name if hasattr(key, "name") else key]


class Block(list):
    is_global = False
    is_static = False

    def __init__(self, symbols=(), parent=None):
        super().__init__(symbols)
        self.superblock = parent


class Frame:
    def __init__(self, block, values, name='engine_frame_recursive', older=None):
        self._block, self.values, self._name, self._older = block, values, name, older

    def block(self):
        return self._block

    def read_var(self, symbol):
        value = self.values[id(symbol)]
        if isinstance(value, Exception):
            raise value
        return value

    def name(self):
        return self._name

    def older(self):
        return self._older

    def pc(self):
        return 0x08001001

    def read_register(self, name):
        assert name == 'sp'
        return 0x20001000


def symbol(name, argument=False):
    return types.SimpleNamespace(name=name, is_argument=argument, is_variable=not argument)


class CollectorTests(unittest.TestCase):
    def test_python2_compatible_json_and_byte_helpers(self):
        value = {'quote': 'a"b', 'slash': 'a\\b', 'control': 'one\ntwo', 'count': 7,
                 'flags': [True, False, None], 'nested': {'unicode': 'F103'}}
        self.assertEqual(json.loads(namespace['json_dumps'](value)), value)
        self.assertEqual(namespace['from_hex']('01020304'), bytes.fromhex('01020304'))
        self.assertEqual(namespace['u32le'](bytes.fromhex('78563412')), 0x12345678)

    def test_aggregate_and_signed(self):
        array = Value([Value(21), Value(34)], Type(fake.TYPE_CODE_ARRAY))
        struct = Value({'tag': Value(0x12345678), 'signed_value': Value(-17), 'pair': array},
                       Type(fake.TYPE_CODE_STRUCT, ['tag', 'signed_value', 'pair']))
        result = namespace['scalar_fields'](struct)
        self.assertEqual(result, [{'path': 'tag', 'value': '305419896'},
                                  {'path': 'signed_value', 'value': '-17'},
                                  {'path': 'pair[0]', 'value': '21'},
                                  {'path': 'pair[1]', 'value': '34'}])

    def test_shadow_and_unavailable(self):
        outer, inner, arg, optimized, broken = [symbol(n, n == 'arg') for n in ['shadow', 'shadow', 'arg', 'optimized', 'broken']]
        block = Block([inner, optimized, broken], Block([outer, arg]))
        values = {id(outer): Value(60, address=0x20001000), id(inner): Value(70), id(arg): Value(23),
                  id(optimized): Value(0, optimized=True), id(broken): GdbError('register unavailable')}
        result = namespace['variables'](Frame(block, values))
        self.assertEqual([r['occurrence'] for r in result if r['name'] == 'shadow'], [0, 1])
        self.assertEqual([r['fields'][0]['value'] for r in result if r['name'] == 'shadow'], ['60', '70'])
        self.assertEqual(result[0]['address'], 0x20001000)
        self.assertTrue(result[1]['argument'])
        self.assertEqual(result[-2]['status'], 'unavailable')
        self.assertEqual(result[-1]['status'], 'unavailable')

    def test_capture_boundary_and_thumb(self):
        main = Frame(Block(), {}, 'main')
        caller = Frame(Block(), {}, older=main)
        leaf = Frame(Block(), {}, 'engine_frame_leaf', caller)
        fake.newest_frame = lambda: leaf
        result = namespace['capture']()
        self.assertEqual([r['name'] for r in result], ['engine_frame_leaf', 'engine_frame_recursive'])
        self.assertEqual(result[0]['pc'], 0x08001000)
        self.assertEqual(result[0]['sp'], 0x20001000)

    def test_fail_on_missing_coverage(self):
        with self.assertRaises(RuntimeError):
            namespace['scalar_fields'](Value(1, Type(999)))
        with self.assertRaises(RuntimeError):
            namespace['scalar_fields'](Value([], Type(fake.TYPE_CODE_ARRAY, bounds=(0, 100))))
        with self.assertRaises(RuntimeError):
            namespace['scalar_fields'](Value({}, Type(fake.TYPE_CODE_STRUCT, [None])))


unittest.main()
