"""Compare persistent stdio MCP with fresh CLI processes on identical disposable data."""
import argparse
import json
import os
from pathlib import Path
import select
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='agentchat-mcp-comparison-') as temp:
        root=Path(temp);cli_data=root/'cli';mcp_data=root/'mcp'
        rows=[dict(id=i,ts='2026-11-17T00:00:00+01:00',sender='user',kind='msg',
                   text=f'message {i}: unicode 🟣\n'+('sample '*20)) for i in range(1,21)]
        for data in [cli_data,mcp_data]:
            (data/'chat').mkdir(parents=True)
            (data/'chat/2026-11-17.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
        env=dict(os.environ,AGENTCHAT_DATA=str(mcp_data),PYTHONDONTWRITEBYTECODE='1')
        start=time.perf_counter();proc=subprocess.Popen([str(ROOT/'bin/chat-mcp'),'--as','codex'],
              cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        sequence=0
        def rpc(method,params):
            nonlocal sequence
            sequence+=1
            request=json.dumps(dict(jsonrpc='2.0',id=sequence,method=method,params=params),separators=(',',':'))+'\n'
            proc.stdin.write(request);proc.stdin.flush()
            if not select.select([proc.stdout],[],[],10)[0]:raise TimeoutError('MCP did not respond')
            wire=proc.stdout.readline();reply=json.loads(wire)
            if 'error' in reply:raise RuntimeError(reply['error'])
            return reply['result'],len(wire.encode())
        def native(name,arguments):
            result,wire_bytes=rpc('tools/call',dict(name=name,arguments=arguments))
            if result.get('isError'):raise RuntimeError(result)
            text=result['content'][0]['text'];return json.loads(text),len(text.encode()),wire_bytes
        def cli(*words):
            result=subprocess.run([str(ROOT/'bin/chat'),*words],cwd=root,
                   env=dict(os.environ,AGENTCHAT_DATA=str(cli_data),PYTHONDONTWRITEBYTECODE='1'),
                   capture_output=True,text=True,timeout=10)
            if result.returncode not in (0,2):raise RuntimeError(result.stderr)
            return result.stdout
        try:
            rpc('initialize',dict(protocolVersion='2025-11-25',capabilities={},clientInfo=dict(name='comparison',version='1')))
            proc.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');proc.stdin.flush()
            listing,_=rpc('tools/list',{});startup_ms=(time.perf_counter()-start)*1000
            results=dict(samples={},mcp_startup_and_tool_discovery_ms=round(startup_ms,2),
                         tool_definition_bytes=len(json.dumps(listing,separators=(',',':')).encode()))
            for mode in ['cli','mcp']:
                times=[];sizes=[]
                for _ in range(10):
                    start=time.perf_counter()
                    if mode=='cli':
                        text=cli('wait','--as','codex','--timeout','0','--compact');items=[json.loads(x) for x in text.splitlines()];size=len(text.encode())
                    else:
                        value,size,_=native('chat_read',{});items=value['messages']
                    times.append((time.perf_counter()-start)*1000);sizes.append(size)
                    assert items==[[r[k] for k in ('id','sender','kind','text')] for r in rows]
                results['samples'][mode+'_read']=dict(n=10,median_ms=round(statistics.median(times),2),content_bytes=sizes[0])
            text='comparison post 🟣\n'+'x'*520
            for mode in ['cli','mcp']:
                times=[];sizes=[]
                for _ in range(4):
                    start=time.perf_counter()
                    if mode=='cli':
                        output=cli('say','--from','codex',text);receipt=json.loads(output);size=len(output.encode())
                    else:receipt,size,_=native('chat_post',dict(text=text))
                    assert receipt['sender']=='codex'
                    times.append((time.perf_counter()-start)*1000);sizes.append(size)
                results['samples'][mode+'_post']=dict(n=4,median_ms=round(statistics.median(times),2),content_bytes=round(statistics.mean(sizes)))
            cli('ack','--as','codex','20');native('chat_ack',dict(id=20))
            for data in [cli_data,mcp_data]:
                records=[]
                for log in sorted((data/'chat').glob('*.jsonl')):records.extend(json.loads(x) for x in log.read_text().splitlines())
                assert [r['text'] for r in records if r['sender']=='codex']==[text]*4
                assert json.loads((data/'cursors/codex.json').read_text())['acked']==20
            stat=lambda:sum(map(int,Path(f'/proc/{proc.pid}/stat').read_text().split()[13:15]))
            before=stat();time.sleep(1);ticks=stat()-before
            results['idle']=dict(interval_seconds=1,cpu_ticks=ticks,rss_kib=int(Path(f'/proc/{proc.pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')//1024,
                                 cli_persistent_processes=0)
            results['correctness']='Exact read text/order, four posts per transport and explicit ack20 agree; no live data'
            results['limits']=['Transport/fixture measurements, not token or billing measurements.','MCP tool definitions and persistent memory are additional costs.','MCP content envelope differs from CLI JSON lines; bytes are reported separately by operation.']
            args.output.write_text(json.dumps(results,indent=2)+'\n');print(json.dumps(results,indent=2))
        finally:
            proc.stdin.close()
            try:proc.wait(5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()
            proc.stdout.close();proc.stderr.close()

if __name__=='__main__':main()
