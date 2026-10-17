/*
    JTAG DMI 扫描汇编（探针侧 RISC-V 引擎专用）—— akaLinkPro / HPM5301EVKLite

    引脚（JTAG 模式，PORT_JTAG_SETUP 已把引脚挂到 FGPIO，移位量由 -D 传入）：
        JTCK = PA27 / PA06      JTMS = PA28 / PA07
        JTDI = PA08 / PA05      JTDO = PA09 / PA04（输入）

    与 JTAG_DP_GPIO_ASM_45M.S 的关系：
      那个文件是 CMSIS-DAP 的通用 JTAG_Sequence（主机驱动用；每次调用只扫
      1~64 bit、TMS 固定，走通用分支并要把 TDI 打包成字节）。主机驱动下一次
      DMI 访问要调用它 6 次，C 侧开销约 1.25 us，占一次访问 4.1 us 的 30%。

      这里把**一次完整的 DMI 访问**收进单个函数：
        Run-Test/Idle -> Select-DR -> Capture-DR -> Shift-DR(41 bit)
        -> Exit1-DR -> Update-DR -> Run-Test/Idle
    41 bit 请求直接在寄存器里逐位右移，TDO 用 1 拍流水采样，C 侧只剩一次
    函数调用。

    【2026-09-28 提速：TDI 预置 + 循环展开】
      一次 DMI 访问 = idle(8) + 导航(5) + 移位(41) = 54 TCK，所以吞吐直接由
      "每 bit 多少拍" 决定。旧结构每 bit 18 拍（低相位 8 条指令专等 TDI 建立 +
      高相位 8 拍 nop 等 TDO），实测 1189.5 KB/s、有效 TCK 16.4 MHz —— 而目标
      （HPM6800EVK）的 JTAG TCK 规格上限是 25 MHz，还差 1.5 倍没吃满。

      两处改动（都靠 hpm6800_selfcheck.py 写图案读回卡死）：
        1) TDI 预置：把"给出下一位 TDI"移到**上一位的高相位**，此时 TCK 已是高，
           把 DO_VAL 写成 "TCK|TDI" 只是改电平、不产生边沿；低相位于是退化成
           一条 `sw DO_CLR`，只受目标最小 TCK 低电平宽度约束（实测要 4 拍），
           不再受 TDI 建立时间约束。累加也一并挪进高相位，正好填掉原来的死等。
        2) 整个 32 位位移循环展开，每条位省下 addi+bnez 两拍。

      实测：读 1189.5 -> **1504.5 KB/s**、写 1194.8 -> **1511.8 KB/s**（+26.5%），
      有效 TCK ~20.7 MHz；RTT 交付 1164.7 -> 1385 KB/s，28.5 MB 字节级零丢包。
      四个时序旋钮的实测边界见 CMakeLists.txt 的说明与
      script_test/README.md 的回归基线表 —— 都已经在最小值上，别再往下减。
*/

.equ FGPIO_BASE,                (0x000C0000)
.equ FGPIO_DI_VAL_OFFSET,       (0x000)
.equ FGPIO_DO_VAL_OFFSET,       (0x100)
.equ FGPIO_DO_SET_OFFSET,       (0x104)
.equ FGPIO_DO_CLR_OFFSET,       (0x108)

#ifndef JTAG_PIN_JTCK_SHIFT
#define JTAG_PIN_JTCK_SHIFT 27
#endif
#ifndef JTAG_PIN_JTMS_SHIFT
#define JTAG_PIN_JTMS_SHIFT 28
#endif
#ifndef JTAG_PIN_JTDI_SHIFT
#define JTAG_PIN_JTDI_SHIFT 8
#endif
#ifndef JTAG_PIN_JTDO_SHIFT
#define JTAG_PIN_JTDO_SHIFT 9
#endif
#ifndef DMI_CAP_HIGH_NOP
#define DMI_CAP_HIGH_NOP 1
#endif
/* 移位相位 TCK **低电平**保持的 nop 数。
 *
 * 新方案里 TDI 已在上一拍的高相位摆好，所以这里**不需要**再等 TDI 建立时间
 * （那是旧方案低相位 8 条指令的来源），只需要满足目标的最小 TCK 低电平宽度。
 * 因此可以比 DMI_NAV_LOW_NOP 小得多 —— 具体能小到多少要用自检实测
 * （script_test/hpm6800_selfcheck.py），别靠推理。 */
#ifndef DMI_TCK_LOW_NOP
#define DMI_TCK_LOW_NOP 4
#endif
/* bit40（Exit1-DR，TMS=1）那一位的采样延迟。它是**一次性**的收尾位，跑在
 * DMI_SHIFT_BIT 之外，自己的 DO_VAL/DO_SET 序列里没有任何"顺带干活"的指令，
 * 所以采样延迟得靠 nop 补齐 —— 不能跟着 DMI_CAP_HIGH_NOP 一起降到 1。 */
#ifndef DMI_BIT40_HIGH_NOP
#define DMI_BIT40_HIGH_NOP 8
#endif

