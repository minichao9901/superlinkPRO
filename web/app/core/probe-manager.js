/** Control-plane arbitration only. Bulk readers and sample processing never enter this queue. */
export class ProbeCancelled extends Error {
  constructor(){ super('探针操作已取消'); this.name = 'ProbeCancelled'; }
}

export class ProbeManager {
  constructor({ beforeAcquire = null, locks = globalThis.navigator?.locks } = {}){
    this.clients = new Map();
    this.leases = new Map();
    this.failures = new Map();
    this.pending = new Set();
    this.current = null;
    this._chain = Promise.resolve();
    this.beforeAcquire = beforeAcquire;
    this.locks = locks;
  }

  register(owner, { resources, release, label = owner, active = () => false, protected: guarded = () => false }){
    if (this.clients.has(owner)) throw new Error(`重复的探针使用者：${owner}`);
    this.clients.set(owner, { resources: new Set(resources), release, label, active, guarded });
  }

  _enqueue(fn){
    const p = this._chain.then(fn);
    this._chain = p.catch(() => {});
    return p;
  }

  _conflicts(a, b){ return [...a].some(r => b.has(r)); }
  _resources(id, client = this.clients.get(id)){ return this.leases.get(id)?.resources || client.resources; }

  /** Release optional resources after a confirmed STOP; adding resources requires run(). */
  narrow(owner, extra = []){
    const lease = this.leases.get(owner);
    if (!lease) return;
    const resources = new Set([...this.clients.get(owner).resources, ...extra]);
    if ([...resources].some(r => !this._resources(owner).has(r))) throw new Error('新增资源需要重新仲裁');
    this.leases.set(owner, { ...lease, resources });
  }

  _checkProtected(owner, resources, recovery = false){
    for (const [id, c] of this.clients){
      if (this.failures.has(id) && this._conflicts(resources, this._resources(id, c)) && !(id === owner && recovery))
        throw new Error(`探针释放尚未确认（${id}）：${this.failures.get(id).message}；请重连原功能并停止`);
      if (id !== owner && this._conflicts(resources, this._resources(id, c)) && c.guarded())
        throw new Error(`${c.label}正在使用探针，请等待完成`);
    }
  }

  /** Serialize acquisition AND setup: a later feature cannot close a half-open session. */
  async run(owner, fn, { reason = '另一个功能要使用探针', policy = 'handoff', recovery = false,
    resources: extra = [], rejectResources = [], conflictMessage = null } = {}){
    const client = this.clients.get(owner);
    if (!client) throw new Error(`未登记的探针使用者：${owner}`);
    const resources = new Set([...this._resources(owner, client), ...extra]);
    this._checkProtected(owner, resources, recovery);
    const ticket = { owner, controller: new AbortController(), phase: 'queued' };
    this.pending.add(ticket);
    const alive = () => { if (ticket.controller.signal.aborted) throw new ProbeCancelled(); };
    let onAbort;
    const cancelled = new Promise((_, reject) => {
      onAbort = () => { if (ticket.phase !== 'setup') reject(new ProbeCancelled()); };
      ticket.controller.signal.addEventListener('abort', onAbort, { once: true });
    });
    try {
      const task = this._enqueue(async () => {
        alive();
        ticket.phase = 'lock';
        const execute = async () => {
          alive(); ticket.phase = 'setup'; this.current = ticket;
          try {
            this._checkProtected(owner, resources, recovery);
            const conflicts = [...this.clients].filter(([id, c]) => id !== owner &&
              (this.leases.has(id) || c.active()) && this._conflicts(resources, this._resources(id, c)));
            if (conflicts.some(([id, c]) => rejectResources.some(r => this._resources(id, c).has(r))))
              throw new Error(conflictMessage || `共享资源正在被 ${conflicts.map(([, c]) => c.label).join('、')} 使用，请先停止对应功能`);
            if (policy === 'reject' && conflicts.length) throw new Error('共享资源正在使用中，请先停止对应功能');
            for (const [id, c] of conflicts){
              try { await c.release(reason); }
              catch (e){ this.fail(id, e); throw e; }
              this.leases.delete(id);
              alive();
            }
            // One page owns the physical device. Compatible local clients share that ownership.
            if (!this.leases.size && this.beforeAcquire) await this.beforeAcquire(reason);
            alive();
            const lease = Object.freeze({ owner, resources, signal: ticket.controller.signal, assert: alive });
            this.leases.set(owner, lease);
            return await fn(lease);
          } finally {
            if (!client.active() && !this.failures.has(owner)) this.leases.delete(owner);
            this.current = null;
          }
        };
        // Same-origin tabs cannot start simultaneous handshakes. This lock is released after setup.
        if (this.locks?.request)
          return await this.locks.request('web-serial-rtt-tools/probe-control', { signal: ticket.controller.signal }, execute);
        return await execute();
      });
      // A release callback can await a cancelled reconnect queued behind itself.
      // Settle that caller immediately, while keeping the queue entry until its turn.
      return await Promise.race([task, cancelled]);
    } catch (e){
      if (ticket.controller.signal.aborted && e.name === 'AbortError') throw new ProbeCancelled();
      throw e;
    } finally {
      ticket.controller.signal.removeEventListener('abort', onAbort);
      this.pending.delete(ticket);
    }
  }

  /** Cancel queued starts immediately; existing feature state machines drain starts already executing. */
  cancel(owner){
    for (const t of this.pending) if (t.owner === owner) t.controller.abort();
  }

  fail(owner, error){
    this.failures.set(owner, error);
    if (!this.leases.has(owner)) this.leases.set(owner, { owner, fault: true });
  }
  confirm(owner){ this.failures.delete(owner); }
  forget(owner){ this.leases.delete(owner); this.confirm(owner); }

  async releaseOthers(keep, reason = '另一个功能要使用探针'){
    // Compatibility calls made inside setup have already been arbitrated by run().
    if (keep && this.current?.owner === keep) return;
    const all = new Set([...this.clients.values()].flatMap(c => [...c.resources]));
    this._checkProtected(keep, all);
    for (const t of this.pending) if (t.owner !== keep) t.controller.abort();
    return await this._enqueue(async () => {
      this._checkProtected(keep, all);
      for (const [id, c] of this.clients){
        if (id === keep || (!this.leases.has(id) && !c.active())) continue;
        try { await c.release(reason); }
        catch (e){ this.fail(id, e); throw e; }
        this.leases.delete(id);
      }
    });
  }

  summary(){
    return {
      owners: [...this.leases.keys()],
      ...(this.cdcMode ? { cdc: this.cdcMode.state() } : {}),
      pending: [...this.pending].map(t => ({ owner: t.owner, phase: t.phase })),
      failures: [...this.failures].map(([owner, error]) => ({ owner, error: error.message })),
    };
  }
}

/** View adapters can run without a manager in standalone demos/tests. */
export async function runProbeOperation(view, owner, fn, { mock = false, ...opts } = {}){
  if (mock || !view.probeManager) return await fn();
  try { return await view.probeManager.run(owner, fn, opts); }
  catch (e){ if (e instanceof ProbeCancelled) return false; throw e; }
}
