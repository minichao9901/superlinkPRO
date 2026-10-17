/** Analog math, independent of DOM/USB. Amplitude is peak; offset is DC volts. */
export const WAVES = Object.freeze({ sine: '正弦', square: '方波', triangle: '三角波', saw: '锯齿波↑', reverseSaw: '锯齿波↓', pulse: '脉冲', dc: '直流', noise: '白噪声（伪随机）' });
export function adcPlan({ channel, bits, rate, count = 0 }){
  if (!Number.isInteger(channel) || channel < 0 || channel > 15 || ![8, 10, 12, 16].includes(bits)) throw Error('ADC 通道或输出位宽无效');
  if (!Number.isFinite(rate) || rate < 1 / 60 || rate > 1000) throw Error('采样率范围为 1/60–1000 Sa/s');
  if (!Number.isInteger(count) || count < 0 || count > 0xFFFFFFFF) throw Error('采集次数必须为非负整数');
  const period = Math.round(1000 / rate);
  return { period, count, actualRate: 1000 / period, records: [{ kind: 4, data: Uint8Array.of(channel, bits) }] };
}
export function adcValue(data, bits, reference, gain = 1){
  if (data.length !== 2 || ![8, 10, 12, 16].includes(bits) || !Number.isFinite(reference) || reference <= 0 || ![1, 2].includes(gain)) throw Error('ADC 结果或标定无效');
  const code = data[0] | data[1] << 8;
  if (code >= 2 ** bits) throw Error('ADC 码值超出输出位宽');
  return { code, volts: code / (2 ** bits - 1) * reference * gain };
}
/** Linked generator controls: peak amplitude, Vpp, common mode and high/low levels. */
export function signalLevels({amplitude,offset,min,max,vpp},source='amplitude'){
  if(source==='range'){
    if(!Number.isFinite(min)||!Number.isFinite(max)||max<min)throw Error('最大电压必须不小于最小电压');
    amplitude=(max-min)/2;offset=(max+min)/2;
  }else if(source==='vpp')amplitude=vpp/2;
  if(!Number.isFinite(amplitude)||amplitude<0||!Number.isFinite(offset))throw Error('幅度必须为非负数，共模必须为有限电压');
  return {amplitude,offset,min:offset-amplitude,max:offset+amplitude,vpp:2*amplitude};
}
export function waveform({ shape = 'sine', rate = 10000, frequency = 100, amplitude = 1, offset = 1.65, reference = 3.3, bits = 12, duty = 50, points = 1024, seed = 1, phase = 0 } = {}){
  if (!Object.hasOwn(WAVES, shape) || ![8, 10, 12, 16].includes(bits) || !Number.isInteger(points) || points < 8 || points > 65536) throw Error('波形类型、位宽或点数无效');
  if (![rate, frequency, amplitude, offset, reference, duty, phase].every(Number.isFinite) || rate <= 0 || rate > 1e7 || frequency <= 0 || reference <= 0 || amplitude < 0 || duty <= 0 || duty >= 100) throw Error('波形参数无效');
  if (!['dc', 'noise'].includes(shape) && rate / frequency < 8) throw Error('每周期至少 8 个采样点，请提高更新率或降低频率');
  const low = shape === 'dc' ? offset : offset - amplitude, high = shape === 'dc' ? offset : offset + amplitude;
  if (low < 0 || high > reference) throw Error('offset ± 峰值幅度必须落在 0–参考电压之间，不自动削顶');
  let random = seed >>> 0 || 1;
  const phaseTurns=((phase%360)+360)%360/360;
  const rows = new Array(points);
  for (let i = 0; i < points; i++){
    const position = (i * frequency / rate + phaseTurns) % 1;
    let unit;
    switch (shape){
      case 'sine': unit = Math.sin(2 * Math.PI * position); break;
      case 'square': unit = position < 0.5 ? 1 : -1; break;
      case 'triangle': unit = 1 - 4 * Math.abs(position - 0.5); break;
      case 'saw': unit = 2 * position - 1; break;
      case 'reverseSaw': unit = 1 - 2 * position; break;
      case 'pulse': unit = position < duty / 100 ? 1 : -1; break;
      case 'dc': unit = 0; break;
      case 'noise': random ^= random << 13; random ^= random >>> 17; random ^= random << 5; unit = (random >>> 0) / 0xFFFFFFFF * 2 - 1; break;
    }
    const volts = offset + amplitude * unit;
    rows[i] = { time: i / rate, volts, code: Math.round(volts / reference * (2 ** bits - 1)) };
  }
  return rows;
}
export function adcCsv(rows){ return 'probe_time_ms,cycle,skipped,code,volts\n' + rows.map(r => `${r.timeMs},${r.cycle},${r.skipped},${r.code},${r.volts.toFixed(8)}`).join('\n') + '\n'; }
export function waveCsv(rows){ return 'time_s,volts,code\n' + rows.map(r => `${r.time.toFixed(9)},${r.volts.toFixed(8)},${r.code}`).join('\n') + '\n'; }

/** A periodic LUT has exactly one cycle; do not loop the arbitrary preview window. */
export function dacTable(options, caps){
  const rate=options.rate, frequency=options.frequency;
  if(!Number.isInteger(rate)||rate<1||rate>caps.maxRate)throw Error('更新率需为固件范围内的整数');
  if(!Number.isFinite(frequency)||frequency<=0)throw Error('波形频率无效');
  const points=options.shape==='dc'?8:options.shape==='noise'?(options.points||1024):Math.round(rate/frequency);
  if(points<8||points>caps.maxPoints||rate/frequency<8&&!['dc','noise'].includes(options.shape))throw Error('每周期点数超出 DAC 能力；调整频率或更新率');
  const rows=waveform({...options,rate,frequency:rate/points,points,bits:caps.bits,reference:caps.fullScale});
  return {rows,codes:rows.map(r=>r.code),actualFrequency:['dc','noise'].includes(options.shape)?null:rate/points};
}
