import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const MATRIX_FILE = path.join(ROOT, 'tools', 'target-firmware', 'board-matrix.json');
const MATRIX = JSON.parse(fs.readFileSync(MATRIX_FILE, 'utf8'));

export const repoRoot = ROOT;
export const boardMatrixFile = MATRIX_FILE;
export const boardMatrix = MATRIX;

export function getBoard(id) {
  const board = MATRIX.boards[id];
  if (!board) throw new Error(`未知板卡 ${id}（可选：${Object.keys(MATRIX.boards).join(' / ')}）`);
  return board;
}

export function getExample(id, example) {
  const board = getBoard(id);
  const item = board.examples?.[example];
  if (!item) throw new Error(`${id} 没有 ${example} 例程`);
  return item;
}

export function artifact(id, example, kind = 'buildArtifact') {
  return getExample(id, example)[kind];
}

export function diskPath(repoRelative) {
  return path.resolve(ROOT, repoRelative.replaceAll('/', path.sep));
}

export function urlPath(repoRelative) {
  return '/' + repoRelative.replaceAll('\\', '/').replace(/^\//, '');
}
