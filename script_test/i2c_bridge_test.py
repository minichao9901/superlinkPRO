"""USB→I2C 转发桥（HID 0x36）自检 / 手动工具。

协议真源：firmware/application_5301/src/i2c_bridge/i2c_bridge_proto.h
网页侧说明：docs/web-handoff-i2c-bridge.md

接线（HPM5301EVKLite）：
    I2C SDA ← J3[21]  PA28        I2C SCL ← J3[19]  PA29
    GND     ← J3[6]/[9]/[14]…     器件 VCC ← J3[1]/[17] 3V3
    ⚠️ PA28(SDA) 板上**没有**上拉，通常要外接 4.7k~10k 到 3.3V；
       PA29(SCL) 有 R6 10k。先跑 `pintest` 看两根线能不能被拉到高。

用法：
    python i2c_bridge_test.py info                      # 状态 + 配置
    python i2c_bridge_test.py enable 1                  # 使能（配引脚 + 初始化 I2C3）
    python i2c_bridge_test.py pintest                   # 引脚/上拉自检（接线第一件事）
    python i2c_bridge_test.py scan                      # 扫 0x08..0x77
    python i2c_bridge_test.py rd 0x50 0x00 8            # 读：写子地址 0x00 后读 8 字节
    python i2c_bridge_test.py wr 0x50 0x10 1 2 3        # 写：子地址 0x10 + 3 字节
    python i2c_bridge_test.py rr 0x50 4                 # 纯读（不发子地址）
    python i2c_bridge_test.py wrr 0x50 0xA5             # 纯写（不发子地址）
    python i2c_bridge_test.py probe 0x50                # 只发地址，问 ACK/NACK
    python i2c_bridge_test.py err                       # 错误路径（NACK/越界/忙）
    python i2c_bridge_test.py cfg --scl-hz 400000 --pullup 1
    python i2c_bridge_test.py dbg                       # 寄存器现场
"""
import argparse
import os
import struct
import sys
import threading
import time

import hid

try:                                    # Windows 控制台默认 GBK，中文/符号会直接抛异常
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

VID, PID = 0x0D28, 0x0204
HID_CMD = 0x36
ACT = {'STATUS': 0, 'ENABLE': 1, 'RESET': 2, 'SET_CFG': 3, 'GET_CFG': 4,
       'XFER': 5, 'RESULT': 6, 'SCAN': 7, 'DBG': 10, 'PINTEST': 11, 'BITPROBE': 12}
ERR_NAME = {0: 'OK', 1: 'E_DISABLED', 2: 'E_BUSY', 3: 'E_NO_ADDR(地址没 ACK)',
            4: 'E_NO_ACK(数据被 NACK)', 5: 'E_TIMEOUT', 6: 'E_RANGE(参数越界)',
            7: 'E_BAD_FRAME', 8: 'E_BUS_STUCK(总线被拉死)', 9: 'E_STATE'}

# 状态字位
ST_ENABLED, ST_PENDING, ST_BUS_OK = 1 << 0, 1 << 1, 1 << 2
ST_SHIFT_ERR, ST_SHIFT_DONE, ST_SHIFT_CMD = 8, 16, 24

SCAN_FIRST, SCAN_LAST = 0x08, 0x77


def watchdog(sec):
    def _f():
        time.sleep(sec)
        print("!! WATCHDOG TIMEOUT")
        os._exit(9)
    threading.Thread(target=_f, daemon=True).start()


def open_hid():
    for _ in range(30):
        cand = [i for i in hid.enumerate(VID, PID) if i.get("usage_page") == 0xFF00]
        if cand:
            d = hid.device()
            d.open_path(cand[0]["path"])
            d.set_nonblocking(1)
            time.sleep(0.2)
            return d
        time.sleep(0.3)
    raise RuntimeError("no custom HID interface (VID 0D28 / PID 0204)")


def xfer(dev, data, tmo=3.0):
    """data = [action, ...]；返回剥掉 Report ID 的响应（r[1] = cmd, r[2] = action）。"""
    req = [0x01, 2 + len(data), HID_CMD] + list(data)
    assert len(req) <= 64, "请求 %d B 超过 64" % len(req)
    req += [0] * (64 - len(req))
    dev.write(req)
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = dev.read(64, timeout_ms=200)
        if r and r[0] == 0x02 and r[2] == HID_CMD:
            return list(r)[1:]
    return None


