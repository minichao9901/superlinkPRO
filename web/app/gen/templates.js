/**
 * app/gen/templates.js
 *
 * 浏览器可用的 ES module：`uvprojx2cmake.py` 里 5 个「文件生成器」的逐字节移植。
 * 无任何依赖：不使用 Node API、不使用 fetch、不使用 Buffer。
 *
 * 源真源：E:\VibeCoding\my_trace_tools\uvprojx2cmake.py
 *   - JLINK_MAKEFILE_TEMPLATE   -> Makefile.jlink
 *   - PYOCD_MAKEFILE_TEMPLATE   -> Makefile.pyocd
 *   - OPENOCD_MAKEFILE_TEMPLATE -> Makefile.openocd
 *   - RTT_LOGGER_TEMPLATE       -> rtt_logger.py   (OpenOCD 生成的附带产物)
 *   - FileGenerator.generate_gdb_script  -> jlink_gdb.script
 *   - FileGenerator.generate_test_bin    -> test_sram.bin
 *
 * ── 换行（重要）────────────────────────────────────────────────────────────
 * Python 侧用 `with open(path, 'w') as f: f.write(content)` 写文本。CPython 在
 * Windows 上以 newline=None 打开，会把 '\n' 翻译成 os.linesep = '\r\n'。
 * 实测复现（2026-xx，见报告）：
 *     python uvprojx2cmake.py ... --gen-jlink --gen-gdb --gen-pyocd --gen-openocd
 *            --gen-test-bin --pyocd-target stm32f103rb --force
 * 产出的 6 个文件与仓库内参照产物逐字节相同，且 CRLF=170/133/189/59/70、裸 LF=0。
 * 因此「字节相等」的唯一正确选择是 CRLF —— 本模块默认按 CRLF 输出（EOL）。
 * 若 UI 需要 LF 文本，用 toLf() 转换（会破坏与 Python 的逐字节相等）。
 * test_sram.bin 是二进制，不受换行影响。
 */

/* ========================================================================== */
/* 换行                                                                        */
/* ========================================================================== */

/** Python 在 Windows 上写文本文件时的实际换行（os.linesep）。 */
export const EOL = '\r\n';

/** 把本模块产出的 CRLF 文本转成 LF 文本（供 UI 展示/写入非 Windows 场景）。 */
export function toLf(text) {
  return String(text).split('\r\n').join('\n');
}

/* ========================================================================== */
/* 默认值                                                                      */
/* ========================================================================== */

/**
 * 模板里 `XXX ?= 默认值` 那一行的默认值（前 11 项是任务书给定的契约字段），
 * 其余按 Python 模板里的实际默认值补齐。
 */
export const DEFAULTS = {
  // —— 契约字段 ——
  jlinkIf: 'SWD',                 // JLINK_IF ?= SWD            (Makefile.jlink)
  jlinkSpeed: 25000,              // JLINK_SPEED ?= 25000       (Makefile.jlink)
  rttSize: '0x5000',              // RTT_SIZE ?= 0x5000         (Makefile.jlink)
  gdbPort: 3333,                  // GDB_PORT ?= 3333           (jlink/pyocd/openocd)
  buildDir: 'build',              // BUILD_DIR ?= build         (jlink/pyocd/openocd)
  cmakeGenerator: 'Ninja',        // CMAKE_GENERATOR ?= Ninja   (jlink/pyocd/openocd)
  buildType: 'Debug',             // CMAKE_BUILD_TYPE ?= Debug  (jlink/pyocd/openocd)
  pyocdFreq: 10000000,            // PYOCD_FREQ ?= 10000000 Hz  (Makefile.pyocd)
  openocdFreq: 10000,             // OPENOCD_FREQ ?= 10000 kHz  (Makefile.openocd)
  rttPort: 9090,                  // RTT_PORT ?= 9090           (Makefile.openocd)
  testBinSize: 20480,             // generate_test_bin(size=20480)

  // —— 其余模板内实际默认值（补齐）——
  jlinkDevice: 'STM32F103RB',     // 兜底：Python 侧来自 resolve_jlink_device_name()
  pyocdTarget: 'stm32f103rb',     // 兜底：Python 侧来自 resolve_pyocd_target_name()
  flashStart: '0x08000000',       // device_info.get('flash_start', '0x08000000')
  rttAddress: '0x20002000',       // DEFAULT_RTT_ADDRESS
  gdb: 'arm-none-eabi-gdb',       // GDB ?= arm-none-eabi-gdb
  gdbScript: 'jlink_gdb.script',  // GDB_SCRIPT ?= jlink_gdb.script
  cmake: 'cmake',                 // CMAKE ?= cmake
  openocdInterface: 'interface/cmsis-dap.cfg', // OPENOCD_INTERFACE_MAP['daplink']
  openocdTarget: 'target/cortex_m.cfg',        // OPENOCD_TARGET_MAP 兜底
  openocdRoot: '',                // Python 侧 _detect_openocd_root()，探测不到则为空
  testBin: 'test_sram.bin',       // TEST_BIN ?= test_sram.bin
  testSramAddr: '0x20000000',     // TEST_SRAM_ADDR ?= 0x20000000
  testSramSize: '0x5000',         // TEST_SRAM_SIZE ?= 0x5000
  pyocdRttFreq: 30000000,         // pyocd rtt -f 30000000（写死的 30MHz，非 PYOCD_FREQ）
  openocdRttSize: '0x2000',       // RTT_SIZE ?= 0x2000（openocd 侧，注意与 jlink 的 0x5000 不同）
  rttLoggerHost: '127.0.0.1',     // rtt_logger.py: HOST = '127.0.0.1'
  rttLoggerPort: 9090,            // rtt_logger.py: PORT = 9090
  rttLoggerFile: 'rtt_log.txt',   // rtt_logger.py: LOG_FILE = 'rtt_log.txt'
};

