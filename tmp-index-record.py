import sys, time
sys.path.insert(0,'etm')
import server
sid='capture-ca13925b3cd4453ca5a097aa41554c18'
t=time.time()
store=server.browse_store(sid)
store.ensure()
print('index built in %.1fs' % (time.time()-t))
c=server.app.test_client()
r=c.get('/api/browse/'+sid+'/overview')
print('overview', r.status_code, r.json.get('eventCount'), r.json.get('summary',{}).get('totalCycles'))
p=c.get('/api/browse/'+sid+'/page?offset=4371168&limit=3')
print('page', p.status_code, [row['seq'] for row in p.json.get('rows',[])])
d=c.get('/api/records/'+sid)
print('record', d.status_code, d.json.get('summary',{}).get('totalCycles'))