def u32(b):
    return int.from_bytes(bytes(b), "little")


class Resp:
    def __init__(self, r):
        self.raw = r
        self.action = r[2] if r else None
        self.status = u32(r[3:7]) if r else 0
        self.data = r[7:] if r else []
        self.cmd_rc = (self.status >> ST_SHIFT_CMD) & 0xFF
        self.last_err = (self.status >> ST_SHIFT_ERR) & 0xFF
        self.pending = bool(self.status & ST_PENDING)
        self.done = (self.status >> ST_SHIFT_DONE) & 0xFF

    def __repr__(self):
        return ("status=0x%08X [%s%s%s] lastErr=%s cmdRc=%s done=%d"
                % (self.status,
                   'EN ' if self.status & ST_ENABLED else '',
                   'PEND ' if self.pending else '',
                   'BUSOK' if self.status & ST_BUS_OK else '',
                   ERR_NAME.get(self.last_err, self.last_err),
                   ERR_NAME.get(self.cmd_rc, self.cmd_rc), self.done))


def xf(dev, data):
    r = xfer(dev, data)
    if r is None:
        raise RuntimeError("探针没响应（命令 0x%02X action %d）" % (HID_CMD, data[0]))
    return Resp(r)


def wait_result(dev, tmo=2.0, poll=0.002):
    """轮询 RESULT 直到 PENDING 清零；返回 (err, data)。"""
    t0 = time.time()
    while time.time() - t0 < tmo:
        r = xf(dev, [ACT['RESULT']])
        if not r.pending:
            err = r.data[0] if r.data else 0xFF
            n = r.data[1] if len(r.data) > 1 else 0
            return err, bytes(r.data[2:2 + n])
        time.sleep(poll)
    return 0xFE, b''            # 超时（探针侧没做完）


def do_xfer(dev, dev_addr, addr=None, wr=b'', rd=0, quiet=False):
    """一次事务：sub_addr(addr) + wr + 读 rd 字节。返回 (err, data)。"""
    addr = addr or b''
    p = [0, dev_addr & 0x7F, len(addr), len(wr), rd]
    p += list(addr.ljust(4, b'\x00')[:4]) + list(wr)
    r = xf(dev, [ACT['XFER']] + p)
    if r.cmd_rc != 0:
        return r.cmd_rc, b''      # 没登记上（忙/未使能/越界）
    err, data = wait_result(dev)
    if not quiet:
        print("  %s" % (ERR_NAME.get(err, err)))
    return err, data


def cmd_info(dev, args):
    r = xf(dev, [ACT['STATUS']])
    print("状态：%s" % r)
    w = [u32(r.data[i * 4:i * 4 + 4]) for i in range(10)]
    names = ['frames_ok', 'frames_err', 'bytes_tx', 'bytes_rx', 'nack_addr',
             'nack_data', 'timeouts', 'bus_recover', 'actual_scl_hz', 'last_ticks']
    print("计数：" + "  ".join("%s=%d" % (n, v) for n, v in zip(names, w)))
    print("      实际 SCL = %.0f kHz，最近一次事务 = %.1f µs"
          % (w[8] / 1000.0, w[9] / 24.0))
    c = xf(dev, [ACT['GET_CFG']])
    if len(c.data) >= 16:
        scl, pullup, retries, flags = u32(c.data[0:4]), c.data[4], c.data[5], c.data[6]
        actual = u32(c.data[8:12])
        print("配置：scl_hz=%d(实际 %d) pullup=%d retries=%d flags=%d"
              % (scl, actual, pullup, retries, flags))
    return 0


def cmd_enable(dev, args):
    r = xf(dev, [ACT['ENABLE'], 1 if args.on else 0])
    print("ENABLE=%d → %s" % (args.on, r))
    return 0 if r.cmd_rc == 0 else 1


def cmd_cfg(dev, args):
    r = xf(dev, [ACT['GET_CFG']])
    cur = bytearray(r.data[:16])
    if args.scl_hz is not None:
        cur[0:4] = struct.pack('<I', args.scl_hz)
    if args.pullup is not None:
        cur[4] = args.pullup
    if args.retries is not None:
        cur[5] = args.retries
    r = xf(dev, [ACT['SET_CFG']] + list(cur))
    print("SET_CFG → %s" % r)
    if not args.no_info:
        cmd_info(dev, args)
    return 0 if r.cmd_rc == 0 else 1


