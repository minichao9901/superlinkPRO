"""便携入口的默认引擎及显式对照选项，不依赖硬件或开发机环境。"""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('portable_engine',ROOT/'bridge/etm/etm_bridge.py')
bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)


class EngineTests(unittest.TestCase):
    def test_missing_or_old_config_defaults_to_native(self):
        for config in ({},{'port':8878},{'engine':'native'}):
            with self.subTest(config=config),patch.dict(os.environ,{},clear=True):
                self.assertEqual(bridge.configure_engine(None,config),'native')
                self.assertEqual(os.environ['ETM_STORE_ENGINE'],'native')

    def test_inherited_debug_flags_cannot_disable_optimizations(self):
        inherited=dict(ETM_STORE_WRITE='0',ETM_STORE_ENGINE='python',
                       ETM_DECODE_ENGINE='legacy',ETM_NATIVE_FORMAT='text',
                       ETM_STORE_ENGINE_FALLBACK='1')
        with patch.dict(os.environ,inherited,clear=True):
            bridge.configure_engine(None,{})
            self.assertEqual(dict(os.environ),dict(ETM_STORE_WRITE='1',ETM_STORE_ENGINE='native',
                ETM_DECODE_ENGINE='fast',ETM_NATIVE_FORMAT='binary',ETM_STORE_ENGINE_FALLBACK='0'))

    def test_explicit_python_config_and_command_line_precedence(self):
        for cli,config,expected in [(None,{'engine':'python'},'python'),
                ('native',{'engine':'python'},'native'),('python',{'engine':'native'},'python')]:
            with self.subTest(cli=cli,config=config),patch.dict(os.environ,{},clear=True):
                self.assertEqual(bridge.configure_engine(cli,config),expected)
                self.assertEqual(os.environ['ETM_STORE_ENGINE'],expected)

    def test_auto_selects_native_without_silent_fallback(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertEqual(bridge.configure_engine('auto',{}),'native')
            self.assertEqual(os.environ['ETM_STORE_ENGINE_FALLBACK'],'0')

    def test_invalid_config_is_rejected_before_changing_environment(self):
        for value in (None,'',False,1,'wrong'):
            with self.subTest(value=value),patch.dict(os.environ,{},clear=True):
                with self.assertRaises(ValueError):bridge.configure_engine(None,{'engine':value})
                self.assertNotIn('ETM_STORE_ENGINE',os.environ)


if __name__=='__main__':unittest.main()
