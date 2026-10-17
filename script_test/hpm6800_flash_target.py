"""Flash a firmware image into the HPM6800EVK's QSPI NOR through the akaLinkPro
probe (CMSIS-DAP + OpenOCD), i.e. the same probe that debugs it.

The board's OpenOCD config provides the hpm_xpi flash driver, so no J-Link and
no jumper juggling is needed. The XPI clock is only initialised by the
reset-init event, so the sequence is reset-init -> halt -> write -> verify.

Usage:
  python hpm6800_flash_target.py [image.bin] [--addr 0x80000000] [--no-reset]
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SDK_ENV = os.environ.get("HPM_SDK_ENV_DIR", r"E:\sdk_env_v1.11.0")
OPENOCD = os.path.join(SDK_ENV, "tools", "openocd", "openocd.exe")
SCRIPTS = os.path.join(SDK_ENV, "tools", "openocd", "tcl")
SDK_BOARDS = os.path.join(SDK_ENV, "hpm_sdk", "boards", "openocd")
CFG = os.path.join(HERE, "openocd_hpm6800evk_dap.cfg")

# 必须烧 **ELF**：这个 SDK 构建出的 .bin 里 0x1000 处的启动头是全 0（ELF 里有），
# 烧 .bin 会让 ROM 认不出启动头、PC 停在 boot ROM 不动（实测踩过）。
DEFAULT_IMAGE = os.path.join(HERE, "hpm6800evk_rtt_flood", "build", "flash_xip", "output", "demo.elf")


def main():
    argv = [a for a in sys.argv[1:]]
    addr = "0x80000000"
    if "--addr" in argv:
        i = argv.index("--addr")
        addr = argv[i + 1]
        del argv[i:i + 2]
    image = argv[0] if argv else DEFAULT_IMAGE
    if not os.path.exists(image):
        raise SystemExit("image not found: %s" % image)
    # 必须绝对化：OpenOCD 的 flash write_image 按**它自己的 cwd** 解析相对路径，
    # 传相对路径会找不到文件（或误伤别的同名文件）。
    img = os.path.abspath(image).replace("\\", "/")

    cmds = [
        "-c", "init",
        "-c", "halt",
        # 必须用 ELF：SDK 生成的 .bin 在 0x1000 处的启动头是全 0（ELF 里有），
        # 烧 .bin 会让 ROM 认不出启动头、PC 停在 boot ROM 不动。ELF 里各段齐全。
        "-c", 'flash write_image erase "%s"' % img,
        "-c", "mdw 0x80001000 1",
    ]
    if "--no-reset" not in sys.argv:
        # 用 DM 的 ndmreset（cfg 里 reset_config none），绝不碰 SRST 引脚 ——
        # 20 针排线第 15 脚是探针自己的 RESET_N。
        cmds += ["-c", "reset halt", "-c", "reg pc", "-c", "resume"]
    # 🚨 必须显式 shutdown：不加的话 OpenOCD 烧完还当服务器跑着，
    #    subprocess 只能等到 timeout（180 s）才收场 —— 看起来像"烧录卡死"。
    cmds += ["-c", "shutdown"]

    args = [OPENOCD, "-s", SCRIPTS, "-s", SDK_BOARDS, "-f", CFG] + cmds
    print("flashing %s -> %s" % (os.path.basename(image), addr))
    t0 = time.time()
    r = subprocess.run(args, capture_output=True, text=True, timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if any(k in line for k in ("wrote ", "Verified", "diff", "Error", "error",
                                   "0x01200000", "flash", "Flash")):
            print("  " + line.strip())
    ok = ("wrote " in out) and ("diff" not in out) and ("Error" not in out)
    print("%s in %.1fs" % ("OK" if ok else "FAILED", time.time() - t0))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
