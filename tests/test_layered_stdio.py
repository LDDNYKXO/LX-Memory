"""Real stdio framing with fake read-only memory, never a native host acceptance."""
import json
import os
from pathlib import Path
from queue import Queue, Empty
import subprocess
import sys
from threading import Thread
import unittest


class LayeredStdioTests(unittest.TestCase):
    def test_legacy_initialize_background_and_paged_audit(self):
        root = Path(__file__).resolve().parents[1]
        child = subprocess.Popen([sys.executable, '-B', str(root/'tests/fixtures/layered_stdio_server.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', cwd=root,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE':'1','PYTHONIOENCODING':'utf-8'},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        queue = Queue()
        def reader():
            for line in child.stdout:
                queue.put(line)
        Thread(target=reader, daemon=True).start()
        def send(data):
            child.stdin.write(json.dumps(data, ensure_ascii=False)+'\n'); child.stdin.flush()
        def request(identifier, method, params):
            send({'jsonrpc':'2.0','id':identifier,'method':method,'params':params})
            for _ in range(30):
                try:
                    value = json.loads(queue.get(timeout=5))
                except Empty:
                    self.fail('isolated stdio response timed out')
                if value.get('id') == identifier:
                    self.assertNotIn('error', value, value)
                    return value['result']
            self.fail('too many unrelated stdio messages')
        try:
            request(1,'initialize',{'protocolVersion':'2025-06-18','capabilities':{},
                                     'clientInfo':{'name':'isolated-test','version':'1'}})
            send({'jsonrpc':'2.0','method':'notifications/initialized'})
            tools = request(2,'tools/list',{})['tools']
            self.assertEqual(len(tools),9)
            names = {t['name'] for t in tools}
            self.assertIn('lingxi_memory_validate', names)
            self.assertIn('lingxi_memory_archive', names)
            tool = next(t for t in tools if t['name']=='lingxi_memory_context')
            self.assertEqual(tool['inputSchema']['required'],['request'])
            result = request(3,'tools/call',{'name':'lingxi_memory_context',
                'arguments':{'request':'继续','project':'Example','request_id':'stdio-1'}})
            data = json.loads(result['content'][0]['text'])
            self.assertEqual(data['state'],'complete')
            self.assertEqual(data['constraints'],['不保存、不发布。','不得自动提交。'])
            self.assertEqual(data['essential']['constraints_status'],'complete')
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),8000)
            # Older protocol versions may omit resultType; the server budgets its superset.
            self.assertGreaterEqual(data['budget']['output_chars'],len(json.dumps(result,ensure_ascii=False)))
            snap=data['audit']['snapshot_id']; cursor=''; fragments=[]
            for i in range(4, 70):
                result=request(i,'tools/call',{'name':'lingxi_memory_context','arguments':
                    {'request':'审计','view':'audit','snapshot_id':snap,'cursor':cursor}})
                self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),8000)
                audit=json.loads(result['content'][0]['text'])
                self.assertEqual(audit['snapshot_id'],snap)
                fragments.extend(x['content'] for x in audit['sections'] if x['name']=='sources')
                if not audit['has_more']: break
                cursor=audit['next_cursor']
            else: self.fail('audit did not finish')
            sources=json.loads(''.join(fragments))
            self.assertIn('### 审计附件',sources[0]['content'])
            self.assertEqual(data['budget']['search_attempts'],2)
            self.assertEqual(data['budget']['reads'],2)
        finally:
            child.stdin.close()
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
            child.stdout.close(); child.stderr.close()