.equ JTCK_OFFSET,               (1 << JTAG_PIN_JTCK_SHIFT)
.equ JTMS_OFFSET,               (1 << JTAG_PIN_JTMS_SHIFT)
.equ JTDI_OFFSET,               (1 << JTAG_PIN_JTDI_SHIFT)
.equ JTDI_SHIFT,                (JTAG_PIN_JTDI_SHIFT)
.equ JTDO_SHIFT,                (JTAG_PIN_JTDO_SHIFT)

/* 非移位状态（导航 / idle）TCK 高电平保持的 nop 数 */
#ifndef DMI_NAV_HIGH_NOP
#define DMI_NAV_HIGH_NOP 4
#endif
/* 非移位状态 TCK 低电平保持的 nop 数。**不能省**：TMS/TDI 是在拉低的同一
 * 次 DO_VAL 写里给出的，如果紧跟着就拉高，TMS 只在上升沿前 1 个周期才变化，
 * 目标端采样到的还是旧 TMS（TAP 会走错分支，实测表现为响应 op/addr 对、
 * 但读数据恒为 0）。实测 6 稳、4 直接 FAIL（读回全 0）。 */
#ifndef DMI_NAV_LOW_NOP
#define DMI_NAV_LOW_NOP 6
#endif

/* 一个非移位时钟：给出 TMS/TDI -> 低电平保持 -> 拉高 -> 高电平保持 */
.macro DMI_NAV_CLOCK word
    sw      \word, FGPIO_DO_VAL_OFFSET(t0)
.rept DMI_NAV_LOW_NOP
    nop
.endr
    sw      t1, FGPIO_DO_SET_OFFSET(t0)
.rept DMI_NAV_HIGH_NOP
    nop
.endr
.endm

/* 移位位：**TDI 预置**方案（TMS 恒为 0，所以不用叠加 TMS）。
 *
 * 旧方案每一位都在低相位里完成"取 TDI 位 + 拉低 TCK + 写 DO_VAL"，于是低相位
 * 有 8 条指令 ≈ 8 拍，纯粹是在等目标侧的 TDI 建立时间；TCK 周期 = 8 + 1 + 8 + 1
 * = 18 拍，实测 16.3 MHz（HPM6800EVK 规格上限 25 MHz，还有 1.5 倍没吃满）。
 *
 * 新方案把 TDI 的建立**移到上一位的高相位**：此时 TCK 已经是高，把 DO_VAL 写成
 * "TCK | TDI" 只是改 TDI 电平，不产生任何边沿。于是低相位退化成
 *     sw t1, DO_CLR        # 只把 TCK 拉低
 * 一个 store，只受 TCK 低电平宽度约束，不再受 TDI 建立时间约束。
 *
 * 顺带用上 45M 汇编里两个更省的写法：
 *   andi t4, in, JTDI_OFFSET  —— JTDI_OFFSET 就是 1<<shift，一条 andi 同时完成
 *                                "取位"和"移位"（旧写法要 andi + slli 两条）
 *   or out,out,b ; rori out,out,1 —— 两拍完成累加（旧写法 srli+slli+or 三拍），
 *                                32 拍后 b_k 正好落在 bit[k]，也不再需要收尾对奇
 *
 * 进入本迭代的前提：TCK 为高、且**当前这一位的 TDI 已经摆好**（由 prologue 或
 * 上一拍结尾的 DMI_TDI_PRESET 完成）。
 *
 * \out : TDO 累加器（or 进 bit0 后循环右移 1，32 拍后 b_k 落在 bit[k]）
 * \in  : TDI 位来源（每拍右移，bit0 是下一位） */
.macro DMI_SHIFT_BIT out, in
    sw      t1, FGPIO_DO_CLR_OFFSET(t0)     # --- 低相位：只拉低 TCK（TDI 已稳定）
.rept DMI_TCK_LOW_NOP
    nop
.endr
    sw      t1, FGPIO_DO_SET_OFFSET(t0)     # --- TCK 拉高（本位的上升沿）
    bexti   a6, a5, JTDO_SHIFT              # 整理上一拍采样（在高相位里干活，不占低相位）
    or      \out, \out, a6
    rori    \out, \out, 1
    srli    \in, \in, 1                     # 下一位
    /* 注意**不能**写成 `andi t4, \in, JTDI_OFFSET`：那条写法只在 TDI 流已经预先
     * 左移过 JTDI_SHIFT 的代码里成立（45M 汇编的 a4 就是那种约定）。这里的 \in
     * 是原始 DMI 请求寄存器、bit0 才是当前位，套过去会去取 bit[JTDI_SHIFT]，
     * 结果是请求被整串写成 0（op=NOP）—— 症状极隐蔽：不报错、基准还"跑得通"，
     * 只是读回来全是 0，只有自检能抓到。 */
    andi    t4, \in, 1
    slli    t4, t4, JTDI_SHIFT
    or      t4, t4, t1
    sw      t4, FGPIO_DO_VAL_OFFSET(t0)
.rept DMI_CAP_HIGH_NOP
    nop
.endr
    lw      a5, FGPIO_DI_VAL_OFFSET(t0)     # 采样本位 TDO
