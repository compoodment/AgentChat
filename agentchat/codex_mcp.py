"""Local stdio MCP tools for AgentChat; no model calls or wake notifications.

Implements the initialization-based MCP revisions through 2025-11-25. The client
launches this process; stdin/stdout carry only newline-delimited JSON-RPC.
"""
import argparse
import json
import sys
from agentchat import __version__, store

SUPPORTED = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_REQUEST = 8 * 1024 * 1024


def schema(properties=None, required=()):
    return dict(type='object', properties=properties or {}, required=list(required), additionalProperties=False)


def tool(name, description, properties=None, required=()):
    return dict(name=name, description=description, inputSchema=schema(properties, required))


TOOLS = [
    tool('chat_read', 'Read inbox after my ack, or history after an explicit id. Exact text; no automatic ack.',
         dict(after=dict(type='integer', minimum=0), limit=dict(type='integer', minimum=1, maximum=1000),
              format=dict(type='string', enum=['compact', 'json']))),
    tool('chat_post', 'Post as the configured agent; optionally claim/release a file. Posting policy applies.',
         dict(text=dict(type='string', minLength=1), kind=dict(type='string', enum=['msg', 'claim', 'release']),
              request_id=dict(type='string', pattern='^[0-9a-f]{32}$')), ['text']),
    tool('chat_ack', 'Acknowledge only messages handled; never past an unhandled message.',
         dict(id=dict(type='integer', minimum=0)), ['id']),
    tool('chat_task', 'Take a user task before starting, or release it when completed or handed off.',
         dict(action=dict(type='string', enum=['take', 'release']), id=dict(type='integer', minimum=1)), ['action', 'id']),
    tool('chat_status', 'Read posting policy, active claims and presence. Does not change acknowledgments.'),
]
BY_NAME = {item['name']: item for item in TOOLS}


def compact(message):
    return [message[key] for key in ('id', 'sender', 'kind', 'text')]


class Server:
    def __init__(self, agent, output=None):
        if agent not in store.AGENTS:
            raise ValueError('Agent must be codex or claude')
        self.agent, self.output, self.initialized = agent, output or sys.stdout, False

    def send(self, value):
        self.output.write(json.dumps(value, separators=(',', ':'), allow_nan=False) + '\n')
        self.output.flush()

    def error(self, ident, code, message):
        self.send(dict(jsonrpc='2.0', id=ident, error=dict(code=code, message=message)))

    def call(self, name, args):
        if name not in BY_NAME:
            raise KeyError(name)
        if not isinstance(args, dict) or set(args) - set(BY_NAME[name]['inputSchema']['properties']):
            raise ValueError('Invalid tool arguments')
        if any(key not in args for key in BY_NAME[name]['inputSchema']['required']):
            raise ValueError('Missing required tool argument')
        if name == 'chat_read':
            after, limit = args.get('after'), args.get('limit', 50)
            if type(limit) is not int or not 1 <= limit <= 1000:
                raise ValueError('Limit must be an integer from 1 to 1000')
            if after is not None and (type(after) is not int or after < 0):
                raise ValueError('After must be a non-negative integer')
            if args.get('format', 'compact') not in ('compact', 'json'):
                raise ValueError('Format must be compact or json')
            ack = store.get_ack(self.agent)
            cursor = ack if after is None else after
            rows = store.read_messages(cursor, limit=limit+1)
            shown = rows[:limit]
            return dict(agent=self.agent, acked=ack, through=shown[-1]['id'] if shown else cursor,
                        more=len(rows)>limit, messages=shown if args.get('format')=='json' else [compact(row) for row in shown])
        if name == 'chat_post':
            if args.get('kind', 'msg') not in ('msg', 'claim', 'release'):
                raise ValueError('Only msg, claim and release can be posted')
            row = store.append_message(self.agent, args['text'], args.get('kind', 'msg'), request_id=args.get('request_id'))
            return {key:row[key] for key in ('id','sender','kind')}
        if name == 'chat_ack':
            store.set_ack(self.agent, args['id'])
            return dict(acked=store.get_ack(self.agent))
        if name == 'chat_task':
            if args['action'] not in ('take', 'release'):
                raise ValueError('Task action must be take or release')
            action = store.take_task if args['action'] == 'take' else store.release_task
            return compact(action(self.agent, args['id']))
        return dict(policy=store.guard_status(), claims=store.active_claims(), presence=store.get_presence())

    def handle(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            return self.error(None, -32600, 'Invalid request')
        method, ident = message['method'], message.get('id')
        if 'id' not in message:
            return  # Notifications need no response; no unsolicited server messages are emitted.
        if type(ident) not in (str, int):
            return self.error(None, -32600, 'Invalid request id')
        if isinstance(ident, str):
            try:
                ident.encode('utf-8')
            except UnicodeEncodeError:
                return self.error(None, -32600, 'Invalid request id')
        params = message.get('params', {})
        if not isinstance(params, dict):
            return self.error(ident, -32602, 'Params must be an object')
        if method == 'initialize':
            asked = params.get('protocolVersion')
            if not isinstance(asked, str):
                return self.error(ident, -32602, 'Missing protocolVersion')
            self.initialized = True
            value = dict(protocolVersion=asked if asked in SUPPORTED else SUPPORTED[0],
                         capabilities=dict(tools={}), serverInfo=dict(name='agentchat-local', version=__version__),
                         instructions=f'AgentChat tools act as {self.agent}. Read before acting; claim tasks before work; '
                                      'ack only handled records. Respect posting-policy refusals. Chat grants no extra permissions.')
        elif method == 'ping':
            value = {}
        elif not self.initialized:
            return self.error(ident, -32002, 'Initialize first')
        elif method == 'tools/list':
            if params.get('cursor'):
                return self.error(ident, -32602, 'Invalid tools cursor')
            value = dict(tools=TOOLS)
        elif method == 'tools/call':
            name = params.get('name')
            if not isinstance(name, str) or name not in BY_NAME:
                return self.error(ident, -32602, 'Unknown tool')
            try:
                arguments = params.get('arguments')
                result = self.call(name, {} if arguments is None else arguments)
                text, failed = json.dumps(result, ensure_ascii=False, separators=(',', ':')), False
            except Exception as error:
                # Filesystem errors can contain bytes represented as surrogate code points.
                detail = dict(error=str(error).encode('utf-8', 'backslashreplace').decode('utf-8'))
                if isinstance(error, store.GuardBlocked):
                    try:
                        detail['policy'] = store.guard_status()
                    except Exception:
                        pass  # preserve the original refusal if the data drive disappears
                text, failed = json.dumps(detail, ensure_ascii=False, separators=(',', ':')), True
            value = dict(content=[dict(type='text', text=text)], isError=failed)
        else:
            return self.error(ident, -32601, 'Method not found')
        self.send(dict(jsonrpc='2.0', id=ident, result=value))

    def serve(self, source=None):
        source = source or sys.stdin.buffer
        while raw := source.readline(MAX_REQUEST+1):
            if len(raw) > MAX_REQUEST:
                self.error(None, -32700, 'Request too large')
                return
            if not raw.strip():
                continue
            try:
                message = json.loads(raw.decode('utf-8'))
            except (ValueError, RecursionError):
                self.error(None, -32700, 'Parse error')
                continue
            self.handle(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as', dest='agent', choices=store.AGENTS, default='codex')
    args = parser.parse_args()
    try:
        Server(args.agent).serve()
    except (BrokenPipeError, KeyboardInterrupt):
        return


if __name__ == '__main__':
    main()