/* ========================================================================== */
/* 内部工具                                                                    */
/* ========================================================================== */

/** 取 p[key]，缺省（undefined/null）时用 fallback。 */
function pick(p, key, fallback) {
  const v = p ? p[key] : undefined;
  return v === undefined || v === null ? fallback : v;
}

/** 定点替换模板占位符（split/join，避免正则的 $ 语义）。 */
function subst(text, map) {
  let out = text;
  for (const key of Object.keys(map)) {
    out = out.split('{' + key + '}').join(String(map[key]));
  }
  return out;
}

/**
 * 定点替换某一行 `NAME ?= 默认值`。
 * 只改这一行（注释、help 文本、make 变量引用 `$(NAME)` 一律不动）。
 */
function setMakeDefault(lines, name, value) {
  const prefix = name + ' ?= ';
  const idx = lines.findIndex((l) => l.startsWith(prefix));
  if (idx < 0) {
    throw new Error('template anchor not found: ' + name + ' ?=');
  }
  lines[idx] = prefix + String(value);
}

/**
 * 按 [[选项键, make 变量名], ...] 把 p 里显式给出的覆盖值写进模板行数组。
 * 未给值的项完全不触碰（保证默认输出与 Python 逐字节相同）。
 */
function applyOverrides(lines, p, specs) {
  for (const spec of specs) {
    const key = spec[0];
    const name = spec[1];
    if (p && p[key] !== undefined && p[key] !== null) {
      setMakeDefault(lines, name, p[key]);
    }
  }
}

/** 行数组 -> CRLF 文本（每行末尾都有 CRLF，与 Python 模板尾部的 '\n' 一致）。 */
function render(lines) {
  return lines.join(EOL) + EOL;
}

/** 复制一份模板行数组（避免覆盖值污染后续调用）。 */
function clone(lines) {
  return lines.slice();
}

/* ========================================================================== */
/* JLINK_MAKEFILE_TEMPLATE -> Makefile.jlink                                   */
/* ========================================================================== */

