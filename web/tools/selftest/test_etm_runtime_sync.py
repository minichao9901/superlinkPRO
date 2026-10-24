"""原生索引器回退回归：只对“缺 SQLite3/目标不存在”降级，真实错误必须失败。

审查发现 1：旧实现 catch 所有 (CalledProcessError, FileNotFoundError)，且不删除目标
快照里的旧 etm_index.exe，可能保留旧程序再生成新 provenance。这里离线覆盖：
  · 缺 SQLite3（CMake 不生成目标）→ 降级、删除旧 EXE、provenance 不含该文件并提示；
  · 缓存声称有 SQLite 但目标不存在（unknown target）→ 同样降级；
  · 真实编译错误 → SystemExit，且在写 provenance 之前中止、移除旧派生产物；
  · 构建成功 → 复制新索引器并进入 packagedHashes；
  · build-native 未配置 → 失败（配置缺失不算可选依赖）。

测试不依赖本机是否安装 SQLite3/CMake：runner 与源/快照目录都是临时构造的。

跑：python -X utf8 -m unittest discover -s web/tools/selftest -p "test_etm_runtime_sync.py" -q
"""
import importlib.util
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'dev' / 'sync-etm-runtime.py'


def load_module():
    spec = importlib.util.spec_from_file_location('sync_etm_runtime_under_test', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    """替代 subprocess.run：记录调用，按需抛错或在“构建”时产出索引器产物。"""

    def __init__(self, error=None, artifact=None):
        self.calls = []
        self.error = error
        self.artifact = artifact

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if self.error is not None:
            raise self.error
        if self.artifact is not None:
            Path(self.artifact).write_bytes(b'MZ fake indexer')
        return subprocess.CompletedProcess(command, 0, '', '')


def workspace(temp, sqlite=True, stale=True):
    temp = Path(temp)
    source, dest, root = temp / 'source', temp / 'dest', temp / 'root'
    (source / 'build-native').mkdir(parents=True)
    (dest / 'build-native').mkdir(parents=True)
    if sqlite is not None:
        include = 'C:/msys64/mingw64/include' if sqlite else 'SQLite3_INCLUDE_DIR-NOTFOUND'
        library = 'C:/msys64/mingw64/lib/libsqlite3.a' if sqlite else 'SQLite3_LIBRARY-NOTFOUND'
        (source / 'build-native' / 'CMakeCache.txt').write_text(
            f'SQLite3_INCLUDE_DIR:PATH={include}\nSQLite3_LIBRARY:FILEPATH={library}\n', encoding='utf8')
    if stale:
        (dest / 'build-native' / 'etm_index.exe').write_bytes(b'old indexer')
    (dest / 'build-native' / 'etm_decode.exe').write_bytes(b'decoder')
    (dest / 'tools').mkdir()
    (dest / 'tools' / 'server.py').write_bytes(b'# server\n')
    return source, dest, root


class NativeIndexerTests(unittest.TestCase):
    def test_identical_mapped_executable_needs_no_write_but_changes_must_publish(self):
        module=load_module()
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'new.exe';target=Path(temp)/'mapped.exe'
            source.write_bytes(b'same');target.write_bytes(b'same')
            with patch.object(module.shutil,'copyfile',side_effect=PermissionError('mapped')) as copy:
                module.copy_native_if_changed(source,target);copy.assert_not_called()
                source.write_bytes(b'changed')
                with self.assertRaises(PermissionError):module.copy_native_if_changed(source,target)
            self.assertEqual(target.read_bytes(),b'same')

    def test_missing_sqlite_falls_back_removes_stale_and_excludes_provenance(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            source, dest, root = workspace(temp, sqlite=False)
            runner = FakeRunner()
            indexer, provenance = module.publish_native_assets(
                source, dest, root, {}, runner=runner, revision='deadbeef')
            self.assertFalse(indexer['available'])
            self.assertIn('SQLite3', indexer['reason'])
            self.assertEqual(runner.calls, [])                      # 无目标就不跑 cmake
            self.assertFalse((dest / 'build-native' / 'etm_index.exe').exists())
            self.assertNotIn('build-native/etm_index.exe', provenance['packagedHashes'])
            self.assertIn('build-native/etm_decode.exe', provenance['packagedHashes'])
            self.assertFalse(provenance['nativeIndexer']['available'])
            self.assertTrue(any('Python 回退' in line for line in provenance['localChanges']))
            written = json.loads((root / 'bridge/etm/provenance.json').read_text(encoding='utf8'))
            self.assertEqual(written['sourceRevision'], 'deadbeef')
            self.assertNotIn('build-native/etm_index.exe', written['packagedHashes'])

    def test_unknown_target_falls_back(self):
        module = load_module()
        error = subprocess.CalledProcessError(1, ['cmake'], output='',
                                              stderr="ninja: error: unknown target 'etm_index'")
        with tempfile.TemporaryDirectory() as temp:
            source, dest, root = workspace(temp, sqlite=True)
            runner = FakeRunner(error=error)
            indexer, provenance = module.publish_native_assets(
                source, dest, root, {}, runner=runner, revision='rev-1')
            self.assertEqual(len(runner.calls), 1)
            self.assertFalse(indexer['available'])
            self.assertIn('目标', indexer['reason'])
            self.assertFalse((dest / 'build-native' / 'etm_index.exe').exists())
            self.assertNotIn('build-native/etm_index.exe', provenance['packagedHashes'])

    def test_real_compile_error_fails_before_provenance(self):
        module = load_module()
        error = subprocess.CalledProcessError(
            1, ['cmake'], output='',
            stderr='native/indexer.cpp:42:12: error: expected expression before ...')
        with tempfile.TemporaryDirectory() as temp:
            source, dest, root = workspace(temp, sqlite=True)
            runner = FakeRunner(error=error)
            published = []
            original = module.write_provenance
            module.write_provenance = lambda where, value: published.append(value)
            try:
                with self.assertRaises(SystemExit) as caught:
                    module.publish_native_assets(source, dest, root, {}, runner=runner, revision='rev-2')
            finally:
                module.write_provenance = original
            self.assertIn('不降级', str(caught.exception))
            self.assertEqual(published, [])                          # 不写 provenance
            self.assertFalse((root / 'bridge/etm/provenance.json').exists())
            self.assertFalse((dest / 'build-native' / 'etm_index.exe').exists())  # 旧产物被移除

    def test_success_copies_indexer_into_packaged_hashes(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            source, dest, root = workspace(temp, sqlite=True)
            artifact = source / 'build-native' / 'etm_index.exe'
            runner = FakeRunner(artifact=artifact)
            indexer, provenance = module.publish_native_assets(
                source, dest, root, {}, runner=runner, revision='rev-3')
            self.assertTrue(indexer['available'])
            self.assertEqual((dest / 'build-native' / 'etm_index.exe').read_bytes(), b'MZ fake indexer')
            self.assertIn('build-native/etm_index.exe', provenance['packagedHashes'])
            self.assertTrue(provenance['nativeIndexer']['available'])

    def test_dependency_missing_rule_is_not_a_fallback(self):
        # “No rule to make target 'sqlite3.lib'” 是真实构建错误，不是 etm_index 目标不存在。
        module = load_module()
        error = subprocess.CalledProcessError(
            1, ['cmake'], output='', stderr="make: *** No rule to make target 'sqlite3.lib'. Stop.")
        with tempfile.TemporaryDirectory() as temp:
            source, dest, root = workspace(temp, sqlite=True)
            with self.assertRaises(SystemExit):
                module.publish_native_assets(source, dest, root, {}, runner=FakeRunner(error=error),
                                             revision='rev-5')
            self.assertFalse((dest / 'build-native' / 'etm_index.exe').exists())

    def test_unconfigured_build_dir_fails(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as temp:
            source, dest, root = workspace(temp, sqlite=None)
            with self.assertRaises(SystemExit) as caught:
                module.publish_native_assets(source, dest, root, {}, runner=FakeRunner(), revision='rev-4')
            self.assertIn('无法构建原生索引器', str(caught.exception))
            self.assertFalse((root / 'bridge/etm/provenance.json').exists())


if __name__ == '__main__':
    unittest.main()
