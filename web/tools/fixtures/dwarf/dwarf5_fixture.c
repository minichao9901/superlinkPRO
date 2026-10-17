/* DWARF 5 夹具源码：只为了产出"带 DWARF5 调试信息的真 ELF"。
   变量布局模仿 HPM 靶子固件的契约块（结构体 + 各种标量 + 一个数组用来测剔除）。*/
struct vpack { unsigned int tick; unsigned int u_hi; float f_sin; float f_tri; int i_sq1k; unsigned int ramp; };
volatile struct vpack g_v;
volatile unsigned int g_updates;
volatile float g_tri_buf[16];
volatile char g_flag;
int main(void){ g_v.tick++; g_updates++; return (int)g_v.tick; }