const JLINK_MAKEFILE_LINES = [
  '# JLink Makefile for {project_name}',
  '# Auto-generated by uvprojx2cmake.py',
  '# ',
  '# This Makefile provides convenient targets for JLink debugging and programming',
  '#',
  '# Requirements:',
  '#   - J-Link Software Pack installed and in PATH',
  '#   - J-Link debugger connected to target',
  '#',
  '# Usage:',
  '#   make jlink-erase    - Erase entire flash',
  '#   make jlink-prog     - Program the firmware (using .hex file)',
  '#   make jlink-prog-bin - Program the firmware (using .bin file)',
  '#   make jlink-rtt      - Start RTT logger',
  '#   make jlink-swo      - Start SWO viewer',
  '',
  '# =============================================================================',
  '# Configuration',
  '# =============================================================================',
  '',
  '# JLink device name (auto-detected or override)',
  'JLINK_DEVICE ?= {jlink_device}',
  '',
  '# Interface and speed',
  'JLINK_IF ?= SWD',
  'JLINK_SPEED ?= 25000',
  '',
  '# Build output files',
  'BUILD_DIR ?= build',
  'HEX_FILE ?= $(BUILD_DIR)/{project_name}.hex',
  'BIN_FILE ?= $(BUILD_DIR)/{project_name}.bin',
  '',
  '# RTT search range (RAM address and size)',
  'RTT_ADDR ?= {rtt_address}',
  'RTT_SIZE ?= 0x5000',
  '',
  '# GDB configuration',
  'GDB ?= arm-none-eabi-gdb',
  'GDB_PORT ?= 3333',
  'GDB_SCRIPT ?= jlink_gdb.script',
  '',
  '# CMake configuration',
  'CMAKE ?= cmake',
  'CMAKE_BUILD_TYPE ?= Debug',
  'CMAKE_GENERATOR ?= Ninja',
  '',
  '# =============================================================================',
  '# Targets',
  '# =============================================================================',
  '',
  '.PHONY: all build clean cmake jlink-erase jlink-prog jlink-prog-bin jlink-rtt jlink-swo jlink-gdb jlink-debug jlink-check clean-jlink jlink-help',
  '',
  '# Default target: build everything',
  'all: build',
  '',
  '# CMake configure',
  'cmake:',
  '\t@echo "Configuring with CMake..."',
  '\t$(CMAKE) -S . -B $(BUILD_DIR) -G $(CMAKE_GENERATOR) -DCMAKE_BUILD_TYPE=$(CMAKE_BUILD_TYPE)',
  '',
  '# Build the project',
  'build: cmake',
  '\t@echo "Building project..."',
  '\t$(CMAKE) --build $(BUILD_DIR)',
  '',
  '# Clean build directory',
  'clean:',
  '\t@echo "Cleaning build directory..."',
  '\t-rmdir /s /q $(BUILD_DIR) 2>nul || rm -rf $(BUILD_DIR) 2>/dev/null || true',
  '',
  '# Erase entire flash',
  'jlink-erase:',
  '\t@echo "Erasing flash..."',
  '\t@echo r > jlink_erase.script',
  '\t@echo h >> jlink_erase.script',
  '\t@echo erase >> jlink_erase.script',
  '\t@echo r >> jlink_erase.script',
  '\t@echo q >> jlink_erase.script',
  '\tJLink.exe -device $(JLINK_DEVICE) -if $(JLINK_IF) -speed $(JLINK_SPEED) -CommanderScript jlink_erase.script',
  '\t@del jlink_erase.script 2>nul || rm -f jlink_erase.script',
  '',
  '# Program firmware using HEX file (requires pre-built hex file)',
  'jlink-prog:',
  '\t@echo "Programming $(HEX_FILE)..."',
  '\t@echo r > jlink_prog.script',
  '\t@echo h >> jlink_prog.script',
  '\t@echo loadfile $(HEX_FILE) >> jlink_prog.script',
  '\t@echo r >> jlink_prog.script',
  '\t@echo q >> jlink_prog.script',
  '\tJLink.exe -device $(JLINK_DEVICE) -if $(JLINK_IF) -speed $(JLINK_SPEED) -CommanderScript jlink_prog.script',
  '\t@del jlink_prog.script 2>nul || rm -f jlink_prog.script',
  '',
  '# Program firmware using BIN file (requires pre-built bin file)',
  'jlink-prog-bin:',
  '\t@echo "Programming $(BIN_FILE)..."',
  '\t@echo r > jlink_prog.script',
  '\t@echo h >> jlink_prog.script',
  '\t@echo loadfile $(BIN_FILE) {flash_start} >> jlink_prog.script',
  '\t@echo r >> jlink_prog.script',
  '\t@echo q >> jlink_prog.script',
  '\tJLink.exe -device $(JLINK_DEVICE) -if $(JLINK_IF) -speed $(JLINK_SPEED) -CommanderScript jlink_prog.script',
  '\t@del jlink_prog.script 2>nul || rm -f jlink_prog.script',
  '',
  '# Start RTT Logger',
  'jlink-rtt:',
  '\t@echo "Starting RTT Logger (output: rtt.txt)..."',
  '\tJLinkRTTLogger.exe -Device $(JLINK_DEVICE) -If $(JLINK_IF) -Speed $(JLINK_SPEED) \\',
  '\t\t-RTTSearchRanges "$(RTT_ADDR) $(RTT_SIZE)" \\',
  '\t\t-RTTChannel 0 rtt.txt',
  '',
  '# Start SWO Viewer',
  'jlink-swo:',
  '\t@echo "Starting SWO Viewer (output: swo.log)..."',
  '\tJLinkSWOViewerCL.exe -device $(JLINK_DEVICE) -cpufreq 72000000 -swofreq 36000000 -itmport 0 -outputfile swo.log',
  '',
  '# Start GDB Server',
  'jlink-gdb:',
  '\t@echo "Starting JLink GDB Server on port $(GDB_PORT)..."',
  '\tJLinkGDBServerCL.exe -device $(JLINK_DEVICE) -if $(JLINK_IF) -speed $(JLINK_SPEED) -port $(GDB_PORT)',
  '',
  '# Check JLink connection',
  'jlink-check:',
  '\t@echo "Checking JLink connection..."',
  '\t@echo r > jlink_check.script',
  '\t@echo h >> jlink_check.script',
  '\t@echo q >> jlink_check.script',
  '\tJLink.exe -device $(JLINK_DEVICE) -if $(JLINK_IF) -speed $(JLINK_SPEED) -CommanderScript jlink_check.script',
  '\t@del jlink_check.script 2>nul || rm -f jlink_check.script',
  '',
  '# GDB debug - requires jlink-gdb-server running in another terminal',
  'jlink-debug: $(BUILD_DIR)/{project_name}.elf',
  '\t@echo "Starting GDB debug session..."',
  '\t@echo "Make sure to run \'make jlink-gdb\' in another terminal first"',
  '\t$(GDB) $(BUILD_DIR)/{project_name}.elf -x $(GDB_SCRIPT)',
  '',
  '# Clean generated files',
  'clean-jlink:',
  '\t@del jlink_*.script 2>nul || rm -f jlink_*.script',
  '\t@del *.log 2>nul || rm -f *.log',
  '\t@del $(GDB_SCRIPT) 2>nul || rm -f $(GDB_SCRIPT)',
  '',
  '# Help',
  'jlink-help:',
  '\t@echo "Build Targets:"',
  '\t@echo "  all             - Configure and build the project (default)"',
  '\t@echo "  build           - Build the project (configure if needed)"',
  '\t@echo "  cmake           - Run CMake configuration"',
  '\t@echo "  clean           - Clean build directory"',
  '\t@echo ""',
  '\t@echo "JLink Targets:"',
  '\t@echo "  jlink-erase     - Erase entire flash"',
  '\t@echo "  jlink-prog      - Program firmware (.hex)"',
  '\t@echo "  jlink-prog-bin  - Program firmware (.bin)"',
  '\t@echo "  jlink-rtt       - Start RTT logger"',
  '\t@echo "  jlink-swo       - Start SWO viewer"',
  '\t@echo "  jlink-gdb       - Start GDB server"',
  '\t@echo "  jlink-debug     - Start GDB debug session (requires jlink-gdb)"',
  '\t@echo "  jlink-check     - Check JLink connection"',
  '\t@echo "  clean-jlink     - Clean generated files"',
  '\t@echo ""',
  '\t@echo "Configuration:"',
  '\t@echo "  JLINK_DEVICE    = $(JLINK_DEVICE)"',
  '\t@echo "  JLINK_IF        = $(JLINK_IF)"',
  '\t@echo "  JLINK_SPEED     = $(JLINK_SPEED)"',
  '\t@echo "  HEX_FILE        = $(HEX_FILE)"',
  '\t@echo "  BIN_FILE        = $(BIN_FILE)"',
  '\t@echo "  GDB             = $(GDB)"',
  '\t@echo "  BUILD_DIR       = $(BUILD_DIR)"',
  '\t@echo "  CMAKE_BUILD_TYPE= $(CMAKE_BUILD_TYPE)"',
  '\t@echo "  GDB_PORT     = $(GDB_PORT)"',
];

