import { AkaLinkHid, CMD } from '../hid/probe.js';

/** Configure only the probe's physical mux. Target traffic goes through Wasm. */
export async function prepareOpenOcdJtag(device){
  if (device.vendorId !== 0x0d28 || device.productId !== 0x0204 ||
      !device.serialNumber || !AkaLinkHid.supported()) return;
  const serial = device.serialNumber.toUpperCase();
  const devices = (await navigator.hid.getDevices()).filter(hid =>
    hid.vendorId === device.vendorId && hid.productId === device.productId &&
    hid.collections.some(collection => collection.usagePage === 0xff00));
  for (const candidate of devices){
    const hid = new AkaLinkHid();
    try {
      await hid.open(candidate);
      if ((await hid.text(CMD.SN)).toUpperCase() !== serial) continue;
      const config = (await hid.xfer(CMD.GET_CONFIG)).slice(2, 9);
      if (config.length !== 7) throw Error('探针引脚配置回读长度错误');
      if (config[0] !== 1){
        config[0] = 1;
        await hid.xfer(CMD.SET_CONFIG, config);
        const actual = (await hid.xfer(CMD.GET_CONFIG)).slice(2, 9);
        if (actual.length !== config.length || !actual.every((byte,i) => byte === config[i]))
          throw Error('未确认探针已切换到 SWD + JTAG');
      }
      return true;
    } finally { await hid.close(); }
  }
  return false;
}
