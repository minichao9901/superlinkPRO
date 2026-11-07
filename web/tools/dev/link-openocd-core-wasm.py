"""Link a pinned OpenOCD core build as an isolated browser configuration probe."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


OPENOCD_REVISION = "9ea7f3d647c8ecf6b0f1424002dfc3f4504a162c"


def compile_flash_registries(source, build, scratch, emscripten, env, profile):
    nor_extra = {"cfi_flash", "stmqspi_flash", "virtual_flash", "hpm_xpi_flash"}
    specs = [
        ("flash/nor/drivers.c", "flash_drivers", lambda name: name.startswith("stm32") or name in nor_extra),
        ("flash/nand/driver.c", "nand_flash_controllers", lambda name: False),
    ]
    objects = []
    manifest = {}
    item = re.compile(r"(?m)^[ \t]*&(\w+),[ \t]*\n")
    for path, name, required in specs:
        def keep(entry):
            return profile == "full" or required(entry)

        original = source / "src" / path
        text = original.read_text(encoding="utf-8")
        table = re.compile(r"(?ms)(^static [^\n]*\b" + name + r"\[\]\s*=\s*\{\n)(.*?)(^\};)")
        match = table.search(text)
        if not match:
            raise ValueError(f"registry {name} was not found in {original}")
        entries = item.findall(match[2])
        if not entries:
            raise ValueError(f"registry {name} has no recognizable entries")
        filtered = item.sub(lambda entry: entry[0] if keep(entry[1]) else "", match[2])
        generated = scratch / f"{name}.c"
        generated.write_text(text[:match.start(2)] + filtered + text[match.end(2):], encoding="utf-8")
        obj = scratch / f"{name}.o"
        subprocess.run([
            sys.executable, emscripten / "emcc.py", "-std=gnu23", "-O1", "-DHAVE_CONFIG_H",
            "-I", build, "-I", source / "src", "-I", build / "src", "-I", original.parent,
            "-I", source / "jimtcl", "-I", build / "jimtcl", "-c", generated.name, "-o", obj.name,
        ], check=True, env=env, cwd=scratch)
        objects.append(obj)
        manifest[name] = {"fullEntries": len(entries), "kept": [entry for entry in entries if keep(entry)]}
    (scratch.parent / "registries.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return objects


def compile_webusb_cmsis_dap(source, build, scratch, emscripten, env):
    original = source / 'src/jtag/drivers/cmsis_dap.c'
    text = original.read_text(encoding='utf-8')
    marker = 'static void cmsis_dap_flush_read(struct cmsis_dap *dap)\n{\n'
    if text.count(marker) != 1:
        raise ValueError('CMSIS-DAP drain changed upstream; review the WebUSB adaptation')
    # A speculative read from an empty endpoint cannot be cancelled by WebUSB.
    # Keep the upstream command/response validation and never treat a stale
    # response as the result of a different command.
    start = text.index(marker)
    end = text.index('\n}\n', start + len(marker))
    replacement = marker + '\t(void)dap;\n\tLOG_DEBUG("Skipping speculative CMSIS-DAP read on WebUSB");'
    generated = scratch / original.name
    generated.write_text(text[:start] + replacement + text[end:], encoding='utf-8')
    obj = scratch / 'cmsis-dap-webusb.o'
    subprocess.run([
        sys.executable, emscripten / 'emcc.py', '-std=gnu23', '-O1', '-DHAVE_CONFIG_H',
        '-I', build, '-I', source / 'src', '-I', original.parent,
        '-I', source / 'jimtcl', '-I', build / 'jimtcl', '-c', generated.name, '-o', obj.name,
    ], check=True, env=env, cwd=scratch)
    return obj


def compile_webusb_events(source, build, scratch, emscripten, env):
    original = source / 'libusb/os/events_posix.c'
    text = original.read_text(encoding='utf-8')
    original_wait = '\tawait Atomics.waitAsync(HEAP32, ptr >> 2, expected_value, timeout).value;'
    notify = '\temscripten_atomic_notify(&event->has_event, EMSCRIPTEN_NOTIFY_ALL_WAITERS);'
    if text.count(original_wait) != 1 or text.count(notify) != 1:
        raise ValueError('libusb event code changed upstream; review the WebUSB adaptation')
    # The browser session links without pthreads. Use its worker event loop
    # to wake the sole libusb event waiter instead of parking on shared memory.
    text = text.replace(original_wait, '''
    if (timeout === 0 || HEAP32[ptr >> 2] !== expected_value) return;
    const waiters = Module.webUsbEventWaiters ??= new Map();
    await new Promise(resolve => {
        const wake = () => {
            clearTimeout(timer);
            waiters.delete(ptr);
            resolve();
        };
        const timer = setTimeout(wake, timeout);
        waiters.set(ptr, wake);
    });'''.strip('\n'))
    text = text.replace('static void em_libusb_wait(', '''EM_JS(void, wake_webusb_waiter, (const void *ptr), {
    Module.webUsbEventWaiters?.get(ptr)?.();
});

static void em_libusb_wait(''', 1)
    text = text.replace(notify, '\twake_webusb_waiter(&event->has_event);')
    generated = scratch / original.name
    generated.write_text(text, encoding='utf-8')
    obj = scratch / 'libusb-webusb-events.o'
    subprocess.run([
        sys.executable, emscripten / 'emcc.py', '-std=gnu11', '-O2', '-pthread',
        '-DHAVE_CONFIG_H', '-I', build, '-I', source / 'libusb', '-I', original.parent,
        '-c', generated.name, '-o', obj.name,
    ], check=True, env=env, cwd=scratch)
    return obj


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emsdk", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--libusb", type=Path, help="libusb WebUSB static archive, for a CMSIS-DAP build")
    parser.add_argument("--libusb-source", type=Path, help="libusb source used for --browser-session")
    parser.add_argument("--libusb-build", type=Path, help="configured libusb build used for --browser-session")
    parser.add_argument("--optimization", choices=["O1", "Oz"], default="O1",
                        help="Emscripten link optimization (default: O1)")
    parser.add_argument("--source-revision", default=OPENOCD_REVISION)
    parser.add_argument("--profile", choices=["full", "stm32-hpm-flash"], default="full")
    parser.add_argument("--browser-session", action="store_true",
                        help="replace the socket server loop with a browser command queue")
    args = parser.parse_args()
    if args.browser_session and not (args.libusb and args.libusb_source and args.libusb_build):
        parser.error("--browser-session requires --libusb, --libusb-source and --libusb-build")
    sdk, source, build, output = (path.resolve() for path in
                                  (args.emsdk, args.source, args.build, args.output))
    revision = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
    if revision != args.source_revision:
        parser.error(f"expected OpenOCD revision {args.source_revision}, found {revision}")

    output.mkdir(parents=True, exist_ok=False)
    scratch = output / "tmp"
    scratch.mkdir()
    env = {**os.environ, "EM_CONFIG": str(sdk / ".emscripten"),
           "TEMP": str(scratch), "TMP": str(scratch), "TMPDIR": str(scratch)}
    emscripten = sdk / "upstream/emscripten"
    main_obj = scratch / "main.o"
    subprocess.run([
        sys.executable, emscripten / "emcc.py", "-DHAVE_CONFIG_H", "-I", build,
        "-I", source / "src", "-c", source / "src/main.c", "-o", main_obj,
    ], check=True, env=env)
    libraries = [build / "src/.libs/libopenocd.a", build / "jimtcl/libjim.a"]
    replacements = compile_flash_registries(source, build, scratch, emscripten, env, args.profile)
    linker = "emcc.py"
    features = []
    if args.libusb:
        libraries.append(args.libusb.resolve())
        linker = "em++.py"
        features = ["--bind", "-sASYNCIFY=1", "-sEXPORTED_FUNCTIONS=_main,_malloc,_free"]
    runtime_methods = "FS,callMain,ccall,lengthBytesUTF8,stringToUTF8,setValue"
    if args.browser_session:
        bridge = Path(__file__).with_name("openocd-browser-loop.c")
        bridge_obj = scratch / "openocd-browser-loop.o"
        subprocess.run([
            sys.executable, emscripten / "emcc.py", "-std=gnu23", "-O1", "-DHAVE_CONFIG_H",
            "-I", build, "-I", source / "src", "-I", source / "jimtcl", "-I", build / "jimtcl",
            "-c", bridge, "-o", bridge_obj,
        ], check=True, env=env)
        replacements.append(bridge_obj)
        replacements.append(compile_webusb_cmsis_dap(source, build, scratch, emscripten, env))
        replacements.append(compile_webusb_events(args.libusb_source.resolve(), args.libusb_build.resolve(),
                                                  scratch, emscripten, env))
        features.append("-Wl,--wrap=server_loop")
        runtime_methods += ",stringToNewUTF8,UTF8ToString"
    subprocess.run([
        sys.executable, emscripten / linker, f"-{args.optimization}", main_obj,
        *replacements, *libraries, *features,
        "-sMODULARIZE=1", "-sEXPORT_ES6=1", "-sENVIRONMENT=worker",
        "-sINVOKE_RUN=0", "-sEXIT_RUNTIME=1", "-sFORCE_FILESYSTEM=1",
        "-sALLOW_MEMORY_GROWTH=1", "-sMAXIMUM_MEMORY=134217728", "-sSTACK_SIZE=524288",
        f"-sEXPORTED_RUNTIME_METHODS={runtime_methods}",
        "-o", output / "openocd-core.mjs",
    ], check=True, env=env)
    worker = Path(__file__).with_name("openocd-core-wasm-worker.mjs")
    shutil.copyfile(worker, output / worker.name)
    shutil.copyfile(source / "COPYING", output / "OpenOCD-COPYING")
    (output / "index.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>OpenOCD core configuration probe</title>\n',
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
