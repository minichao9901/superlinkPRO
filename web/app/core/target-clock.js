/** Product Web limits. Native research CLIs deliberately keep their own limits. */
export const isHpmClockTarget = target => /^hpm/i.test(String(target?.id || target || ''));
export const webClockLimitKhz = target => isHpmClockTarget(target) ? 25000 : 60000;
export function webClockKhz(value,target,fallback=0){
  const hz=Number(value),selected=Number.isFinite(hz)&&hz>0?hz:fallback;
  return Math.min(webClockLimitKhz(target),Math.max(0,selected));
}