def cmd_pintest(dev, args):
    r = xf(dev, [ACT['PINTEST']])
    if r.cmd_rc != 0:
        print("PINTEST 失败：%s（总线忙？先等一下再试）" % ERR_NAME.get(r.cmd_rc, r.cmd_rc))
        return 1
    v = u32(r.data[0:4])
    print("空闲电平（控制器线感知）：SDA=%d SCL=%d" % (v & 1, (v >> 1) & 1))
    print("打开内部上拉后        ：SDA=%d SCL=%d" % ((v >> 2) & 1, (v >> 3) & 1))
    scl_act, sda_act = (v >> 16) & 1, (v >> 17) & 1
    print("真实事务中曾拉低      ：SCL=%d SDA=%d   （1 = 我们的脚确实在驱动总线）"
          % (scl_act, sda_act))
    prob = (v >> 8) & 0xFF
    msgs = []
    if prob & 0x01: msgs.append("SCL 在事务里从未被拉低（桥侧驱动异常）")
    if prob & 0x02: msgs.append("空闲 SCL 常低（被拽住/短路）")
    if prob & 0x04: msgs.append("空闲 SDA 常低（被拽住/短路）")
    print("结论：%s" % ("桥这一侧一切正常 ✓（没 ACK 就往器件侧查：接线/供电/地址/上拉）"
                      if not msgs else "；".join(msgs)))
    return 0 if (not msgs and scl_act == 1) else 1


def cmd_scan(dev, args):
    r = xf(dev, [ACT['SCAN']])
    if r.cmd_rc != 0:
        print("SCAN 被拒：%s" % ERR_NAME.get(r.cmd_rc, r.cmd_rc))
        return 1
    err, bm = wait_result(dev, tmo=5.0)
    if err != 0:
        print("SCAN 失败：%s" % ERR_NAME.get(err, err))
        return 1
    found = []
    for i in range(len(bm) * 8):
        if bm[i >> 3] & (1 << (i & 7)):
            found.append(SCAN_FIRST + i)
    print("扫描 0x%02X..0x%02X：%s" % (SCAN_FIRST, SCAN_LAST,
          " ".join("0x%02X" % a for a in found) if found else "（没有器件应答）"))
    return 0 if found else 1


def cmd_probe(dev, args):
    err, _ = do_xfer(dev, args.dev, None, b'', 0, quiet=True)
    print("地址 0x%02X：%s" % (args.dev, ERR_NAME.get(err, err)))
    return 0 if err == 0 else 3       # 3 = 没应答


def cmd_rd(dev, args):
    addr = bytes.fromhex(args.reg.replace('0x', '').zfill(2 * args.regbytes))
    err, data = do_xfer(dev, args.dev, addr, b'', args.n, quiet=True)
    if err != 0:
        print("读 0x%02X[%s] 失败：%s" % (args.dev, args.reg, ERR_NAME.get(err, err)))
        return 1
    print("读 0x%02X[%s] × %d → %s" % (args.dev, args.reg, args.n, data.hex(' ')))
    return 0


def cmd_wr(dev, args):
    addr = bytes.fromhex(args.reg.replace('0x', '').zfill(2 * args.regbytes))
    wr = bytes(int(x, 0) & 0xFF for x in args.bytes)
    err, _ = do_xfer(dev, args.dev, addr, wr, 0, quiet=True)
    print("写 0x%02X[%s] ← %s：%s" % (args.dev, args.reg, wr.hex(' '), ERR_NAME.get(err, err)))
    return 0 if err == 0 else 1


def cmd_rr(dev, args):
    err, data = do_xfer(dev, args.dev, None, b'', args.n, quiet=True)
    print("纯读 0x%02X × %d：%s" % (args.dev, args.n,
          data.hex(' ') if err == 0 else ERR_NAME.get(err, err)))
    return 0 if err == 0 else 1


def cmd_wrr(dev, args):
    wr = bytes(int(x, 0) & 0xFF for x in args.bytes)
    err, _ = do_xfer(dev, args.dev, None, wr, 0, quiet=True)
    print("纯写 0x%02X ← %s：%s" % (args.dev, wr.hex(' '), ERR_NAME.get(err, err)))
    return 0 if err == 0 else 1