/** 可覆盖项：[入参名, 模板里的 make 变量名] */
const JLINK_OVERRIDES = [
  ['jlinkIf', 'JLINK_IF'],
  ['jlinkSpeed', 'JLINK_SPEED'],
  ['rttSize', 'RTT_SIZE'],
  ['gdbPort', 'GDB_PORT'],
  ['buildDir', 'BUILD_DIR'],
  ['cmakeGenerator', 'CMAKE_GENERATOR'],
  ['buildType', 'CMAKE_BUILD_TYPE'],
];

/**
 * 生成 Makefile.jlink。
 * @param {{projectName:string, jlinkDevice:string, flashStart:string, rttAddress:string,
 *          jlinkIf?:string, jlinkSpeed?:number, rttSize?:string, gdbPort?:number,
 *          buildDir?:string, cmakeGenerator?:string, buildType?:string}} p
 * @returns {string} CRLF 文本
 */
export function renderJlinkMakefile(p) {
  const lines = clone(JLINK_MAKEFILE_LINES);
  applyOverrides(lines, p, JLINK_OVERRIDES);
  return subst(render(lines), {
    project_name: pick(p, 'projectName', ''),
    jlink_device: pick(p, 'jlinkDevice', DEFAULTS.jlinkDevice),
    flash_start: pick(p, 'flashStart', DEFAULTS.flashStart),
    rtt_address: pick(p, 'rttAddress', DEFAULTS.rttAddress),
  });
}

/* ========================================================================== */
/* PYOCD_MAKEFILE_TEMPLATE -> Makefile.pyocd                                   */
/* ========================================================================== */

const PYOCD_MAKEFILE_LINES = [
  '# PyOCD Makefile for {project_name}',
  '# Auto-generated by uvprojx2cmake.py',
  '# ',
  '# This Makefile provides convenient targets for PyOCD debugging and programming',
  '#',
  '# Requirements:',
  '#   - pyocd installed: pip install pyocd',
  '#   - CMSIS-Pack for target device installed',
  '#',
  '# Usage:',
  '#   make pyocd-erase    - Erase entire flash',
  '#   make pyocd-prog     - Program the firmware (using .hex file)',
  '#   make pyocd-prog-bin - Program the firmware (using .bin file)',
  '#   make pyocd-gdb      - Start PyOCD GDB server',
  '#   make pyocd-debug    - Start GDB debug session (requires pyocd-gdb)',
  '#   make pyocd-rtt      - Start RTT client',
  '',
  '# =============================================================================',
  '# Configuration',
  '# =============================================================================',
  '',
  '# PyOCD target device (auto-detected or override)',
  'PYOCD_TARGET ?= {pyocd_target}',
  '',
  '# PyOCD frequency (Hz)',
  'PYOCD_FREQ ?= 10000000',
  '',
  '# GDB configuration',
  'GDB ?= arm-none-eabi-gdb',
  'GDB_PORT ?= 3333',
  'GDB_SCRIPT ?= jlink_gdb.script',
  '',
  '# Build output files',
  'BUILD_DIR ?= build',
  'HEX_FILE ?= $(BUILD_DIR)/{project_name}.hex',
  'BIN_FILE ?= $(BUILD_DIR)/{project_name}.bin',
  '',
  '# CMake configuration',
  'CMAKE ?= cmake',
  'CMAKE_BUILD_TYPE ?= Debug',
  'CMAKE_GENERATOR ?= Ninja',
  '',
  '# =============================================================================',
  '# Targets',
  '# =============================================================================',
  '',
  '.PHONY: all build clean cmake pyocd-erase pyocd-prog pyocd-prog-bin pyocd-gdb pyocd-debug pyocd-rtt pyocd-reset pyocd-help',
  '',
  '# Default target: build everything',
  'all: build',
  '',
  '# CMake configure',
  'cmake:',
  '\t@echo "Configuring with CMake..."',
  '\t$(CMAKE) -S . -B $(BUILD_DIR) -G $(CMAKE_GENERATOR) -DCMAKE_BUILD_TYPE=$(CMAKE_BUILD_TYPE)',
  '',
  '# Build the project',
  'build: cmake',
  '\t@echo "Building project..."',
  '\t$(CMAKE) --build $(BUILD_DIR)',
  '',
  '# Clean build directory',
  'clean:',
  '\t@echo "Cleaning build directory..."',
  '\t-rmdir /s /q $(BUILD_DIR) 2>nul || rm -rf $(BUILD_DIR) 2>/dev/null || true',
  '',
  '# Erase entire flash',
  'pyocd-erase:',
  '\t@echo "Erasing flash with pyocd..."',
  '\tpyocd erase -c -t $(PYOCD_TARGET)',
  '',
  '# Program firmware using HEX file (requires pre-built hex file)',
  'pyocd-prog:',
  '\t@echo "Programming $(HEX_FILE) with pyocd..."',
  '\tpyocd flash -t $(PYOCD_TARGET) $(HEX_FILE) -f $(PYOCD_FREQ)',
  '',
  '# Program firmware using BIN file (requires pre-built bin file)',
  'pyocd-prog-bin:',
  '\t@echo "Programming $(BIN_FILE) with pyocd..."',
  '\tpyocd flash -t $(PYOCD_TARGET) $(BIN_FILE) -f $(PYOCD_FREQ) --base-address {flash_start}',
  '',
  '# Start PyOCD GDB server',
  'pyocd-gdb:',
  '\t@echo "Starting PyOCD GDB server on port $(GDB_PORT) ($(PYOCD_FREQ) Hz)..."',
  '\tpyocd gdbserver -t $(PYOCD_TARGET) -p $(GDB_PORT) -f $(PYOCD_FREQ)',
  '',
  '# Start RTT client (30MHz, output to rtt.txt)',
  'pyocd-rtt:',
  '\t@echo "Starting PyOCD RTT client (30MHz, output to rtt.txt)..."',
  '\tpyocd rtt -t $(PYOCD_TARGET) -f 30000000 -a {rtt_address} -d rtt.txt',
  '',
  '# Reset target',
  'pyocd-reset:',
  '\t@echo "Resetting target..."',
  '\tpyocd reset -t $(PYOCD_TARGET)',
  '',
  '# GDB debug - requires pyocd-gdb running in another terminal',
  'pyocd-debug: $(BUILD_DIR)/{project_name}.elf',
  '\t@echo "Starting GDB debug session..."',
  '\t@echo "Make sure to run \'make pyocd-gdb\' in another terminal first"',
  '\t$(GDB) $(BUILD_DIR)/{project_name}.elf -x $(GDB_SCRIPT)',
  '',
  '# Clean generated files',
  'clean-pyocd:',
  '\t@del $(GDB_SCRIPT) 2>nul || rm -f $(GDB_SCRIPT)',
  '',
  '# Help',
  'pyocd-help:',
  '\t@echo "Build Targets:"',
  '\t@echo "  all             - Configure and build the project (default)"',
  '\t@echo "  build           - Build the project (configure if needed)"',
  '\t@echo "  cmake           - Run CMake configuration"',
  '\t@echo "  clean           - Clean build directory"',
  '\t@echo ""',
  '\t@echo "PyOCD Targets:"',
  '\t@echo "  pyocd-erase     - Erase entire flash"',
  '\t@echo "  pyocd-prog      - Program firmware (.hex)"',
  '\t@echo "  pyocd-prog-bin  - Program firmware (.bin)"',
  '\t@echo "  pyocd-gdb       - Start PyOCD GDB server"',
  '\t@echo "  pyocd-debug     - Start GDB debug session (requires pyocd-gdb)"',
  '\t@echo "  pyocd-rtt       - Start RTT client"',
  '\t@echo "  pyocd-reset     - Reset target"',
  '\t@echo "  clean-pyocd     - Clean generated files"',
  '\t@echo ""',
  '\t@echo "Configuration:"',
  '\t@echo "  PYOCD_TARGET    = $(PYOCD_TARGET)"',
  '\t@echo "  PYOCD_FREQ      = $(PYOCD_FREQ)"',
  '\t@echo "  HEX_FILE        = $(HEX_FILE)"',
  '\t@echo "  BIN_FILE        = $(BIN_FILE)"',
  '\t@echo "  GDB             = $(GDB)"',
  '\t@echo "  GDB_PORT        = $(GDB_PORT)"',
  '\t@echo "  BUILD_DIR       = $(BUILD_DIR)"',
  '\t@echo "  CMAKE_BUILD_TYPE= $(CMAKE_BUILD_TYPE)"',
];

