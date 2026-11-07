/** Bound the caller's wait; the transport still owns any in-flight request. */
export async function withTimeout(promise, ms, label){
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(Error(`${label} 超时（${ms}ms）：探针没响应`)), ms);
  });
  try { return await Promise.race([promise, timeout]); }
  finally { clearTimeout(timer); }
}
