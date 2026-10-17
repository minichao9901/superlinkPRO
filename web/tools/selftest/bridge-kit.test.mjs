/**
 * 「桥包」生成器自测（纯离线，不需要探针 / OpenOCD / Node 之外的任何东西）：
 *   node tools/selftest/bridge-kit.test.mjs      （等价：并入 make test-gen）
 *
 * 咬三件事：
 *   ① **不漂移**：包里的 `rtt-bridge.mjs` 必须与仓库 `bridge/rtt-bridge.mjs` 逐字节一致
 *      （`app/gen/bridge-src.js` 是嵌进去的副本，改完桥忘了重嵌 = 这里红）；
 *   ② **能跑**：bat/sh/ps1 里有那几件必需品（预检、便携 Node、参数透传、Ctrl+C 提示、
 *      两个下载源 + sha256 校验、CRLF/LF 各就各位）；
 *   ③ **可移植**：配置里的路径留空 = 自动探测；换机器只改 openocd / scripts / jlink 三处。
 */
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');
const load = p => import('file://' + join(root, p).replace(/\\/g, '/'));

const { bridgeKitFiles, kitConfig, KIT_DEFAULTS, KIT_DIR } = await load('app/gen/bridge-kit.js');
const { BRIDGE_SRC, BRIDGE_SHA256 } = await load('app/gen/bridge-src.js');

let pass = 0, fail = 0;
const ok = (c, name, extra = '') => { if (c){ pass++; console.log(`  PASS  ${name}`); } else { fail++; console.log(`  FAIL  ${name}${extra ? '  → ' + extra : ''}`); } };

const WHEN = new Date('2026-09-30T12:00:00Z');
const files = bridgeKitFiles({}, WHEN);
const byName = n => files.find(f => f.name === `${KIT_DIR}/${n}`);
const txt = n => byName(n).text;
const bytes = n => new TextDecoder().decode(byName(n).data);

// ==================================================================== 1
console.log('== 1. 文件清单与换行 ==');
{
  const names = files.map(f => f.name.replace(KIT_DIR + '/', ''));
  ok(names.length === 7 && names.includes('rtt-bridge.mjs') && names.includes('start-bridge.bat') &&
     names.includes('check-tools.bat') && names.includes('get-node.ps1') && names.includes('start-bridge.sh') &&
     names.includes('bridge.config.json') && names.includes('README-bridge.txt'),
     `7 个文件齐了：${names.join(' · ')}`);
  ok(bytes('start-bridge.bat').includes('\r\n') && !/[^\r]\n/.test(bytes('start-bridge.bat')),
     '.bat 是纯 CRLF（cmd 对 LF 的 label/goto 不友好）');
  ok(!bytes('start-bridge.sh').includes('\r'), '.sh 是纯 LF（CRLF 会让 bash 报 bad interpreter）');
  ok(!bytes('rtt-bridge.mjs').includes('\r'), 'rtt-bridge.mjs 是纯 LF（跨平台一致）');
  ok(bridgeKitFiles({ on: false }, WHEN).length === 0, '关掉开关（on:false）= 一个文件都不生成');
}

// ==================================================================== 2
console.log('== 2. 不漂移：包里的桥 == 仓库里的桥（逐字节） ==');
{
  const repo = readFileSync(join(root, 'bridge', 'rtt-bridge.mjs'), 'utf8').replace(/\r\n/g, '\n');
  const sha = createHash('sha256').update(repo, 'utf8').digest('hex');
  ok(BRIDGE_SRC === repo, `嵌入副本与仓库那份逐字节一致（${repo.length} 字节）`,
     `嵌入 ${BRIDGE_SRC.length} / 仓库 ${repo.length}`);
  ok(BRIDGE_SHA256 === sha, `sha256 对得上（${sha.slice(0, 16)}…）`);
  ok(bytes('rtt-bridge.mjs') === repo, '包里写出的 rtt-bridge.mjs 也是同一份字节');
  ok(txt('README-bridge.txt').includes(BRIDGE_SHA256), 'README 里印了 sha256（人工核对用）');
  ok(/embed-bridge\.mjs/.test(txt('README-bridge.txt')), 'README 说明了"改了桥要重跑 embed-bridge.mjs"');
}