/** 可覆盖项：[入参名, 模板里的 make 变量名] */
const PYOCD_OVERRIDES = [
  ['pyocdFreq', 'PYOCD_FREQ'],
  ['gdbPort', 'GDB_PORT'],
  ['buildDir', 'BUILD_DIR'],
  ['cmakeGenerator', 'CMAKE_GENERATOR'],
  ['buildType', 'CMAKE_BUILD_TYPE'],
];

/**
 * 生成 Makefile.pyocd。
 * @param {{projectName:string, pyocdTarget:string, flashStart:string, rttAddress:string,
 *          pyocdFreq?:number, gdbPort?:number, buildDir?:string,
 *          cmakeGenerator?:string, buildType?:string}} p
 * @returns {string} CRLF 文本
 */
export function renderPyocdMakefile(p) {
  const lines = clone(PYOCD_MAKEFILE_LINES);
  applyOverrides(lines, p, PYOCD_OVERRIDES);
  return subst(render(lines), {
    project_name: pick(p, 'projectName', ''),
    pyocd_target: pick(p, 'pyocdTarget', DEFAULTS.pyocdTarget),
    flash_start: pick(p, 'flashStart', DEFAULTS.flashStart),
    rtt_address: pick(p, 'rttAddress', DEFAULTS.rttAddress),
  });
}

/* ========================================================================== */
/* OPENOCD_MAKEFILE_TEMPLATE -> Makefile.openocd                               */
/* ========================================================================== */

