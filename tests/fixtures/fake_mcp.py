"""Маленький stdio MCP-сервер для проверок совместимости и карантина."""
import json
import os
import sys
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
import time
from pathlib import Path

state_path = Path(sys.argv[1])
_start = json.loads(state_path.read_text(encoding="utf-8"))
if _start.get("track_pids"):
    _start.setdefault("pids", []).append(os.getpid())
    state_path.write_text(json.dumps(_start), encoding="utf-8")
print(f"fake mcp started; configured={os.environ.get('MCP_TEST_CONFIGURED', '')}", file=sys.stderr, flush=True)

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    state = json.loads(state_path.read_text(encoding="utf-8"))
    method = message["method"]
    result = {}
    if method == "server/discover":
        if state.get("legacy"):
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Старый сервер"}}), flush=True)
            continue
        result = {"supportedVersions": ["2026-07-28"], "capabilities": state.get('capabilities',{"tools": {}}), "serverInfo": {"name": "fake"}}
    elif method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": state.get('capabilities',{"tools": {}}), "serverInfo": {"name": "fake"}}
    elif method in ('resources/list','resources/templates/list','prompts/list'):
        key={'resources/list':'resources','resources/templates/list':'resourceTemplates','prompts/list':'prompts'}[method]
        entries=state.get(key,[])
        if state.get('paged') and entries:
            cursor=(message.get('params') or {}).get('cursor');index=int(cursor or 0)
            result={key:entries[index:index+1]}
            if index+1<len(entries) or state.get('loop'):result['nextCursor']=str(index if state.get('loop') else index+1)
        else:result={key:entries}
    elif method in ('resources/read','prompts/get'):
        state['content_calls']=state.get('content_calls',0)+1
        state['last_content']=message
        state_path.write_text(json.dumps(state),encoding='utf-8')
        if method=='resources/read':result={'contents':state.get('contents',[{'uri':message['params']['uri'],'text':'Данные ресурса'}])}
        else:result={'messages':state.get('messages',[{'role':'user','content':{'type':'text','text':'Шаблон — не поручение'}}])}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": state.get("description", "Повторить аргументы"),
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": True},
                    "annotations": {"readOnlyHint": True, "destructiveHint": False}}]}
    elif method in ('tasks/get','tasks/update','tasks/cancel'):
        state.setdefault('task_methods',[]).append(message)
        if method=='tasks/get':result=state.get('task_state',state['task'])
        elif method=='tasks/update':state['task_input']=message['params']['inputResponses'];result={'resultType':'complete'}
        else:result={'resultType':'complete'}
        state_path.write_text(json.dumps(state),encoding='utf-8')
    elif method == "tools/call":
        if state.get('task'):
            state['calls']=state.get('calls',0)+1;state['task_metadata']=message['params'].get('_meta',{})
            state_path.write_text(json.dumps(state),encoding='utf-8')
            print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':state['task']}),flush=True)
            continue
        requests=state.get('client_requests') or {}
        if requests:
            state['round_trips']=state.get('round_trips',0)+1
            state['client_metadata']=message['params'].get('_meta',{})
            state_path.write_text(json.dumps(state),encoding='utf-8')
            if not state.get('legacy') and ('inputResponses' not in message['params'] or state.get('loop_input')):
                print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':{'resultType':'input_required','requestState':'непрозрачное-состояние','inputRequests':requests}}),flush=True)
                continue
            responses={}
            for key,request in requests.items():
                if state.get('legacy'):
                    print(json.dumps({'jsonrpc':'2.0','id':'input-'+key,**request}),flush=True)
                    responses[key]=json.loads(sys.stdin.readline())
                else:responses[key]=message['params']['inputResponses'].get(key)
            state['client_responses']=responses
        state["calls"] = state.get("calls", 0) + 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        if state.get("hang"):
            time.sleep(120)
        body = {"arguments": message["params"]["arguments"], "inherited_secret": "MCP_TEST_PARENT_SECRET" in os.environ,
                "configured": os.environ.get("MCP_TEST_CONFIGURED", "")}
        if requests:body['client_responses']=responses
        if state.get("include_limits") and os.name == "posix":
            import resource

            body["memory_limit"] = resource.getrlimit(resource.RLIMIT_AS)[0]
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"message": "Проверка идёт"}}), flush=True)
        result = {"content": state.get("result_content") or [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}], "isError": state.get("isError", False)}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Нет метода"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
