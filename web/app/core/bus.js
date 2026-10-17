/** 极简事件总线：够用就好，不引入任何依赖。 */
export class Bus {
  constructor(){ this._m = new Map(); }
  on(ev, fn){
    if (!this._m.has(ev)) this._m.set(ev, new Set());
    this._m.get(ev).add(fn);
    return () => this.off(ev, fn);
  }
  off(ev, fn){ const s = this._m.get(ev); if (s) s.delete(fn); }
  emit(ev, ...args){
    const s = this._m.get(ev);
    if (!s) return;
    for (const fn of [...s]) {
      try { fn(...args); } catch (e) { console.error(`[bus] ${ev} 处理器出错:`, e); }
    }
  }
}
