export const HPM_WASM_BOARD = 'hpm6800evk';
export const isHpmOpenOcdEntry = name => name === 'hpm6800_all_in_one.cfg' || name === 'boards/hpm6800evk.cfg';

const scripts = {
  'hpm6800_all_in_one.cfg':'edde81030558e1904ea9301f56c86c0d6b188891c9b3776c81c28d03f8e58ca1',
  'probes/cmsis_dap.cfg':'5a5ff987ec864da0002b7771329df1cc5d91469981eb346dd21101bdba5d788c',
  'soc/hpm6880.cfg':'675dc3c27f5d4ca4595b6084a1311e9141bd31921218b61dc5035a284a3085d2',
  'soc/hpm_common.cfg':'1591620f7255149b4bc036bd362842ea45871efcf1b4b1b2705e90f0e653187b',
  'soc/hpm_reset.cfg':'6ac20afdfa83259aaf30628d0c8546c2628fca018ed300368400563385da7d06',
  'soc/hpm_common_csr.cfg':'52f01daeca5d4586288823c0dcff4665f6bae291338f730466a656adb716c74f',
  'boards/hpm6800evk.cfg':'c79fd8f0d5ebfbc393483fdfe2bf600ca4629dea4a58e5079f683f3a20cabc76',
};

// Run only the audited SDK scripts on hardware in this profile.
// They declare a Flash bank but its reset and GDB hooks must never run here.
export async function hpmOpenOcdFiles(directory, entry){
  if (!directory || !isHpmOpenOcdEntry(entry))
    throw new Error('请选择 HPM SDK 1.11.0 的 boards/openocd 目录和 HPM6800EVK 入口 CFG');
  return await Promise.all(Object.entries(scripts).map(async ([path, expected]) => {
    const text = directory.get(path)?.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');
    if (text == null) throw new Error('CFG 目录缺少 ' + path);
    const hash = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)));
    const actual = [...hash].map(byte => byte.toString(16).padStart(2, '0')).join('');
    if (actual !== expected) throw new Error('请使用已核对的 HPM SDK 1.11.0 原版 CFG：' + path);
    return [path, text];
  }));
}
