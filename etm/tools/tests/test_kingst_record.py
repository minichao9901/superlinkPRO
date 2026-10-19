import csv
import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kingst_record import layout
from trace_port import extract, read_tracks
from test_trace_port import waveform


def make_record(csv_path, native_path):
    initial, tracks, start, end = read_tracks(csv_path)
    rate=500000000;trigger=round(-start*rate)
    depth=max(round(end*rate)+trigger+1,1)
    xml=b'<settings><global><version>3.6.6</version></global></settings>'
    data=bytearray(xml+b'kvdat\0\0\0'+struct.pack('<4Q',depth,rate,trigger,len(initial)))
    for ch,level in initial.items():
        positions=[round(t*rate)+trigger for t,_ in tracks[ch]]
        data+=struct.pack('<IBBHQ',0x442323,ch,level,0,len(positions)+1)
        for pos in positions+[depth]:data+=struct.pack('<IB',pos,0)
    native_path.write_bytes(data)


class KingstRecordTests(unittest.TestCase):
    def test_native_and_csv_recover_same_payload_in_both_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path=Path(tmp)/'capture.csv';native=Path(tmp)/'capture.kvdat'
            for clock in (0,1):
                for phase in (None,11):
                    waveform(csv_path,bytes(range(64)),clock,prefix=phase)
                    with csv_path.open(encoding='utf-8-sig') as stream:rows=list(csv.reader(stream))
                    for row in rows[1:]:row[0]=str(round(float(row[0])*500000000)/500000000)
                    with csv_path.open('w',newline='',encoding='utf8') as stream:csv.writer(stream).writerows(rows)
                    make_record(csv_path,native);before=hashlib.sha256(native.read_bytes()).hexdigest()
                    for threshold in (0,2,4):
                        csv_report,a=extract(csv_path,0,[1,2,3,4],.25,threshold)
                        native_report,b=extract(native,0,[1,2,3,4],.25,threshold)
                        self.assertEqual(a,b)
                        self.assertEqual(csv_report['software_filter'],native_report['software_filter'])
                    self.assertEqual(before,hashlib.sha256(native.read_bytes()).hexdigest())
    def test_constant_channels_and_truncation_or_unknown_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path=Path(tmp)/'capture.csv';native=Path(tmp)/'capture.kvdat'
            waveform(csv_path,b'\xff'*8,0);make_record(csv_path,native)
            good=native.read_bytes();self.assertTrue(layout(native))
            for bad in [good[:-1],good+b'x',good.replace(b'3.6.6',b'3.7.0')]:
                native.write_bytes(bad)
                with self.assertRaises(ValueError):extract(native,0,[1,2,3,4])
            native.write_bytes(good);info=layout(native);bad=bytearray(good)
            bad[info['channels'][0]['offset']+4]=1;native.write_bytes(bad)
            with self.assertRaises(ValueError):extract(native,0,[1,2,3,4])
    def test_missing_channels_and_invalid_initial_level_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path=Path(tmp)/'capture.csv';native=Path(tmp)/'capture.kvdat'
            waveform(csv_path,b'\xa5'*8,0);make_record(csv_path,native)
            with self.assertRaises(ValueError):extract(native,0,[1,2,3,15])
            raw=bytearray(native.read_bytes());marker=raw.index(b'kvdat\0\0\0')
            raw[marker+40+5]=2;native.write_bytes(raw)
            with self.assertRaises(ValueError):layout(native)
    def test_native_export_uses_save_only_and_protects_existing_evidence(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'la'))
        from kingst_la import do_export
        with tempfile.TemporaryDirectory() as tmp:
            csv_path=Path(tmp)/'fixture.csv';native=Path(tmp)/'capture.kvdat'
            waveform(csv_path,b'\xa5'*8,0)
            commands=[]
            class Vis:
                def cmd(self,command,**kw):
                    commands.append(command);make_record(csv_path,native);return 'ACK'
            self.assertEqual(do_export(Vis(),str(native)),(str(native),str(native)))
            self.assertEqual(commands,[f'save-data "{native}"'])
            with self.assertRaises(FileExistsError):do_export(Vis(),str(native))


if __name__=='__main__':unittest.main()