// ==================================================================== 3
console.log('== 3. start-bridge.bat：找 Node → 预检 → 起桥 ==');
{
  const bat = bytes('start-bridge.bat');
  ok(/cd \/d "%~dp0"/.test(bat), '先 cd 到自己所在目录（双击也能找到 rtt-bridge.mjs）');
  ok(/for %%I in \(node\.exe\) do if not "%%~\$PATH:I"=="" /.test(bat) || /%%~\$PATH:I/.test(bat),
     '先从 PATH 找 node.exe');
  ok(/"%NODE_DIR%\\node\.exe"/.test(bat), '便携版优先：先看 .\\node\\node.exe');
  ok(/get-node\.ps1" -Version %NODE_VER% -Mirror %MIRROR% -Dest "%NODE_DIR%"/.test(bat),
     '没有 Node + 允许自动装 → 调 get-node.ps1（版本/镜像/目标目录都传进去）');
  ok(/set "AUTO_NODE=1"/.test(bat) && /set "MIRROR=1"/.test(bat) && /set "NODE_VER=22\.14\.0"/.test(bat),
     '三个开关写成了可改的字面量（AUTO_NODE / MIRROR / NODE_VER）');
  ok(/"%NODE_EXE%" "%BRIDGE%" --doctor --port %PORT% --target %TARGET%/.test(bat),
     '起桥**之前**先跑 --doctor 预检');
  ok(/if errorlevel 1/.test(bat) && /预检里有带 X 的项/.test(bat), '预检有 ❌ 会明确提示（但仍继续起桥）');
  ok(/if not "%~1"=="" set "ARGS=%\*"/.test(bat), '命令行参数原样透传（给了参数就用给的）');
  ok(/Ctrl\+C/.test(bat) && /子进程一起收掉/.test(bat), '横幅里讲清了 Ctrl+C 会收掉子进程（不留孤儿占探针）');
  ok(/:fail/.test(bat) && /pause >nul/.test(bat), '失败会停住窗口（双击场景看不到一闪而过的报错）');
}

// ==================================================================== 4
console.log('== 4. check-tools.bat / get-node.ps1 ==');
{
  const chk = bytes('check-tools.bat');
  ok(/--doctor/.test(chk) && /--port 17321 --target stm32f103/.test(chk), 'check-tools.bat 就是跑 --doctor（带默认端口/目标）');
  ok(/没找到 Node\.js/.test(chk), '没 Node 时给出可操作的提示');

  const ps = bytes('get-node.ps1');
  ok(/registry\.npmmirror\.com/.test(ps) && /nodejs\.org\/dist/.test(ps), '两个下载源都写上了（国内镜像 + 官方）');
  ok(/\$Mirror -eq 1/.test(ps) && /if \(\$Mirror -eq 1\)/.test(ps), '镜像优先级由 -Mirror 控制（生成时写死默认值）');
  ok(/SHASUMS256\.txt/.test(ps) && /Get-FileHash/.test(ps), '下载后按 SHASUMS256.txt 校验 sha256');
  ok(/sha256 不一致/.test(ps) && /continue/.test(ps), '校验不过就换下一个源（不是硬失败）');
  ok(/Expand-Archive/.test(ps) && /Copy-Item/.test(ps) && /node\.exe/.test(ps), '解压并铺到 -Dest（找到 node.exe 才认）');
  ok(/不动系统 PATH|不写注册表/.test(ps), '注释里讲明是绿色安装（不动系统）');
}