const OPENOCD_MAKEFILE_LINES = [
  '# OpenOCD Makefile for {project_name}',
  '# Auto-generated by uvprojx2cmake.py',
  '#',
  '# This Makefile provides convenient targets for OpenOCD debugging and programming',
  '#',
  '# Requirements:',
  '#   - OpenOCD installed and in PATH',
  '#   - OpenOCD config files for your interface and target',
  '#',
  '# Usage:',
  '#   make openocd-erase    - Erase entire flash',
  '#   make openocd-prog     - Program the firmware',
  '#   make openocd-gdb      - Start OpenOCD GDB server',
  '#   make openocd-debug    - Start GDB debug session (requires openocd-gdb)',
  '#   make openocd-reset    - Reset target',
  '',
  '# =============================================================================',
  '# Configuration',
  '# =============================================================================',
  '',
  '# OpenOCD root directory (modify this if using custom OpenOCD installation)',
  '# Examples:',
  '#   MounRiver Studio: E:/MounRiver/MounRiver_Studio2/resources/app/resources/win32/components/WCH/OpenOCD/OpenOCD',
  '#   Default Windows: C:/openocd',
  '#   Default Linux: /usr',
  'OPENOCD_ROOT ?= {openocd_root}',
  '',
  '# OpenOCD executable',
  'OPENOCD ?= $(OPENOCD_ROOT)/bin/openocd',
  '',
  '# OpenOCD scripts directory',
  'OPENOCD_SCRIPTS ?= $(OPENOCD_ROOT)/scripts',
  '',
  '# OpenOCD interface config (relative to scripts dir, or absolute path)',
  'OPENOCD_INTERFACE ?= {openocd_interface}',
  '',
  '# OpenOCD target config (relative to scripts dir, or absolute path)',
  'OPENOCD_TARGET ?= {openocd_target}',
  '',
  '# OpenOCD frequency (kHz)',
  'OPENOCD_FREQ ?= 10000',
  '',
  '# GDB configuration',
  'GDB ?= arm-none-eabi-gdb',
  'GDB_PORT ?= 3333',
  'GDB_SCRIPT ?= jlink_gdb.script',
  '',
  '# Build output files',
  'BUILD_DIR ?= build',
  'ELF_FILE ?= $(BUILD_DIR)/{project_name}.elf',
  'HEX_FILE ?= $(BUILD_DIR)/{project_name}.hex',
  'BIN_FILE ?= $(BUILD_DIR)/{project_name}.bin',
  '',
  '# CMake configuration',
  'CMAKE ?= cmake',
  'CMAKE_BUILD_TYPE ?= Debug',
  'CMAKE_GENERATOR ?= Ninja',
  '',
  '# OpenOCD command with scripts path',
  'OPENOCD_CMD = $(OPENOCD) -s $(OPENOCD_SCRIPTS)',
  '',
  '# =============================================================================',
  '# Targets',
  '# =============================================================================',
  '',
  '.PHONY: all build clean cmake openocd-erase openocd-prog openocd-gdb openocd-debug openocd-reset openocd-halt openocd-help',
  '',
  '# Default target: build everything',
  'all: build',
  '',
  '# CMake configure',
  'cmake:',
  '\t@echo "Configuring with CMake..."',
  '\t$(CMAKE) -S . -B $(BUILD_DIR) -G $(CMAKE_GENERATOR) -DCMAKE_BUILD_TYPE=$(CMAKE_BUILD_TYPE)',
  '',
  '# Build the project',
  'build: cmake',
  '\t@echo "Building project..."',
  '\t$(CMAKE) --build $(BUILD_DIR)',
  '',
  '# Clean build directory',
  'clean:',
  '\t@echo "Cleaning build directory..."',
  '\t-rmdir /s /q $(BUILD_DIR) 2>nul || rm -rf $(BUILD_DIR) 2>/dev/null || true',
  '',
  '# Erase entire flash',
  'openocd-erase:',
  '\t@echo "Erasing flash with OpenOCD..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); halt; flash erase_sector 0 0 last; exit"',
  '',
  '# Program firmware (using .elf file, requires pre-built elf file)',
  'openocd-prog:',
  '\t@echo "Programming $(ELF_FILE) with OpenOCD..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); halt; program $(ELF_FILE) verify reset exit"',
  '',
  '# Program firmware (using .bin file with address, requires pre-built bin file)',
  'openocd-prog-bin:',
  '\t@echo "Programming $(BIN_FILE) with OpenOCD..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); halt; program $(BIN_FILE) {flash_start} verify reset exit"',
  '',
  '# Start OpenOCD GDB server',
  'openocd-gdb:',
  '\t@echo "Starting OpenOCD GDB server on port $(GDB_PORT)..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "gdb_port $(GDB_PORT); init; adapter speed $(OPENOCD_FREQ)"',
  '',
  '# Reset target',
  'openocd-reset:',
  '\t@echo "Resetting target..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); reset; exit"',
  '',
  '# Halt target',
  'openocd-halt:',
  '\t@echo "Halting target..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); halt; exit"',
  '',
  '# GDB debug - requires openocd-gdb running in another terminal',
  'openocd-debug: $(ELF_FILE)',
  '\t@echo "Starting GDB debug session..."',
  '\t@echo "Make sure to run \'make openocd-gdb\' in another terminal first"',
  '\t$(GDB) $(ELF_FILE) -x $(GDB_SCRIPT)',
  '',
  '# Test SRAM Read/Write Speed',
  '# Requires test_sram.bin file (generated by --gen-test-bin)',
  'TEST_BIN ?= test_sram.bin',
  'TEST_SRAM_ADDR ?= 0x20000000',
  'TEST_SRAM_SIZE ?= 0x5000',
  '',
  'openocd-sram:',
  '\t@echo "Testing SRAM Speed..."',
  '\t@echo "Loading $(TEST_BIN) to SRAM at $(TEST_SRAM_ADDR)..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); halt; load_image $(TEST_BIN) $(TEST_SRAM_ADDR) bin; exit"',
  '\t@echo "Dumping SRAM to verify..."',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c "init; adapter speed $(OPENOCD_FREQ); halt; dump_image sram_dump.bin $(TEST_SRAM_ADDR) $(TEST_SRAM_SIZE); exit"',
  '\t@echo "SRAM test complete. Compare $(TEST_BIN) and sram_dump.bin"',
  '',
  '# OpenOCD RTT Support',
  '# Note: RTT_ADDR should be obtained from the map file (_SEGGER_RTT symbol)',
  '# Example: RTT_ADDR = 0x20002000',
  'RTT_ADDR ?= {rtt_address}',
  'RTT_SIZE ?= 0x2000',
  'RTT_PORT ?= 9090',
  '',
  'openocd-rtt:',
  '\t@echo "Starting OpenOCD RTT Server on port $(RTT_PORT)..."',
  '\t@echo "RTT Address: $(RTT_ADDR), Size: $(RTT_SIZE)"',
  '\t@echo "Note: Ensure _SEGGER_RTT is placed at $(RTT_ADDR) in your firmware"',
  '\t@echo "Run \'python rtt_logger.py\' in another terminal to capture logs"',
  '\t$(OPENOCD_CMD) -f $(OPENOCD_INTERFACE) -f $(OPENOCD_TARGET) -c \'init; adapter speed $(OPENOCD_FREQ); rtt setup $(RTT_ADDR) $(RTT_SIZE) "SEGGER RTT"; rtt start; rtt polling_interval 1; rtt server start $(RTT_PORT) 0\'',
  '',
  '# Clean generated files',
  'clean-openocd:',
  '\t@echo "Cleaning OpenOCD generated files..."',
  '\t-rm -f test_sram.bin sram_dump.bin rtt_log.txt',
  '',
  '# Help',
  'openocd-help:',
  '\t@echo "Build Targets:"',
  '\t@echo "  all             - Configure and build the project (default)"',
  '\t@echo "  build           - Build the project (configure if needed)"',
  '\t@echo "  cmake           - Run CMake configuration"',
  '\t@echo "  clean           - Clean build directory"',
  '\t@echo ""',
  '\t@echo "OpenOCD Targets:"',
  '\t@echo "  openocd-erase   - Erase entire flash"',
  '\t@echo "  openocd-prog    - Program firmware (.elf)"',
  '\t@echo "  openocd-prog-bin- Program firmware (.bin)"',
  '\t@echo "  openocd-gdb     - Start OpenOCD GDB server"',
  '\t@echo "  openocd-debug   - Start GDB debug session (requires openocd-gdb)"',
  '\t@echo "  openocd-reset   - Reset target"',
  '\t@echo "  openocd-halt    - Halt target"',
  '\t@echo "  openocd-sram    - Test SRAM read/write speed"',
  '\t@echo "  openocd-rtt     - Start OpenOCD RTT server"',
  '\t@echo "  clean-openocd   - Clean generated files"',
  '\t@echo ""',
  '\t@echo "Configuration:"',
  '\t@echo "  OPENOCD_ROOT      = $(OPENOCD_ROOT)"',
  '\t@echo "  OPENOCD           = $(OPENOCD)"',
  '\t@echo "  OPENOCD_SCRIPTS   = $(OPENOCD_SCRIPTS)"',
  '\t@echo "  OPENOCD_INTERFACE = $(OPENOCD_INTERFACE)"',
  '\t@echo "  OPENOCD_TARGET    = $(OPENOCD_TARGET)"',
  '\t@echo "  OPENOCD_FREQ      = $(OPENOCD_FREQ)"',
  '\t@echo "  ELF_FILE          = $(ELF_FILE)"',
  '\t@echo "  HEX_FILE          = $(HEX_FILE)"',
  '\t@echo "  BIN_FILE          = $(BIN_FILE)"',
  '\t@echo "  GDB               = $(GDB)"',
  '\t@echo "  GDB_PORT          = $(GDB_PORT)"',
  '\t@echo "  BUILD_DIR         = $(BUILD_DIR)"',
  '\t@echo "  CMAKE_BUILD_TYPE  = $(CMAKE_BUILD_TYPE)"',
  '\t@echo "  RTT_ADDR          = $(RTT_ADDR)"',
];

