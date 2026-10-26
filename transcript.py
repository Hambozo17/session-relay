from __future__ import annotations
import json, pathlib

def latest_context(path):
    # Long tool output can push a turn's settings outside the normal tail.
    # Walk backward to the latest context without reading the full history.
    with pathlib.Path(path).open('rb') as file:
        file.seek(0,2);position=file.tell();carry=b''
        while position:
            length=min(position,1_000_000);position-=length;file.seek(position)
            parts=(file.read(length)+carry).split(b'\n');carry=parts[0]
            for line in reversed(parts[1:]):
                if b'"turn_context"' not in line[:300]:continue
                try:row=json.loads(line)
                except ValueError:continue
                if row.get('type')=='turn_context':return row.get('payload') or {}
            # Oversized non-context lines need not be retained in memory.
            if len(carry)>2_000_000:carry=carry[:300]
        if b'"turn_context"' in carry[:300]:
            try:return json.loads(carry).get('payload') or {}
            except ValueError:pass
    return {}

def read_transcript(path):
    path = pathlib.Path(path)
    info = {'path': str(path), 'status': 'unknown', 'error_kind': None}
    with path.open('rb') as file:
        first = file.readline()
        try:
            meta = json.loads(first).get('payload', {})
            info.update(id=meta.get('id'), cwd=meta.get('cwd'), is_child=bool(meta.get('source', {}).get('subagent')) if isinstance(meta.get('source'), dict) else False)
        except ValueError: raise RuntimeError('Session metadata is unreadable')
        file.seek(max(len(first), path.stat().st_size - 2_000_000))
        if file.tell() > len(first): file.readline()
        for line in file:
            try: row = json.loads(line)
            except ValueError: continue
            payload = row.get('payload') or {}
            if row.get('type') == 'turn_context':
                info.update({k: payload.get(k) for k in ('model', 'effort', 'model_provider', 'approval_policy', 'sandbox_policy')})
                if payload.get('turn_id') and (info.get('turn_id') != payload['turn_id'] or info['status']=='unknown'):
                    info.update(turn_id=payload['turn_id'],status='running',error_kind=None)
            if row.get('type') != 'event_msg': continue
            event = payload.get('type')
            if event in ('task_started','task_complete','turn_aborted','error'): info['event_pos'] = file.tell()
            if event == 'task_started': info.update(status='running', error_kind=None, turn_id=payload.get('turn_id'))
            elif event == 'task_complete': info.update(status='idle', error_kind=None)
            elif event == 'turn_aborted':
                # Only explicit machine-readable quota/login reasons override a
                # stop. Missing, unknown, and human stop reasons stay interrupted.
                reason = payload.get('reason')
                if isinstance(reason,dict): reason = reason.get('type') or reason.get('kind')
                reason = ''.join(c for c in str(reason or '').lower() if c.isalnum())
                if reason in ('quota','usagelimitexceeded','ratelimitexceeded'):
                    info.update(status='failed',error_kind='quota')
                elif reason in ('login','unauthorized','authenticationerror'):
                    info.update(status='failed',error_kind='login')
                else: info.update(status='interrupted', error_kind='user_interrupt')
            elif event == 'error':
                message = str(payload.get('message', '')).lower()
                quota = any(w in message for w in ('usage limit', 'rate limit', 'usage_limit', 'quota', 'limit reached'))
                login = any(w in message for w in ('unauthorized', 'refresh token', 'authentication', '401'))
                info.update(status='failed', error_kind='quota' if quota else 'login' if login else 'other')
    if not info.get('model') or not info.get('effort') or not info.get('turn_id'):
        context=latest_context(path)
        for key in ('model','effort','model_provider','approval_policy','sandbox_policy','turn_id'):
            if info.get(key) is None and key in context:info[key]=context[key]
    return info

def resume_arguments(info):
    if not info.get('model') or not info.get('effort'): raise RuntimeError('Cannot verify the original model settings')
    policy = info.get('sandbox_policy') or {}
    sandbox = policy.get('type') if isinstance(policy, dict) else policy
    approval = info.get('approval_policy')
    if sandbox not in ('danger-full-access', 'workspace-write', 'read-only') or approval not in ('never', 'on-request', 'on-failure', 'untrusted'):
        raise RuntimeError('Cannot preserve this session permission policy')
    args = ['--no-daemon', 'resume', info['id'], '-m', info['model'], '-c', 'model_reasoning_effort="' + info['effort'] + '"', '-a', approval, '-s', sandbox]
    if sandbox == 'workspace-write':
        if policy.get('writable_roots'): raise RuntimeError('Custom writable roots need a supported resume configuration')
        if policy.get('network_access'): args += ['-c', 'sandbox_workspace_write.network_access=true']
    return args

def powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"
