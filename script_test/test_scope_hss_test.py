"""Offline protocol and measurement-window tests; no hidapi/PyUSB needed."""
import struct
import unittest
from unittest.mock import patch
import scope_hss_test as hss

class MeasurementTest(unittest.TestCase):
    def test_period_encoding(self):
        self.assertEqual(hss.config_data(3, hss.V_ONE)[0], 7)
        for us, ticks in [(2.25,54), (2.5,60), (2.75,66), (2.01,48)]:
            data = hss.config_data(us, hss.V_ONE, 0xa1)
            self.assertEqual(data[0], 10)
            self.assertEqual(struct.unpack_from('<I', bytes(data), 1)[0], ticks)
            self.assertEqual(data[5:7], [0xa1,1])
        for value in [float('nan'), float('inf'), 1, 1000001]:
            with self.assertRaises(ValueError): hss.config_data(value, [])

    def test_counter_and_timer_rollover(self):
        begin = dict(tick=0xfffffff0, hz=24000000, produced=0xfffffff0,
                     skipped=0xfffffff0, usb=70000, swd=65536, yield_=4)
        end = dict(tick=23999984, produced=20, skipped=3, usb=80000, swd=65540, yield_=5)
        delta = hss.snapshot_delta(begin, end)
        self.assertEqual(delta, dict(dt=1, produced=36, skipped=19, usb=10000, swd=4, yield_=1))

    def test_arrival_window_excludes_startup_and_drain(self):
        raw = [(0.9,b'startup'), (1,b'a'), (1.9,b'b'), (2,b'end'), (2.1,b'drain')]
        self.assertEqual(hss.arrival_window(raw, 1, 2), b'ab')

    def test_old_status_counter_fallback(self):
        begin = dict(host=1, produced=0xfffffffe, dropped=4, usb=65535)
        end = dict(host=3, produced=10, dropped=8, usb=2)
        self.assertEqual(hss.snapshot_delta(begin,end), dict(dt=2, produced=12,dropped=4,usb=3))

    def test_snapshot_validation(self):
        words = [0x31535348,100,24000000,80000,70000,60000,50000,40000,30000,20000,54,0xa1]
        res = [51,0x32,0] + list(struct.pack('<12I', *words))
        with patch.object(hss,'hid_xfer',return_value=res):
            snap = hss.snapshot(None,True)
        self.assertEqual(snap['usb'],60000)
        self.assertEqual(snap['period_ticks'],54)
        res[3] = 0
        with patch.object(hss,'hid_xfer',return_value=res):
            with self.assertRaises(RuntimeError): hss.snapshot(None,True)

    def test_configuration_capability_and_rejection(self):
        with patch.object(hss,'status',return_value=dict(supportsTicks=False,supportsBatch=False)):
            with self.assertRaises(RuntimeError): hss.do_config(None,2.5,hss.V_ONE)
            with self.assertRaises(RuntimeError): hss.do_config(None,3,hss.V_ONE,0x80)
        with patch.object(hss,'hid_xfer',return_value=[51,0x32,250]):
            with self.assertRaises(RuntimeError): hss.do_config(None,3,hss.V_ONE)

    def test_discard_stops_when_snapshot_fails(self):
        with patch.object(hss,'wait_started'), patch.object(hss,'snapshot',side_effect=RuntimeError('snapshot')), \
             patch.object(hss,'hid_xfer') as xfer:
            with self.assertRaises(RuntimeError): hss.run_discard(None,5,True)
            xfer.assert_called_once_with(None,[hss.ACT['STOP']])

    def test_v2_packet_units(self):
        packet = struct.pack('<HBBIIHH',0x4a53,2,2,1,60,1,4)+struct.pack('<I',0xffffffff)+bytes(492)
        stream = hss.PktStream()
        self.assertEqual(stream.push(packet[:250]),[])
        item = stream.push(packet[250:])[0]
        self.assertEqual((item['version'],item['t_raw'],item['t_us']), (2,60,2.5))
        self.assertEqual(struct.unpack_from('<I',item['payload'])[0],0xffffffff)

if __name__ == '__main__': unittest.main()