.endm

/* 摆当前位的 TDI（TCK 保持高）。用于进入移位循环前把 bit0 预置好 —— 新方案里
 * 低相位不再负责 TDI，所以循环入口必须先做这一下。 */
.macro DMI_TDI_PRESET in
    andi    t4, \in, 1
    slli    t4, t4, JTDI_SHIFT
    or      t4, t4, t1
    sw      t4, FGPIO_DO_VAL_OFFSET(t0)
.endm

/* 冲刷 1 拍流水里剩下的最后一位采样（随后 a5 会被重新清零）。 */
.macro DMI_FLUSH_BIT out
    bexti   a6, a5, JTDO_SHIFT
    or      \out, \out, a6
    rori    \out, \out, 1
.endm

.section .fast, "ax"
.align  2
.global JTAG_DMI_Scan
.type   JTAG_DMI_Scan, @function

/* uint32_t JTAG_DMI_Scan(uint32_t idle, uint32_t dr_lo, uint32_t dr_mid,
 *                        uint32_t dr_hi, uint32_t *tdo_hi)
 *   a0 = Run-Test/Idle 周期数（0..255）
 *   a1 = DMI 请求 bit[31:0]
 *   a2 = DMI 请求 bit[39:32]（8 位）
 *   a3 = DMI 请求 bit40（bit0 有效）
 *   a4 = 存放 TDO bit[40:32] 的地址（低 9 位有效）
 * 返回 a0 = TDO bit[31:0]
 */
JTAG_DMI_Scan:
    addi    sp, sp, -16
    sw      s0, 0(sp)

    li      t0, FGPIO_BASE
    li      t1, JTCK_OFFSET
    li      t2, JTMS_OFFSET
    li      t5, JTDI_OFFSET
    or      t6, t2, t5                  # TMS=1 + TDI=1（导航用）
    mv      s0, a4                      # &tdo_hi

    # ---------------- Run-Test/Idle（TMS=0, TDI=1） ----------------
    beqz    a0, .Ldmi_nav
.Ldmi_idle:
    DMI_NAV_CLOCK t5
    addi    a0, a0, -1
    bnez    a0, .Ldmi_idle

    # ---------------- RTI -> Select-DR -> Capture-DR -> Shift-DR ----------------
.Ldmi_nav:
    DMI_NAV_CLOCK t6                        # Select-DR-Scan（TMS=1）
    DMI_NAV_CLOCK t5                        # Capture-DR（TMS=0）
    DMI_NAV_CLOCK t5                        # Shift-DR（TMS=0）

    # ---------------- 移位 32 bit（TMS=0，捕获 TDO） ----------------
    /* 整个 32 位循环展开：每条位省下 addi+bnez 两拍（约占每拍 16 条指令的 12%）。
     * 展开后 .fast 段多约 2 KB，但它在 ILM 里（单周期取指），不担心取指带宽。 */
    mv      a0, x0                          # TDO bit[31:0]
    mv      a5, x0                          # 上一拍 DI 整字（1 拍流水）
    DMI_TDI_PRESET a1                       # 新方案：循环入口前先把 bit0 的 TDI 摆好
.rept 32
    DMI_SHIFT_BIT a0, a1
.endr
    DMI_FLUSH_BIT a0

    # ---------------- 移位 8 bit（TMS=0，TDO bit[39:32]） ----------------
    mv      a4, x0
    mv      a5, x0
    DMI_TDI_PRESET a2
.rept 8
    DMI_SHIFT_BIT a4, a2
.endr
    DMI_FLUSH_BIT a4
    /* 累加器"插到 bit0 再循环右移"，要正好 32 拍才自然对齐到 bit[31:0]；
     * 8 拍会停在 bit[31:24]，右移回来。 */
    srli    a4, a4, 24

    # ---------------- bit40（TMS=1，Exit1-DR） ----------------
    mv      a5, x0
    andi    t4, a3, 1
    slli    t4, t4, JTDI_SHIFT
    /* 只能用 TMS 预设 t2：t6 里带了导航用的 TDI=1，叠加上去会把请求的
     * bit40 强制成 1（等于把地址或上 0x40，落到非法寄存器上）。 */
    or      t4, t4, t2
    sw      t4, FGPIO_DO_VAL_OFFSET(t0)
.rept DMI_NAV_LOW_NOP
    nop
.endr
    sw      t1, FGPIO_DO_SET_OFFSET(t0)
.rept DMI_BIT40_HIGH_NOP
    nop
.endr
    lw      a5, FGPIO_DI_VAL_OFFSET(t0)
    bexti   a6, a5, JTDO_SHIFT
    slli    a6, a6, 8
    or      a4, a4, a6                      # TDO bit40 -> bit8

    # ---------------- Update-DR -> Run-Test/Idle ----------------
    DMI_NAV_CLOCK t6                        # Update-DR（TMS=1）
    DMI_NAV_CLOCK t5                        # Run-Test/Idle（TMS=0）

    sw      a4, 0(s0)
    lw      s0, 0(sp)
    addi    sp, sp, 16
    ret
