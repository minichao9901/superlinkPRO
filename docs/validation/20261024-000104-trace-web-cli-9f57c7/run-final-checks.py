from pathlib import Path
import json,sys,subprocess,time
run=Path('E:/superlinkPRO/docs/validation/20261024-000104-trace-web-cli-9f57c7')
root=Path('C:/Users/Administrator/.codex/worktrees/etm-kvdat-fastpath/superlinkPRO')
out=run/('checks-final-'+time.strftime('%H%M%S'));out.mkdir()
commands=[
 ('cli-unit',[sys.executable,'-X','utf8','-m','unittest','discover','-s','etm/tools/tests','-p','test_trace_cli*.py','-v'],root),
 ('queue-unit',[sys.executable,'-X','utf8','-m','unittest','discover','-s','etm/tools/tests','-p','test_web_cli_queue.py','-v'],root),
 ('web-cli-adapters',['node','--test','web/tools/selftest/trace-cli-automation.test.mjs','etm/tools/tests/node-usb-adapter.test.mjs','etm/tools/tests/node-images.test.mjs'],root),
 ('dbg-core',['node','tools/selftest/dbg-core.test.mjs'],root/'web'),
 ('dbg-image',['node','tools/selftest/dbg-image.test.mjs'],root/'web'),
 ('dbg-ram-fill',['node','tools/selftest/dbg-ram-fill.test.mjs'],root/'web'),
 ('flash-image',['node','tools/selftest/flash-image.test.mjs'],root/'web'),
 ('flash-abi',['node','tools/selftest/flash-runner-abi.test.mjs'],root/'web'),
 ('flash-clock',['node','tools/selftest/flash-data-clock.test.mjs'],root/'web'),
 ('etm-targets',['node','--test','etm/tools/tests/breakpoints.test.mjs','etm/tools/tests/target.test.mjs','etm/tools/tests/f103-target.test.mjs'],root),
 ('etm-host-reset',['node','--test','tools/selftest/etm-host-control.test.mjs','tools/selftest/target-reset.test.mjs'],root/'web'),
 ('web-bundle',['node','tools/dev/build-web.mjs','--check'],root/'web'),
 ('etm-copy',['node','tools/dev/sync-etm-page.mjs','--check'],root/'web'),
 ('python-compile',[sys.executable,'-m','py_compile','etm/tools/trace_cli.py','etm/tools/trace_cli_hardware.py','etm/tools/trace_cli_runtime.py','etm/tools/web_cli_queue.py','etm/tools/trace_cli_legacy.py'],root),
 ('worker-syntax',['node','--check','etm/tools/node-transport/worker.mjs'],root),
 ('web-cli-syntax',['node','--check','web/app/automation/trace.js'],root),
 ('pipe-syntax',['node','--check','etm/tools/trace_target_cli.mjs'],root),
]
results=[]
for name,command,cwd in commands:
 start=time.perf_counter()
 with (out/(name+'.log')).open('xb') as log:
  try:
   result=subprocess.run(command,cwd=cwd,stdout=log,stderr=subprocess.STDOUT,timeout=180)
   code=result.returncode
  except subprocess.TimeoutExpired:code=124
 results.append(dict(name=name,command=command,cwd=str(cwd),exitCode=code,seconds=time.perf_counter()-start,log=str(out/(name+'.log'))))
 (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf8')
 print(name,'PASS' if code==0 else 'FAIL',code,flush=True)
(run/'checks-pointer.json').write_text(json.dumps(dict(evidence=str(out),passed=all(r['exitCode']==0 for r in results),checks=len(results)),indent=2),encoding='utf8')
assert all(r['exitCode']==0 for r in results),results
