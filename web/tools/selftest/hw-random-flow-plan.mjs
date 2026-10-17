const BOARD_CONFIG = Object.freeze({
  f103cb: Object.freeze({
    label: 'STM32F103CB',
    campaignScript: 'tools/selftest/hw-campaign.mjs',
    campaignBoard: 'f103cb',
    debugScript: 'tools/selftest/dbg-hw-stress.mjs',
    debugBoard: 'f103cb',
  }),
  h743: Object.freeze({
    label: 'STM32H743',
    campaignScript: 'tools/selftest/hw-campaign.mjs',
    campaignBoard: 'h743',
    debugScript: 'tools/selftest/dbg-hw-stress.mjs',
    debugBoard: 'h743',
  }),
  '6800evk': Object.freeze({
    label: 'HPM6800EVK（RISC-V/JTAG）',
    campaignScript: 'tools/selftest/hw-campaign-hpm.mjs',
    campaignBoard: null,
    debugScript: 'tools/selftest/dbg-hw-riscv.mjs',
    debugBoard: null,
  }),
});

export const randomFlowBoardIds = Object.freeze(Object.keys(BOARD_CONFIG));

export function getRandomFlowBoard(id) {
  const board = BOARD_CONFIG[id];
  if (!board) throw new Error(`随机压力流程只支持 ${randomFlowBoardIds.join(' / ')}（给的是 ${id}）`);
  return board;
}

function random(state) {
  let x = state.value >>> 0;
  x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
  state.value = x >>> 0;
  return state.value;
}

function shuffle(list, state) {
  const out = [...list];
  for (let i = out.length - 1; i > 0; i--) {
    const j = random(state) % (i + 1);
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

export function makeRandomFlowPlan(seed = 20261005, rounds = 3) {
  const count = Math.max(1, Math.min(8, Number(rounds) || 3));
  const state = { value: (Number(seed) >>> 0) || 1 };
  const base = Array.from({ length: count }, (_, i) => ({
    kind: i === 1 ? 'debug' : 'feature',
    cycles: 1,
    alt: 0,
  }));
  for (const item of base) {
    if (item.kind === 'feature' && random(state) % 2) item.alt = 1;
  }
  return shuffle(base, state).map((item, i) => ({ ...item, step: i + 1 }));
}

export function featureCommand(boardId, item, runId) {
  const board = getRandomFlowBoard(boardId);
  const prefix = `tmp/hw-random-flow-${boardId}-${runId}-step-${item.step}`;
  return {
    script: board.campaignScript,
    args: [
      ...(board.campaignBoard ? [`--board=${board.campaignBoard}`] : []),
      '--local',
      `--cycles=${item.cycles}`,
      `--alt=${item.alt}`,
      '--keep-going',
      `--out=${prefix}-campaign.json`,
    ],
  };
}

export function debugCommands(boardId, item, runId) {
  const board = getRandomFlowBoard(boardId);
  const prefix = `tmp/hw-random-flow-${boardId}-${runId}-step-${item.step}`;
  return [
    {
      script: 'tools/selftest/flash-elf.mjs',
      args: [`--board=${boardId}`],
    },
    {
      script: board.debugScript,
      args: [
        ...(board.debugBoard ? [`--board=${board.debugBoard}`] : []),
        `--oracle=${prefix}-no-oracle.json`,
        `--out=${prefix}-debug.json`,
      ],
    },
  ];
}

export function randomFlowResultPath(boardId) {
  getRandomFlowBoard(boardId);
  return boardId === 'f103cb'
    ? 'tmp/hw-random-flow-result.json'
    : `tmp/hw-random-flow-${boardId}-result.json`;
}
