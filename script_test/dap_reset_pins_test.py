"""生产源码级回归：EVKLite 的目标 nRESET(PA27) 与 nTRST(PA08) 必须互相独立。

背景（审查 R03）：合并仓的固件源码一度落后于外部 akaLinkPro 仓的
`10adbc7/96e5c6f/2dc273c` —— 板级仍把 PA08 定义成 `BOARD_PIN_nRESET2`，
`PIN_nRESET_OUT()` 同时拉 PA27+PA08、`PIN_nTRST_OUT()` 不动 GPIO、上电默认开漏。
网页与交接文档却说"两路互不联动、默认推挽"。这三者必须一致。

这个测试直接读**生产头文件**做结构检查（不编译、不碰硬件），
把"网页说的语义"钉在源码上；真正的电气/波形验收另由真机测试负责。

跑：python script_test/dap_reset_pins_test.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# make 调的是 `python`（本机默认 GBK 控制台）：不切 UTF-8 会在打印 ✓ 时 UnicodeEncodeError。
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf8', errors='replace')
BOARD = ROOT / 'firmware/application_5301/boards/hpm5301evklite/board.h'
CONFIG = ROOT / 'firmware/application_5301/src/dap/DAP_config.h'
DAP = ROOT / 'firmware/application_5301/src/dap/DAP.c'

fails = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ✓ {name}')
    else:
        fails.append(name)
        print(f'  ✗ {name} {detail}')


def define(text, name):
    match = re.search(rf'^#define\s+{name}\s+\(?([^)\\\n/]+?)\)?\s*(?:/\*.*)?$', text, re.M)
    return match.group(1).strip() if match else None


def body(text, name):
    """取函数体（从**函数定义**后的第一个 { 到配对 }；不能只按名字找，注释里也会出现）。"""
    match = re.search(rf'\b(?:void|uint32_t|uint8_t|static)\s+{re.escape(name)}\s*\(', text)
    if not match:
        raise ValueError(f'找不到 {name} 的定义')
    open_at = text.index('{', match.end())
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == '{':
            depth += 1
        elif text[index] == '}':
            depth -= 1
            if depth == 0:
                return text[open_at:index + 1]
    raise ValueError(f'{name} 的函数体没有闭合')


board = BOARD.read_text(encoding='utf8')
config = CONFIG.read_text(encoding='utf8')
dap = DAP.read_text(encoding='utf8')

print('板级定义')
check('不再有 BOARD_PIN_nRESET2（旧的联动路径）', 'BOARD_PIN_nRESET2' not in board)
check('BOARD_PIN_JTRST = PA08', define(board, 'BOARD_PIN_JTRST') == 'IOC_PAD_PA08',
      define(board, 'BOARD_PIN_JTRST'))
check('BOARD_PIN_nRESET = PA27', define(board, 'BOARD_PIN_nRESET') == 'IOC_PAD_PA27',
      define(board, 'BOARD_PIN_nRESET'))
check('BOARD_HAS_JTRST = 1', define(board, 'BOARD_HAS_JTRST') == '1', define(board, 'BOARD_HAS_JTRST'))
check('BOARD_JTRST_OPEN_DRAIN = 1', define(board, 'BOARD_JTRST_OPEN_DRAIN') == '1',
      define(board, 'BOARD_JTRST_OPEN_DRAIN'))
check('BOARD_HAS_NRESET_OD = 1', define(board, 'BOARD_HAS_NRESET_OD') == '1',
      define(board, 'BOARD_HAS_NRESET_OD'))
check('nRESET 低有效', define(board, 'BOARD_NRESET_ACTIVE_LOW') == '1',
      define(board, 'BOARD_NRESET_ACTIVE_LOW'))

print('DAP 引脚实现')
reset_out = body(config, 'PIN_nRESET_OUT')
check('PIN_nRESET_OUT 只驱动 BOARD_PIN_nRESET',
      'BOARD_PIN_nRESET' in reset_out and 'BOARD_PIN_JTRST' not in reset_out
      and 'nRESET2' not in reset_out)
trst_out = body(config, 'PIN_nTRST_OUT')
check('PIN_nTRST_OUT 真的驱动 BOARD_PIN_JTRST', 'BOARD_PIN_JTRST' in trst_out)
trst_in = body(config, 'PIN_nTRST_IN')
check('PIN_nTRST_IN 回读 PA08 的输出锁存（不是恒 1）',
      'BOARD_PIN_JTRST' in trst_in and 'gpio_get_pin_output_status' in trst_in)
apply_cfg = body(config, 'DAP_APPLY_RESET_PAD_CONFIG')
check('切 nRESET 驱动模式不会碰 TRST 的 pad', 'BOARD_PIN_nRESET' in apply_cfg
      and 'BOARD_PIN_JTRST' not in apply_cfg)
for setup in ('PORT_JTAG_SETUP', 'PORT_SWD_SETUP', 'PORT_OFF'):
    text = body(config, setup)
    check(f'{setup} 的 nRESET 写不再连带 JTRST',
          'BOARD_PIN_JTRST' not in text or re.search(r'BOARD_PIN_JTRST\)[^\n]*,\s*1\)', text) is not None)
check('PORT_OFF 会释放 TRST（关调试口不留断言）',
      re.search(r'#if BOARD_HAS_JTRST\s*\n\s*gpio_write_pin\([^;]*BOARD_PIN_JTRST[^;]*1\)',
                body(config, 'PORT_OFF')) is not None)

print('DAP 能力与默认模式')
check('DAP 代码不再用 BOARD_PIN_nRESET2 当能力开关', 'BOARD_PIN_nRESET2' not in dap)
check('支持开漏/推挽切换用 BOARD_HAS_NRESET_OD',
      'BOARD_HAS_NRESET_OD' in dap and re.search(
          r'#if\s+BOARD_HAS_NRESET_OD\s*\n\s*volatile\s+uint8_t\s+dap_nrst_open_drain\s*=\s*0U', dap) is not None,
      '上电默认必须推挽（=0U）')
check('DAP_ResetOutputModeSupported 走同一能力宏',
      'BOARD_HAS_NRESET_OD' in body(dap, 'DAP_ResetOutputModeSupported'))

print()
if fails:
    print(f'✗ {len(fails)} 项失败：{fails}')
    sys.exit(1)
print('✓ nRESET/nTRST 独立、默认推挽、能力位一致')