def cmd_dbg(dev, args):
    r = xf(dev, [ACT['DBG']])
    names = ['CTRL', 'STATUS', 'ADDR', 'CMD', 'SETUP', 'INTEN',
             'PA28(FUNC|PAD)', 'PA29(FUNC|PAD)', 'cfg.scl_hz', 'actual_scl_hz',
             'status_word', 'done_cnt']
    vals = [u32(r.data[i * 4:i * 4 + 4]) for i in range(12)]
    for n, v in zip(names, vals):
        print("  %-16s 0x%08X" % (n, v))
    print("  busy_rej=%d" % u32(r.data[48:52]))
    return 0


def cmd_bitprobe(dev, args):
    r = xf(dev, [ACT['BITPROBE'], args.dev & 0x7F])
    bits = u32(r.data[0:4])
    print("纯 GPIO 位翻转探测 0x%02X（不经过 I2C 外设）：" % args.dev)
    print("  地址位回读 bit7..0 = %s   ACK 位 = %d（0 = 从机拉低 = 有器件）"
          % ("".join(str((bits >> i) & 1) for i in range(7, -1, -1)), (bits >> 8) & 1))
    print("  事务后 SDA/SCL 电平 = %d / %d（都该是 1）" % ((bits >> 9) & 1, (bits >> 10) & 1))
    print("  结果：%s" % ("收到 ACK ✓ 器件在" if ((bits >> 8) & 1) == 0 else "没有 ACK"))
    return 0 if ((bits >> 8) & 1) == 0 else 3