/** 可覆盖项：[入参名, 模板里的 make 变量名] */
const OPENOCD_OVERRIDES = [
  ['openocdFreq', 'OPENOCD_FREQ'],
  ['rttPort', 'RTT_PORT'],
  ['gdbPort', 'GDB_PORT'],
  ['buildDir', 'BUILD_DIR'],
  ['cmakeGenerator', 'CMAKE_GENERATOR'],
  ['buildType', 'CMAKE_BUILD_TYPE'],
];

/**
 * 生成 Makefile.openocd。
 * @param {{projectName:string, openocdRoot:string, openocdInterface:string, openocdTarget:string,
 *          flashStart:string, rttAddress:string, openocdFreq?:number, rttPort?:number,
 *          gdbPort?:number, buildDir?:string, cmakeGenerator?:string, buildType?:string}} p
 * @returns {string} CRLF 文本
 */
export function renderOpenocdMakefile(p) {
  const lines = clone(OPENOCD_MAKEFILE_LINES);
  applyOverrides(lines, p, OPENOCD_OVERRIDES);
  return subst(render(lines), {
    project_name: pick(p, 'projectName', ''),
    openocd_root: pick(p, 'openocdRoot', DEFAULTS.openocdRoot),
    openocd_interface: pick(p, 'openocdInterface', DEFAULTS.openocdInterface),
    openocd_target: pick(p, 'openocdTarget', DEFAULTS.openocdTarget),
    flash_start: pick(p, 'flashStart', DEFAULTS.flashStart),
    rtt_address: pick(p, 'rttAddress', DEFAULTS.rttAddress),
  });
}

/* ========================================================================== */
/* generate_gdb_script -> jlink_gdb.script                                     */
/* 源：FileGenerator.generate_gdb_script 里的内联 f-string（不是模块级常量）      */
/* ========================================================================== */

