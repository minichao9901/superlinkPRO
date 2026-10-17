"""真实封存窗口只建立派生索引；核对分页与原记录的语义和数量。"""
import json,sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT));import server
class BaselineBrowseTests(unittest.TestCase):
    def test_real_long_record_summary_queries_and_symbol_table(self):
        sid='capture-8365b3adb65b4f0cb281e55aef2c89c7';c=server.app.test_client()
        overview=c.get('/api/browse/'+sid+'/overview');self.assertEqual(overview.status_code,200)
        r=overview.json;self.assertEqual(r['summary']['instructions'],484570)
        self.assertEqual(r['quality']['unknownExecution'],397);self.assertEqual(r['quality']['processedBytes'],31014)
        self.assertEqual(r['spiValidation']['closedSpiCalls'],132);self.assertLess(len(overview.data),100000)
        self.assertNotIn('events',r);self.assertNotIn('sources',r)
        page=c.get('/api/browse/'+sid+'/page?offset=150&limit=150');self.assertEqual(page.json['rows'][0]['seq'],150)
        self.assertLess(len(page.data),150000)
        nav=c.get('/api/browse/'+sid+'/navigate?seq=2&mode=call').json;self.assertEqual(nav['seq'],3)
        event=c.get('/api/browse/'+sid+'/event/4').json
        self.assertEqual(event['event']['function'],'HAL_RCC_GetSysClockFreq')
        self.assertEqual(event['callPath'][-2:],['spi_trace_ready','HAL_RCC_GetSysClockFreq'])
        funcs=c.get('/api/record-elf/'+sid).json['functions']
        self.assertTrue(any(f['name']=='HAL_SPI_Transmit' and f['address']%2==0 for f in funcs))
        self.assertTrue(all(0x08000000<=f['address']<0x08020000 for f in funcs))
if __name__=='__main__':unittest.main()
