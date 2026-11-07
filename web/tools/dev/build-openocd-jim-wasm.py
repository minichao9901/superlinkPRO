"""Build the browser configuration capture probe with OpenOCD v0.12.0's Jim Tcl."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


JIM_REVISION = "a77ef1a6218fad4c928ddbdc03c1aedc41007e70"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emsdk", type=Path, required=True)
    parser.add_argument("--jimtcl", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sdk, source, output = (path.resolve() for path in (args.emsdk, args.jimtcl, args.output))
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    if revision != JIM_REVISION:
        parser.error(f"expected Jim Tcl revision {JIM_REVISION}, found {revision}")
    output.mkdir(parents=True, exist_ok=False)
    headers = output / "include"
    headers.mkdir()
    # This probe links only the core. Jim's configured OS extensions (exec,
    # sockets, native packages and filesystem commands) are not built.
    (headers / "jim-config.h").write_text("#define HAVE_LONG_LONG 1\n", encoding="ascii")
    (headers / "jimautoconf.h").write_text(
        "#define JIM_VERSION 81\n"
        "#define HAVE_SYS_TIME_H 1\n"
        "#define HAVE_GETTIMEOFDAY 1\n"
        "#define HAVE_ISNAN 1\n"
        "#define HAVE_ISINF 1\n"
        "#define TCL_PLATFORM_PLATFORM \"wasm\"\n",
        encoding="ascii",
    )
    scratch = output / "tmp"
    scratch.mkdir()
    env = {**os.environ, "EM_CONFIG": str(sdk / ".emscripten"),
           "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch)}
    subprocess.run(
        [
            sys.executable, sdk / "upstream/emscripten/emcc.py",
            Path(__file__).with_name("openocd-jim-wasm.c").resolve(),
            *[source / name for name in ("jim.c", "jim-format.c", "utf8.c")],
            "-I", headers, "-I", source, "-O2", "--no-entry",
            "-sMODULARIZE=1", "-sEXPORT_ES6=1", "-sENVIRONMENT=worker,node",
            "-sFORCE_FILESYSTEM=1", "-sALLOW_MEMORY_GROWTH=1",
            "-sMAXIMUM_MEMORY=67108864", "-sSTACK_SIZE=262144",
            "-sEXPORTED_RUNTIME_METHODS=FS,cwrap",
            "-sEXPORTED_FUNCTIONS=_cfg_create,_cfg_set,_cfg_run,_cfg_result,_cfg_count,_cfg_arity,_cfg_arg",
            "-o", output / "openocd-jim.mjs",
        ],
        check=True,
        env=env,
    )
    for name in ("openocd-jim-wasm-worker.mjs", "openocd-jim-wasm-client.mjs"):
        shutil.copyfile(Path(__file__).with_name(name), output / name)
    shutil.copyfile(source / "LICENSE", output / "JimTcl-LICENSE")
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Jim Tcl configuration capture</title>\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