const GDB_SCRIPT_LINES = [
  '# GDB script for {project_name}',
  '# Auto-generated by uvprojx2cmake.py',
  '#',
  '# Usage:',
  '#   1. Start JLink GDB Server: make -f Makefile.jlink jlink-gdb',
  '#   2. In another terminal: make -f Makefile.jlink jlink-debug',
  '#',
  '# Or manually:',
  '#   arm-none-eabi-gdb build/{project_name}.elf -x jlink_gdb.script',
  '',
  '# =============================================================================',
  '# GDB Configuration',
  '# =============================================================================',
  '',
  '# Enable logging to file',
  '# set logging file out.txt',
  'set logging on',
  '',
  '# Pretty print for structures',
  'set print pretty on',
  '',
  '# Disable pagination',
  'set pagination off',
  '',
  '# Clear previous breakpoints and displays',
  'delete breakpoints',
  'undisplay',
  '',
  '# =============================================================================',
  '# Custom Functions',
  '# =============================================================================',
  '',
  '# Execute \'next\' 500 times',
  'define do_more',
  '    set $i=500',
  '    while($i>0)',
  '        next',
  '        # step',
  '        set $i-=1',
  '    end',
  'end',
  '',
  '# Execute until stack pointer changes (function exit)',
  'define do_func',
  '    # display/x $sp',
  '    set $entry_frame=$sp',
  '    while($sp<=$entry_frame)',
  '        next',
  '        #step',
  '    end',
  'end',
  '',
  '# =============================================================================',
  '# Connection & Debug Session',
  '# =============================================================================',
  '',
  '# Connect to GDB server',
  'target remote localhost:{gdb_port}',
  '',
  '# Reset and halt target',
  'monitor reset halt',
  '',
  '# Load firmware',
  'load',
  '',
  '# Set breakpoint at main',
  'break main',
  '',
  '# Continue execution',
  'continue',
];

/**
 * 生成 jlink_gdb.script。
 * @param {{projectName:string, gdbPort?:number}} p
 * @returns {string} CRLF 文本
 */
export function renderGdbScript(p) {
  const lines = clone(GDB_SCRIPT_LINES);
  // Python: generate_gdb_script(gdb_port=3333) —— 模板里只有一处 {gdb_port}
  if (p && p.gdbPort !== undefined && p.gdbPort !== null) {
    const idx = lines.findIndex((l) => l.startsWith('target remote localhost:'));
    if (idx < 0) {
      throw new Error('template anchor not found: target remote localhost:');
    }
    lines[idx] = 'target remote localhost:' + String(p.gdbPort);
  }
  return subst(render(lines), {
    project_name: pick(p, 'projectName', ''),
    gdb_port: pick(p, 'gdbPort', DEFAULTS.gdbPort),
  });
}

/* ========================================================================== */
/* RTT_LOGGER_TEMPLATE -> rtt_logger.py                                        */
/* 由 _generate_rtt_logger() 原样写出（不做任何 .format()，花括号都是字面量）      */
/* ========================================================================== */

const RTT_LOGGER_LINES = [
  'import socket',
  'import sys',
  'import time',
  '',
  'HOST = \'127.0.0.1\'',
  'PORT = 9090',
  'LOG_FILE = \'rtt_log.txt\'',
  '',
  'def main():',
  '    print(f"Connecting to OpenOCD RTT server at {HOST}:{PORT}...")',
  '    try:',
  '        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:',
  '            s.connect((HOST, PORT))',
  '            print(f"Connected! Logging to {LOG_FILE}...")',
  '            print("Speed monitoring started (update every 1s)...")',
  '            ',
  '            with open(LOG_FILE, \'wb\') as f:',
  '                total_bytes = 0',
  '                start_time = time.time()',
  '                last_check_time = start_time',
  '                last_bytes = 0',
  '',
  '                while True:',
  '                    data = s.recv(4096)  # Increase buffer size for better throughput',
  '                    if not data:',
  '                        break',
  '                    ',
  '                    # Write to file',
  '                    f.write(data)',
  '                    f.flush()  # Reduce flush frequency for better performance',
  '                    ',
  '                    # Update counters',
  '                    data_len = len(data)',
  '                    total_bytes += data_len',
  '                    ',
  '                    # Speed calculation',
  '                    current_time = time.time()',
  '                    if current_time - last_check_time >= 1.0:',
  '                        interval = current_time - last_check_time',
  '                        bytes_in_interval = total_bytes - last_bytes',
  '                        speed_kbps = (bytes_in_interval / 1024) / interval',
  '                        avg_speed_kbps = (total_bytes / 1024) / (current_time - start_time)',
  '                        ',
  '                        sys.stdout.write(f"\\rSpeed: {speed_kbps:8.2f} KB/s | Avg: {avg_speed_kbps:8.2f} KB/s | Total: {total_bytes/1024:8.2f} KB")',
  '                        sys.stdout.flush()',
  '                        ',
  '                        last_check_time = current_time',
  '                        last_bytes = total_bytes',
  '',
  '    except ConnectionRefusedError:',
  '        print("\\nError: Could not connect to OpenOCD. Make sure OpenOCD is running with RTT server enabled.")',
  '    except KeyboardInterrupt:',
  '        print("\\nLogging stopped by user.")',
  '        print(f"Total received: {total_bytes/1024:.2f} KB")',
  '    except Exception as e:',
  '        print(f"\\nError: {e}")',
  '',
  'if __name__ == "__main__":',
  '    main()',
];

/**
 * 生成 rtt_logger.py（RTT_LOGGER_TEMPLATE 原样，无参数）。
 * @returns {string} CRLF 文本
 */
export function renderRttLogger() {
  return render(clone(RTT_LOGGER_LINES));
}

/* ========================================================================== */
/* generate_test_bin -> test_sram.bin                                          */
/* Python: pattern = bytes(i % 256 for i in range(size))                       */
/* ========================================================================== */

/**
 * 生成 test_sram.bin 的内容。
 * @param {number} [size=20480] 字节数
 * @returns {Uint8Array} 长度 size、内容 i % 256
 */
export function makeTestSramBin(size) {
  const n = size === undefined || size === null ? DEFAULTS.testBinSize : Number(size);
  if (!Number.isFinite(n) || n < 0) {
    throw new Error('makeTestSramBin: invalid size: ' + size);
  }
  const len = Math.trunc(n);
  const out = new Uint8Array(len);
  for (let i = 0; i < len; i++) {
    out[i] = i % 256;
  }
  return out;
}
