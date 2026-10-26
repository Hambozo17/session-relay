from __future__ import annotations
import json, pathlib

_SANDBOX_MODES = ('danger-full-access', 'workspace-write', 'read-only')
_BUILTIN_PROFILES = {
    ':danger-full-access': 'danger-full-access',
    ':workspace': 'workspace-write',
    ':read-only': 'read-only',
}
_WORKSPACE_FIELDS = ('network_access', 'exclude_tmpdir_env_var', 'exclude_slash_tmp')
_WORKSPACE_SUBPATHS = ('.git', '.agents', '.codex', '.aws')

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
    info = {'path': str(path), 'status': 'unknown', 'error_kind': None, '_context_turn_id': None}
    context_seen = False
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
                context_seen = True
                info.update({k: payload.get(k) for k in ('model', 'effort', 'model_provider', 'approval_policy',
                    'sandbox_policy', 'permission_profile', 'active_permission_profile')})
                info['_context_turn_id'] = payload.get('turn_id')
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
        for key in ('model','effort','model_provider','approval_policy','sandbox_policy',
                'permission_profile','active_permission_profile','turn_id'):
            if info.get(key) is None and key in context:info[key]=context[key]
        if not context_seen:info['_context_turn_id']=context.get('turn_id')
    return info

def _policy_error():
    raise RuntimeError('Cannot preserve this session permission policy')


def _sandbox_policy(info):
    policy = info.get('sandbox_policy')
    if isinstance(policy, str):
        policy = {'type': policy}
    if not isinstance(policy, dict):_policy_error()
    sandbox = policy.get('type')
    if sandbox not in _SANDBOX_MODES:_policy_error()
    if sandbox == 'workspace-write':
        if set(policy) - {'type', 'writable_roots', *_WORKSPACE_FIELDS}:_policy_error()
        roots = policy.get('writable_roots', [])
        if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):_policy_error()
        if roots:raise RuntimeError('Custom writable roots need a supported resume configuration')
        values={field:policy.get(field, False) for field in _WORKSPACE_FIELDS}
        if any(not isinstance(value, bool) for value in values.values()):_policy_error()
        return {'type':sandbox,'writable_roots':[],**values}
    if set(policy) != {'type'}:_policy_error()
    return {'type':sandbox}


def _active_profile(info, sandbox):
    active=info.get('active_permission_profile')
    if active is None:return sandbox
    if not isinstance(active, dict) or set(active) - {'id', 'extends'}:_policy_error()
    profile_id=active.get('id')
    if not isinstance(profile_id, str) or active.get('extends') is not None:_policy_error()
    if _BUILTIN_PROFILES.get(profile_id) != sandbox:_policy_error()
    return sandbox


def _profile_entries(profile, sandbox_policy):
    sandbox=sandbox_policy['type']
    if set(profile) != {'type', 'file_system', 'network'}:_policy_error()
    if profile.get('type') != 'managed':_policy_error()
    expected_network='enabled' if sandbox_policy.get('network_access') is True else 'restricted'
    if profile.get('network') != expected_network:_policy_error()
    filesystem=profile.get('file_system')
    if not isinstance(filesystem,dict) or set(filesystem) - {'type','entries','glob_scan_max_depth'}:_policy_error()
    if filesystem.get('type') != 'restricted' or filesystem.get('glob_scan_max_depth') not in (None,):_policy_error()
    entries=filesystem.get('entries')
    if not isinstance(entries,list):_policy_error()

    expected={('root',None,'read',None)}
    if sandbox == 'workspace-write':
        expected.add(('project_roots',None,'write',None))
        if not sandbox_policy['exclude_slash_tmp']:
            expected.add(('slash_tmp',None,'write',None))
        if not sandbox_policy['exclude_tmpdir_env_var']:
            expected.add(('tmpdir',None,'write',None))
        expected.update(('project_roots',subpath,'read','skip') for subpath in _WORKSPACE_SUBPATHS)

    actual=[]
    for entry in entries:
        if not isinstance(entry,dict) or set(entry) - {'path','access','missing_path_behavior'}:_policy_error()
        path=entry.get('path')
        if (not isinstance(path,dict) or set(path) != {'type','value'}
                or path.get('type') != 'special' or not isinstance(path.get('value'),dict)):_policy_error()
        value=path['value']
        if set(value) - {'kind','subpath'}:_policy_error()
        kind=value.get('kind');subpath=value.get('subpath')
        if subpath is not None and not isinstance(subpath,str):_policy_error()
        access=entry.get('access')
        missing=entry.get('missing_path_behavior')
        if (not isinstance(kind,str) or access not in ('read','write')
                or missing not in (None,'skip')):_policy_error()
        actual.append((kind,subpath,access,missing))
    if len(actual) != len(set(actual)) or set(actual) != expected:_policy_error()
    return sandbox


def normalize_permission_policy(info):
    if not isinstance(info, dict):_policy_error()
    approval=info.get('approval_policy')
    if not isinstance(approval,str) or approval not in ('never','on-request'):_policy_error()
    sandbox_policy=_sandbox_policy(info)
    sandbox=sandbox_policy['type']
    _active_profile(info,sandbox)
    profile=info.get('permission_profile')
    if profile is not None:
        if not isinstance(profile,dict) or 'type' not in profile:_policy_error()
        if profile.get('type') == 'disabled':
            if set(profile) != {'type'} or sandbox != 'danger-full-access':_policy_error()
        else:
            if sandbox == 'danger-full-access':_policy_error()
            _profile_entries(profile,sandbox_policy)
    return {
        'approval_policy':approval,
        'sandbox_policy':sandbox_policy,
        # A missing legacy profile and its recognized built-in name have the
        # same semantics once the legacy sandbox mode is made explicit.
        'active_permission_profile':sandbox,
        'permission_profile':sandbox,
    }


def permission_policy_equal(left, right):
    try:return normalize_permission_policy(left) == normalize_permission_policy(right)
    except RuntimeError:return False


def resume_arguments(info):
    if (not isinstance(info,dict) or not isinstance(info.get('id'),str)
            or not isinstance(info.get('model'),str) or not info['model'].strip()
            or not isinstance(info.get('effort'),str) or not info['effort'].strip()):
        raise RuntimeError('Cannot verify the original model settings')
    normalized=normalize_permission_policy(info)
    policy=normalized['sandbox_policy']
    sandbox=policy['type'];approval=normalized['approval_policy']
    args = ['--no-daemon', 'resume', info['id'], '-m', info['model'], '-c', 'model_reasoning_effort="' + info['effort'] + '"', '-a', approval, '-s', sandbox]
    if sandbox == 'workspace-write':
        # Override destination config, including additions and permissive
        # defaults, with every value from the original verified policy.
        args += [
            '-c', 'sandbox_workspace_write.network_access=' + str(policy['network_access']).lower(),
            '-c', 'sandbox_workspace_write.writable_roots=[]',
            '-c', 'sandbox_workspace_write.exclude_tmpdir_env_var=' + str(policy['exclude_tmpdir_env_var']).lower(),
            '-c', 'sandbox_workspace_write.exclude_slash_tmp=' + str(policy['exclude_slash_tmp']).lower(),
        ]
    return args

def powershell_quote(value):
    return "'" + str(value).replace("'", "''") + "'"
