"""The portable CLI must reuse public samples without duplicating their bytes."""
import importlib.util,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('workbench_package',Path(__file__).resolve().parents[2]/'web/tools/dev/build-etm-package.py')
builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)

class PackagingTests(unittest.TestCase):
    def test_tracked_samples_preserve_utf8_and_spaces(self):
        wire='samples/anim/花园.mp4\0samples/panel/带 空格.png\0'.encode('utf8')
        with patch.object(builder.subprocess,'check_output',return_value=wire) as command:
            self.assertEqual(builder.tracked_samples(Path('web')),['samples/anim/花园.mp4','samples/panel/带 空格.png'])
            self.assertIn('-z',command.call_args.args[0])
    def test_cli_keeps_imports_but_only_public_toolbox_contains_samples(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);web=root/'web';package=root/'package';toolbox=package/'toolbox'
            files={'package/toolbox/index.html':'page','package/toolbox/app/core.js':'module','package/toolbox/samples/image.bmp':'sample',
                   'web/package.json':'{}','web/tools/dev/serve-etm.py':'service','web/bridge/etm/backend/web/app/target.js':'target',
                   'etm/tools/node-transport/worker.mjs':'worker','etm/tools/trace_target_cli.mjs':'cli',
                   'web/bridge/etm/licenses/Node-v24.13.1-LICENSE.txt':'license','docs/trace-cli-ai.md':'guide','docs/etm-flame-svg.md':'svg','node.exe':'node'}
            for name,data in files.items():
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(data)
            (package/'licenses').mkdir()
            with patch.object(builder,'ROOT',web),patch.object(builder.subprocess,'check_output',return_value='v24.13.1\n'):
                builder.package_cli(package,toolbox,root/'node.exe')
            self.assertEqual((package/'cli/web/app/core.js').read_text(),'module')
            self.assertEqual((package/'cli/web/index.html').read_text(),'page')
            self.assertEqual((toolbox/'samples/image.bmp').read_text(),'sample')
            self.assertFalse((package/'cli/web/samples').exists())

if __name__=='__main__':unittest.main()