def cmd_eeprom(dev, args):
    """AT24Cxx 全流程验收：扫描 → 基线读 → 页写 → 回读对账 → 还原 → 长块读。

    这是本桥的**数据面门禁**（不需要人手算字节）：跑通说明"写进去的字节原样读回来、
    地址自增、多字节块读都对"。默认只动一个 8 字节页（--scratch 指定起始地址），
    跑完会把原内容写回去。
    """
    fails = []

    def chk(cond, note, detail=''):
        print("  %-36s %-5s %s" % (note, "[ok]" if cond else "[FAIL]", detail))
        if not cond:
            fails.append(note)

    print("=== 0. RESET：清计数器（顺带验证总线恢复动作）===")
    r = xf(dev, [ACT['RESET']])
    chk(r.cmd_rc == 0, "RESET 被受理", "cmdRc=%s" % r.cmd_rc)
    err, _ = wait_result(dev, tmo=2.0)
    chk(err == 0, "总线恢复完成", ERR_NAME.get(err, err))
    s = xf(dev, [ACT['STATUS']])
    w = [u32(s.data[i * 4:i * 4 + 4]) for i in range(10)]
    chk(w[0] == 1 and w[1] == 0 and w[7] == 1,
        "计数器已清零（只留这一笔）", "frames_ok=%d err=%d recover=%d" % (w[0], w[1], w[7]))

    pat = bytes([0xA5, 0x5A, 0xDE, 0xAD, 0xBE, 0xEF, 0x12, 0x34][:args.n])
    print("\n=== 1. 扫描总线（应看到 0x%02X）===" % args.dev)
    r = xf(dev, [ACT['SCAN']])
    err, bm = wait_result(dev, tmo=5.0)
    found = [SCAN_FIRST + i for i in range(len(bm) * 8)
             if i < 128 and (bm[i >> 3] & (1 << (i & 7)))]
    chk(err == 0 and args.dev in found, "0x%02X 在扫描结果里" % args.dev,
        "扫到：" + " ".join("0x%02X" % a for a in found))

    print("\n=== 2. 读基线（先看看这块地址原来是什么）===")
    err, orig = do_xfer(dev, args.dev, bytes([args.scratch & 0xFF]), b'', args.n, quiet=True)
    chk(err == 0 and len(orig) == args.n, "基线读成功",
        "%s" % orig.hex(' ') if err == 0 else ERR_NAME.get(err, err))

    print("\n=== 3. 写 + 回读对账（EEPROM 写周期 ~5 ms，脚本等 10 ms）===")
    err, _ = do_xfer(dev, args.dev, bytes([args.scratch & 0xFF]), pat, 0, quiet=True)
    chk(err == 0, "写入被 ACK", ERR_NAME.get(err, err))
    time.sleep(0.01)
    err, back = do_xfer(dev, args.dev, bytes([args.scratch & 0xFF]), b'', args.n, quiet=True)
    chk(err == 0 and back == pat, "读回的字节与写入一致",
        "%s (期望 %s)" % (back.hex(' '), pat.hex(' ')))

    print("\n=== 4. 还原原内容 ===")
    err, _ = do_xfer(dev, args.dev, bytes([args.scratch & 0xFF]), orig, 0, quiet=True)
    time.sleep(0.01)
    err, back = do_xfer(dev, args.dev, bytes([args.scratch & 0xFF]), b'', args.n, quiet=True)
    chk(err == 0 and back == orig, "还原成功", "%s" % back.hex(' '))

    print("\n=== 5. 长块读（54 B，跨页/地址自增）===")
    err, blk = do_xfer(dev, args.dev, bytes([0x00]), b'', 54, quiet=True)
    chk(err == 0 and len(blk) == 54, "54 B 一次读完", "前 16 B：%s" % blk[:16].hex(' '))

    print("\n=== 6. 计数器 ===")
    s = xf(dev, [ACT['STATUS']])
    w = [u32(s.data[i * 4:i * 4 + 4]) for i in range(10)]
    chk(w[0] >= 6 and w[1] == 0, "frames_ok=%d / frames_err=%d（错误必须 0）" % (w[0], w[1]))
    chk(w[3] >= 54 + args.n * 3, "bytes_rx=%d（读字节数在累加）" % w[3])
    print("      最近一次事务 %.1f µs @ %.0f kHz" % (w[9] / 24.0, w[8] / 1000.0))

    print("\n%s" % ("全部通过 ✓" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
    return 0 if not fails else 1


def cmd_err(dev, args):
    """错误路径自检：不需要真的接器件也能跑（NACK/越界/忙）。"""
    fails = []

    def chk(cond, note, detail=''):
        print("  %-34s %-5s %s" % (note, "[ok]" if cond else "[FAIL]", detail))
        if not cond:
            fails.append(note)

    print("=== 1. 没接器件的地址必须回 E_NO_ADDR ===")
    err, _ = do_xfer(dev, 0x7E, None, b'', 0, quiet=True)
    chk(err == 3, "probe 0x7E → E_NO_ADDR", ERR_NAME.get(err, err))

    print("\n=== 2. 参数越界必须被拒（E_RANGE）===")
    r = xf(dev, [ACT['XFER'], 0x01, 0x50, 0, 0, 0])           # flags ≠ 0
    chk(r.cmd_rc == 6, "flags=1 → E_RANGE", "cmdRc=%s" % r.cmd_rc)
    r = xf(dev, [ACT['XFER'], 0x00, 0x50, 5, 0, 0])           # addr_len=5
    chk(r.cmd_rc == 6, "addr_len=5 → E_RANGE", "cmdRc=%s" % r.cmd_rc)
    r = xf(dev, [ACT['XFER'], 0x00, 0x50, 0, 52, 0] + [0] * 52)  # wr_len=52
    chk(r.cmd_rc == 6, "wr_len=52 → E_RANGE", "cmdRc=%s" % r.cmd_rc)
    r = xf(dev, [ACT['XFER'], 0x00, 0x50, 0, 0, 55])          # rd_len=55
    chk(r.cmd_rc == 6, "rd_len=55 → E_RANGE", "cmdRc=%s" % r.cmd_rc)

    print("\n=== 3. 未知 action 不能被当成合法命令 ===")
    r = xf(dev, [0x7F])
    chk(r.cmd_rc == 7, "action 0x7F → E_BAD_FRAME", "cmdRc=%s" % r.cmd_rc)

    print("\n=== 4. 关掉桥之后所有请求都必须被拒 ===")
    xf(dev, [ACT['ENABLE'], 0])
    r = xf(dev, [ACT['XFER'], 0x00, 0x50, 0, 0, 1])
    chk(r.cmd_rc == 1, "XFER@disabled → E_DISABLED", "cmdRc=%s" % r.cmd_rc)
    r = xf(dev, [ACT['SCAN']])
    chk(r.cmd_rc == 1, "SCAN@disabled → E_DISABLED", "cmdRc=%s" % r.cmd_rc)
    xf(dev, [ACT['ENABLE'], 1])

    print("\n=== 5. 事务进行中再发一条必须回 E_BUSY ===")
    # 用一笔**长事务**占住总线（54 B 读 @100 kHz ≈ 5 ms），紧接着再发一条。
    # 早先用"扫全总线期间发 XFER"来测，但扫描按 16 个地址/轮分摊，400 kHz 下不到 1 ms
    # 就跑完了 —— 后一条请求到的时候早就不忙了（测试本身不严谨，不是固件的问题）。
    xf(dev, [ACT['SET_CFG']] + list(xf(dev, [ACT['GET_CFG']]).data[:16]))   # 保持当前配置
    r = xf(dev, [ACT['XFER'], 0x00, 0x50, 1, 0, 54, 0x00])
    r2 = xf(dev, [ACT['XFER'], 0x00, 0x50, 0, 0, 1])
    busy_seen = (r.cmd_rc == 0) and (r2.cmd_rc == 2)
    wait_result(dev, tmo=5.0)
    chk(busy_seen, "长事务期间 XFER → E_BUSY",
        "第一条 cmdRc=%s / 第二条 cmdRc=%s" % (r.cmd_rc, r2.cmd_rc))

    print("\n%s" % ("全部通过 ✓" if not fails else "失败 %d 项：%s" % (len(fails), fails)))
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser(description="USB→I2C 桥自检/手动工具（HID 0x36）")
    sub = ap.add_subparsers(dest='cmd', required=True)

    sub.add_parser('info').set_defaults(fn=cmd_info)
    p = sub.add_parser('enable'); p.add_argument('on', type=int, choices=[0, 1]); p.set_defaults(fn=cmd_enable)
    p = sub.add_parser('cfg')
    p.add_argument('--scl-hz', type=int, default=None, help='0/100000/400000/1000000（档位选择器）')
    p.add_argument('--pullup', type=int, choices=[0, 1], default=None)
    p.add_argument('--retries', type=int, default=None)
    p.add_argument('--no-info', action='store_true')
    p.set_defaults(fn=cmd_cfg)
    sub.add_parser('pintest').set_defaults(fn=cmd_pintest)
    sub.add_parser('scan').set_defaults(fn=cmd_scan)
    sub.add_parser('dbg').set_defaults(fn=cmd_dbg)
    sub.add_parser('err').set_defaults(fn=cmd_err)
    p = sub.add_parser('eeprom', help='AT24Cxx 全流程验收（数据面门禁）')
    p.add_argument('--dev', type=lambda s: int(s, 0), default=0x50)
    p.add_argument('--scratch', type=lambda s: int(s, 0), default=0xE0, help='用来读写的临时地址')
    p.add_argument('--n', type=int, default=8, help='页写字节数（24C02 页 = 8 B）')
    p.set_defaults(fn=cmd_eeprom)

    def add_dev(p):
        p.add_argument('dev', type=lambda s: int(s, 0), help='7 位从机地址，如 0x50')

    p = sub.add_parser('probe'); add_dev(p); p.set_defaults(fn=cmd_probe)
    p = sub.add_parser('bitprobe', help='纯 GPIO 位翻转探测（和外设路径交叉验证）')
    add_dev(p); p.set_defaults(fn=cmd_bitprobe)
    p = sub.add_parser('rd'); add_dev(p)
    p.add_argument('reg', help='子地址，如 0x00 或 0x0100')
    p.add_argument('n', type=int, nargs='?', default=1, help='读多少字节（≤54）')
    p.add_argument('--regbytes', type=int, default=None, help='子地址字节数（默认按 reg 的位数推）')
    p.set_defaults(fn=cmd_rd)
    p = sub.add_parser('wr'); add_dev(p)
    p.add_argument('reg')
    p.add_argument('bytes', nargs='+', help='要写的字节（十进制/0x 都行）')
    p.add_argument('--regbytes', type=int, default=None)
    p.set_defaults(fn=cmd_wr)
    p = sub.add_parser('rr'); add_dev(p); p.add_argument('n', type=int); p.set_defaults(fn=cmd_rr)
    p = sub.add_parser('wrr'); add_dev(p); p.add_argument('bytes', nargs='+'); p.set_defaults(fn=cmd_wrr)

    args = ap.parse_args()
    if getattr(args, 'regbytes', None) is None and hasattr(args, 'reg'):
        # 长度按写出来的位数推：0x00 → 1 B，0x0100 → 2 B
        args.regbytes = max(1, (len(args.reg.replace('0x', '')) + 1) // 2)

    dev = open_hid()
    return args.fn(dev, args)


if __name__ == "__main__":
    watchdog(120)
    sys.exit(main())