// ==================================================================== 5
console.log('== 5. bridge.config.json：页面参数 → 桥的配置 ==');
{
  const def = JSON.parse(txt('bridge.config.json'));
  ok(def.openocd === '' && def.scripts === '', '工具路径默认留空 = 桥自己去常见位置探测（换机器不用改）');
  ok(def.jlink.device === 'STM32F103C8' && def.jlink.speed === 50000, `J-Link 默认值是真机标定过的（${def.jlink.device} @ ${def.jlink.speed} kHz）`);
  ok(Object.keys(def.targets).join(',') === 'stm32f103,stm32h7b0', `内置两个常用目标：${Object.keys(def.targets).join(' / ')}`);
  ok(def.targets.stm32f103.speed === 4000 && def.targets.stm32f103.cfgs.includes('target/stm32f1x.cfg'),
     'f103 预设：cmsis-dap + stm32f1x.cfg @ 4000 kHz');
  ok(def._生成信息?.桥本体_sha256 === BRIDGE_SHA256, '生成信息里带桥的 sha256（可核对是不是最新那份）');

  const custom = bridgeKitFiles({ target: 'custom', customCfgs: 'interface/cmsis-dap.cfg, target/stm32h7x.cfg', speed: '8000', openocd: 'D:/ocd/bin/openocd.exe', scripts: 'D:/ocd/share/openocd/scripts', jlinkDevice: 'STM32H7B0VB', jlinkSpeed: 12000 }, WHEN);
  const c = JSON.parse(new TextDecoder().decode(custom.find(f => /config\.json$/.test(f.name)).data));
  ok(!!c.targets.custom && c.targets.custom.cfgs.length === 2 && c.targets.custom.speed === 8000,
     `自定义目标写进 targets.custom（${c.targets.custom.cfgs.join(' + ')} @ ${c.targets.custom.speed} kHz）`);
  ok(c.targets.custom.pre[0] === 'cmsis-dap backend usb_bulk', '第一个 cfg 是 cmsis-dap 时自动补 backend usb_bulk（用户不用记）');
  ok(c.openocd === 'D:/ocd/bin/openocd.exe' && c.scripts === 'D:/ocd/share/openocd/scripts', '页面上填的工具路径原样写进配置');
  ok(c.jlink.device === 'STM32H7B0VB' && c.jlink.speed === 12000, 'J-Link 的 device/speed 也跟着页面走');
  ok(/--port 17321 --target custom/.test(new TextDecoder().decode(custom.find(f => /check-tools/.test(f.name)).data)),
     'check-tools.bat 里的目标跟着选了 custom');
}

// ==================================================================== 6
console.log('== 6. README 与确定性 ==');
{
  const rd = txt('README-bridge.txt');
  ok(/换机器 \/ 路径不对/.test(rd) && /"openocd"/.test(rd) && /"scripts"/.test(rd) && /"jlink"/.test(rd),
     'README 明说"换机器改这三处"（openocd / scripts / jlink）');
  ok(/自动探测顺序/.test(rd) && /SEGGER\\JLink_\*/.test(rd), 'README 写了自动探测顺序（找不到时照着填）');
  ok(/Ctrl\+C/.test(rd) && /孤儿/.test(rd), 'README 讲了退出要 Ctrl+C、别留孤儿');
  ok(/--token/.test(rd) && /--allow-origin/.test(rd), 'README 提了口令与来源白名单（自建站要用）');
  ok(/--doctor/.test(rd), 'README 教了怎么只跑预检');

  // 同一个参数 + 同一个时间戳 → 逐字节相同（便于对账、也便于用户核对"我下的是哪一版"）
  const again = bridgeKitFiles({}, WHEN);
  const same = again.length === files.length && again.every((f, i) =>
    f.name === files[i].name && Buffer.compare(Buffer.from(f.data), Buffer.from(files[i].data)) === 0);
  ok(same, '同参数同时间戳 → 两次生成的字节完全一致（确定性）');
  const later = bridgeKitFiles({}, new Date('2026-10-01T00:00:00Z'));
  ok(later[1].text !== files[1].text, '换了时间戳，配置里的生成时间会变（能看出是哪次生成的）');
}

// ==================================================================== 7
console.log('== 7. 端口/口令/镜像这些参数真的落到文件里 ==');
{
  const f2 = bridgeKitFiles({ port: 17999, mirror: 0, autoNode: false, nodeVersion: '20.18.0' }, WHEN);
  const get = n => new TextDecoder().decode(f2.find(f => f.name.endsWith(n)).data);
  ok(/set "PORT=17999"/.test(get('start-bridge.bat')) && /--port 17999/.test(get('check-tools.bat')),
     '端口改到 17999：bat 与 check-tools 都跟着');
  ok(/set "MIRROR=0"/.test(get('start-bridge.bat')) && /\$Mirror = 0/.test(get('get-node.ps1')),
     '镜像优先关掉时，ps1 的默认值也跟着变（官方优先）');
  ok(/set "AUTO_NODE=0"/.test(get('start-bridge.bat')), '关掉自动下载时 bat 里写明（会改用提示 + 退出）');
  ok(/set "NODE_VER=20\.18\.0"/.test(get('start-bridge.bat')) && /\$Version = '20\.18\.0'/.test(get('get-node.ps1')),
     'Node 版本跟着改（bat 的字面量与 ps1 的 -Version 默认值一致）');
  ok(/127\.0\.0\.1:17999/.test(get('README-bridge.txt')), 'README 里的网页地址用的是你选的端口');
}

console.log(`\n${fail ? '❌' : '✅'} bridge-kit.test: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
