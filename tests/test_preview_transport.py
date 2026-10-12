import json
import subprocess
import unittest
from tests.preview_support import ROOT


def run(body):
    result=subprocess.run(['node','--input-type=module','--eval',"import {createPreviewApi} from './frontend/preview/api.js';\n"+body],cwd=ROOT,capture_output=True,text=True,encoding='utf-8')
    if result.returncode: raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class TransportTests(unittest.TestCase):
    def test_all_read_and_download_paths_stay_under_preview(self):
        result=run("""
        const calls=[]; const api=createPreviewApi({fetchImpl:async(path,options)=>{calls.push([path,options.method]);return new Response(JSON.stringify({ok:true,data:{items:[1]}}),{headers:{'Content-Type':'application/json'}});},csrfToken:()=> 'token'});
        await api.get('/api/wells?page=2'); await api.request('/api/dynagraphs');
        await api.download('/api/reports/production.csv','report.csv',{save:false});
        console.log(JSON.stringify(calls));
        """)
        self.assertEqual([x[0] for x in result],['/api/preview/legacy/api/wells?page=2','/api/preview/legacy/api/dynagraphs','/api/preview/legacy/api/reports/production.csv'])

    def test_escape_and_mutation_never_reach_fetch(self):
        result=run("""
        const calls=[]; let confirmations=0; const api=createPreviewApi({fetchImpl:async p=>{calls.push(p);throw Error('unexpected');},confirmRehearsal:async()=>{confirmations++;return null;}});
        let rejected=0;
        for(const p of ['https://example.com/api/wells','//127.0.0.1:8765/api/wells','/api/%252e%252e/private','/api/wells/../commands','/private/accounts.json']) {try{await api.get(p);}catch{rejected++;}}
        for(const method of ['post','patch','raw']) {try{await api[method]('/api/devices/D1/commands',method==='raw'?new Uint8Array([1,2]):{action:'start'});}catch{rejected++;}}
        console.log(JSON.stringify({calls,rejected,confirmations}));
        """)
        self.assertEqual(result['calls'],[]); self.assertEqual(result['rejected'],8)
        self.assertEqual(result['confirmations'],3)

    def test_cancelled_response_does_not_return_after_disposal(self):
        result=run("""
        let resolve; const api=createPreviewApi({fetchImpl:()=>new Promise(r=>resolve=r)});
        const pending=api.get('/api/wells').then(()=>false,e=>e.code==='cancelled');
        api.dispose();resolve(new Response(JSON.stringify({ok:true,data:{items:[]}}),{headers:{'Content-Type':'application/json'}}));
        console.log(JSON.stringify({cancelled:await pending}));
        """)
        self.assertTrue(result['cancelled'])
