/**
 * 多轮随机场景编排：功能基准（RTT Viewer / 转发 / J-Scope）与调试压力交错运行。
 *
 * 每个 feature 场景执行一轮对应板卡的完整功能基准，并可随机加入一次固件交替烧录；
 * debug 场景先烧入该板卡的调试靶子，再执行 ARM 或 RISC-V 专用真机压力测试。
 * 固定 seed 让顺序与报告可复现。每板单独写汇总，每一步也保留自己的 JSON 结果。
 *
 *   node tools/selftest/hw-random-flow.mjs --board=f103cb --seed=20261005 --rounds=3
 *   node tools/selftest/hw-random-flow.mjs --board=h743 --seed=7 --rounds=4 --keep-going
 *   node tools/selftest/hw-random-flow.mjs --board=6800evk
 */
import { spawn } from 'node:child_process';
import { appendFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  debugCommands,
  featureCommand,
  getRandomFlowBoard,
  makeRandomFlowPlan,
  randomFlowResultPath,
} from './hw-random-flow-plan.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const arg = (name, fallback) => {
  const p = process.argv.find(x => x.startsWith(`--${name}=`));
  return p ? p.slice(name.length + 3) : fallback;
};
const has = name => process.argv.includes(`--${name}`);
const BOARD_ID = String(arg('board', 'f103cb'));
const BOARD = getRandomFlowBoard(BOARD_ID);
const SEED = Number(arg('seed', '20261005')) >>> 0;
const ROUNDS = Math.max(1, Math.min(8, Number(arg('rounds', '3')) || 3));
const KEEP_GOING = has('keep-going');
const RUN_ID = new Date().toISOString().replace(/[:.]/g, '-');
const LOG = resolve(ROOT, 'tmp', `hw-random-flow-${BOARD_ID}-${RUN_ID}.log`);
const RESULT = resolve(ROOT, randomFlowResultPath(BOARD_ID));
const PLAN = makeRandomFlowPlan(SEED, ROUNDS);

mkdirSync(resolve(ROOT, 'tmp'), { recursive: true });
writeFileSync(LOG, `seed=${SEED} board=${BOARD_ID} rounds=${ROUNDS}\n`);
console.log(`== ${BOARD.label} 随机场景压力 == seed=${SEED} · ${ROUNDS} 轮`);
console.log('计划：' + PLAN.map(x => `${x.step}:${x.kind}(cycles=${x.cycles},alt=${x.alt})`).join(' → '));

const report = {
  startedAt: new Date().toISOString(),
  board: BOARD_ID,
  seed: SEED,
  rounds: ROUNDS,
  log: LOG,
  plan: PLAN,
  results: [],
};

function run(script, args) {
  return new Promise(resolveRun => {
    const started = Date.now();
    console.log(`\n--- 场景命令 ${script} ${args.join(' ')} ---`);
    appendFileSync(LOG, `\n$ ${script} ${args.join(' ')}\n`);
    const child = spawn(process.execPath, [script, ...args], {
      cwd: ROOT,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    child.stdout.on('data', b => { process.stdout.write(b); appendFileSync(LOG, b); });
    child.stderr.on('data', b => { process.stderr.write(b); appendFileSync(LOG, b); });
    child.on('error', error => resolveRun({ code: 1, error: String(error), ms: Date.now() - started }));
    child.on('exit', (code, signal) => resolveRun({ code: code ?? 1, signal, ms: Date.now() - started }));
  });
}

for (const item of PLAN) {
  let result;
  if (item.kind === 'feature') {
    const command = featureCommand(BOARD_ID, item, RUN_ID);
    result = await run(command.script, command.args);
  } else {
    const [flash, test] = debugCommands(BOARD_ID, item, RUN_ID);
    const flashResult = await run(flash.script, flash.args);
    if (flashResult.code !== 0) {
      result = { code: flashResult.code, ms: flashResult.ms, error: '调试靶子烧录失败' };
    } else {
      const testResult = await run(test.script, test.args);
      result = { ...testResult, ms: flashResult.ms + testResult.ms };
    }
  }

  const row = { step: item.step, kind: item.kind, cycles: item.cycles, alt: item.alt, ...result };
  report.results.push(row);
  console.log(`--- 场景 ${item.step} ${item.kind}：${row.code === 0 ? 'PASS' : 'FAIL'} · ${(row.ms / 1000).toFixed(1)} s ---`);
  if (row.code !== 0 && !KEEP_GOING) break;
}

writeFileSync(RESULT, JSON.stringify(report, null, 2));
console.log('\n================ 随机压力汇总 ================');
for (const row of report.results) console.log(`第 ${row.step} 步 ${row.kind}：${row.code === 0 ? 'PASS' : 'FAIL'} · ${(row.ms / 1000).toFixed(1)} s`);
console.log(`日志：${LOG}`);
console.log(`结果：${RESULT}`);
process.exitCode = report.results.some(r => r.code !== 0) || report.results.length !== PLAN.length ? 1 : 0;
