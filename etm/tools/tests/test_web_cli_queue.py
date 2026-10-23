"""Queue admission, cancellation routing and completion ownership; no hardware."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from flask import Flask
from web_cli_queue import install_web_cli


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=Flask(__name__)
        self.path=Path(self.tmp.name)/'token';install_web_cli(self.app,self.path)
        self.client=self.app.test_client();self.headers={'X-Trace-CLI':self.path.read_text()};self.worker='a'*32

    def tearDown(self):self.tmp.cleanup()

    def poll(self):return self.client.post('/trace-cli/poll',json={'worker':self.worker},headers=self.headers)

    def test_token_and_loopback_are_required(self):
        self.assertEqual(self.client.post('/trace-cli/request',json={'action':'status'}).status_code,403)
        self.assertEqual(self.client.post('/trace-cli/request',json={'action':'status'},headers=self.headers,environ_base={'REMOTE_ADDR':'192.0.2.2'}).status_code,403)
        self.assertEqual(self.client.post('/trace-cli/request',json={'action':'status'},headers=dict(self.headers,Origin='https://evil.example')).status_code,403)

    def test_missing_worker_is_not_queued_for_surprise_later_execution(self):
        r=self.client.post('/trace-cli/request',json={'action':'flash'},headers=self.headers)
        self.assertEqual(r.status_code,409)

    def test_commands_are_claimed_once_and_completion_is_bound_to_worker(self):
        self.poll()
        r=self.client.post('/trace-cli/request',json={'action':'command','commands':['halt']},headers=self.headers)
        jid=r.json['id'];job=self.poll().json['job'];self.assertEqual(job['id'],jid)
        path='/trace-cli/complete/'+jid
        self.assertEqual(self.client.post(path,json={'worker':'b'*32,'result':{}},headers=self.headers).status_code,409)
        self.assertEqual(self.client.post(path,json={'worker':self.worker,'result':{'ok':True}},headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/trace-cli/result/'+jid,headers=self.headers).json['state'],'done')
        self.assertEqual(self.client.post('/trace-cli/ack/'+jid,json={},headers=self.headers).status_code,200)
        self.assertEqual(self.client.get('/trace-cli/result/'+jid,headers=self.headers).status_code,404)
        self.assertIsNone(self.poll().json['job'])

    def test_cancel_is_delivered_before_queued_work_and_second_worker_is_rejected(self):
        self.poll()
        self.client.post('/trace-cli/request',json={'action':'command','commands':['c']},headers=self.headers)
        self.client.post('/trace-cli/request',json={'action':'cancel'},headers=self.headers)
        self.assertEqual(self.poll().json['job']['request']['action'],'cancel')
        self.assertEqual(self.client.post('/trace-cli/poll',json={'worker':'b'*32},headers=self.headers).status_code,409)

    def test_arbitrary_eval_is_not_an_action(self):
        self.assertEqual(self.client.post('/trace-cli/request',json={'action':'eval','source':'anything'},headers=self.headers).status_code,400)


if __name__=='__main__':unittest.main()
