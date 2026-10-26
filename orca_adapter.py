from __future__ import annotations
import json, os, pathlib, re, shutil, sqlite3, time, uuid
from accounts import identity, protected_json, read_goal, transfer_goal, codex_binary, inherit_project_trust, database_connection, plain_path, LOCALDATA, load_json, atomic_json
from local_rpc import OrcaRpc, RpcError
from transcript import read_transcript, resume_arguments, powershell_quote
from recovery_policy import proxy_session, recovery_error

def write_accepted(result):
    send = result.get('send') or result
    settlement = send.get('writeSettlement')
    return send.get('accepted') is True and (settlement is None or
        isinstance(settlement, dict) and settlement.get('outcome') == 'accepted')

def exited_pane(wait, terminal):
    return (isinstance(wait,dict) and wait.get('handle')==terminal and wait.get('condition')=='exit'
        and wait.get('satisfied') is True and wait.get('status')=='exited')

def shell_exit_confirmed(screen, sid, process=None, terminal=None, incarnation=None):
    text = '\n'.join(screen.get('tail', []))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    identifier = r'\s*'.join(re.escape(c) for c in sid)
    exit_hint = bool(re.search(r'(?:\bcodex\s+resume\s+|^Session ID:\s*)' +
        identifier + r'(?=\s|$)', text, re.M))
    if screen.get('source') != 'screen' or not exit_hint or not lines:return False
    prompt_proof = bool(re.fullmatch(r'PS .+>\s*', lines[-1])) and not screen.get('draft')
    if process is None:return prompt_proof
    if process.get('verdict') == 'unverifiable':return prompt_proof
    evidence = process.get('foregroundProcessEvidence')
    if evidence is not None:
        if isinstance(evidence,dict) and evidence.get('verdict') == 'unverifiable':return prompt_proof
        if (not isinstance(evidence,dict) or evidence.get('verdict') != 'live'
                or evidence.get('ptyId') != terminal or evidence.get('ptyIncarnationId') != incarnation
                or not isinstance(evidence.get('capturedAgeMs'),(int,float))
                or not 0 <= evidence['capturedAgeMs'] <= 2000):return False
    if process.get('hasChildProcesses') is not False:return False
    foreground = process.get('foregroundProcess')
    if not isinstance(foreground,str):return False
    name = foreground.strip().strip('"\'').replace('\\','/').rsplit('/',1)[-1].lower()
    return name in ('powershell','powershell.exe','pwsh','pwsh.exe') and not screen.get('draft')

class OrcaAdapter:
    def __init__(self, accounts, rpc=None):
        self.accounts = accounts
        self.rpc = rpc or accounts.rpc or OrcaRpc()
        try: self.bindings = load_json(LOCALDATA / 'orca-bindings.json')
        except (OSError, ValueError): self.bindings = {}
        self._catalog_cache = []
        self._catalog_checked = 0

    def selectable_sessions(self, live_rows):
        # Saved history is display-only. Recovery still requires a verified live
        # owner from discover(), so selecting an old chat cannot launch a copy.
        if time.time()-self._catalog_checked>=30:
            homes={str(r['orca_home']) for r in getattr(self.accounts,'records',[]) if r.get('orca_home')}
            homes.update(s['home'] for s in live_rows if s.get('home'))
            saved={}
            for home in homes:
                path=plain_path(home)/'state_5.sqlite'
                if not path.is_file():continue
                try:
                    with database_connection(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1) as conn:
                        conn.row_factory=sqlite3.Row
                        columns={r[1] for r in conn.execute('PRAGMA table_info(threads)')}
                        if 'id' not in columns:continue
                        fields=[k for k in ('id','title','updated_at','updated_at_ms','archived','is_archived','source','agent_path') if k in columns]
                        for raw in conn.execute('SELECT '+','.join('"'+k+'"' for k in fields)+' FROM threads LIMIT 5000'):
                            row=dict(raw)
                            if row.get('archived') or row.get('is_archived') or row.get('agent_path') not in (None,'','/root'):continue
                            try:source=json.loads(row.get('source') or '{}')
                            except (ValueError,TypeError):source={}
                            if isinstance(source,dict) and 'subagent' in source:continue
                            sid=row['id']
                            try:uuid.UUID(sid)
                            except (ValueError,TypeError,AttributeError):continue
                            updated=row.get('updated_at_ms') or (row.get('updated_at') or 0)*1000
                            if sid not in saved or updated>saved[sid]['updated_at']:
                                saved[sid]={'id':sid,'title':row.get('title') or 'Orca conversation',
                                    'status':'Open in Orca','goal_status':None,'attached':False,'updated_at':updated}
                except (OSError,sqlite3.Error):continue
            self._catalog_cache=list(saved.values());self._catalog_checked=time.time()
        rows={r['id']:dict(r) for r in self._catalog_cache}
        for row in live_rows:
            rows[row['id']]={k:row.get(k) for k in ('id','title','status','goal_status')} | {
                'attached':row.get('supported') is not False,'updated_at':time.time()*1000}
        return sorted(rows.values(),key=lambda r:(r['attached'],r['updated_at']),reverse=True)

    def discover(self):
        try:
            snapshots = self.rpc.call('session.tabs.listAll')['snapshots']
            terminals = self.rpc.call('terminal.list', {'includeVisualLayouts': False, 'requireFreshPtyLiveness': True})['terminals']
        except RpcError as exc:
            # Codex monitoring must not require an open Orca window. A live or
            # uncertain Orca process still needs an ownership check before sharing.
            if exc.code == 'unavailable' and self.rpc.application_running() is False:
                return []
            raise
        by_pty = {t.get('ptyId'): t for t in terminals}
        found = []
        for snapshot in snapshots:
            for tab in snapshot.get('tabs', []):
                agent = tab.get('agentStatus') or {}
                if agent.get('agentType') != 'codex': continue
                provider = agent.get('providerSession') or {}
                path = provider.get('transcriptPath')
                if not path or not provider.get('id'): continue
                path = str(plain_path(path))
                try:
                    info = read_transcript(path)
                    if info['id'] != provider['id']: continue
                    # A recorded managed home must actually contain this transcript.
                    home = next(p for p in pathlib.Path(path).parents if (p / 'auth.json').exists())
                    account = identity(protected_json(home / 'auth.json'))
                    goal = read_goal(home, info['id'])
                except (OSError, ValueError, StopIteration): continue
                terminal = by_pty.get(tab.get('ptyId')) or {}
                state = agent.get('state')
                if terminal.get('handle') and state in ('working','running','idle','done','error'):
                    screen=self.rpc.call('terminal.read', {'terminal':terminal['handle'],'screen':True,'limit':2500})['terminal']
                    screen_text='\n'.join(screen.get('tail',[])).lower()
                    limit_matches=list(re.finditer(r"you(?:'|’)?ve hit your usage limit|you have hit your usage limit|usage limit reached",screen_text))
                    if info['status'] != 'interrupted' and info.get('turn_id') and limit_matches and limit_matches[-1].start()>screen_text.rfind('worked for '):
                        # Screen errors do not contain turn IDs. Keep the transcript's
                        # stop authoritative and require fresh exhausted quota in the
                        # controller before this supplemental evidence can rotate.
                        info.update(status='failed',error_kind='quota',_screen_quota=True);state='error'
                waiting = state in ('waiting', 'permission', 'asking', 'approval', 'needs-input')
                if state in ('working', 'running') and info['status'] not in ('interrupted','failed'): info['status'] = 'running'
                if state in ('idle', 'done') and info['status'] == 'running': info['status'] = 'idle'
                info.update(app='orca', title=tab.get('title') or 'Codex chat', provider='codex',
                    account_key=account['key'] if account else None, home=str(home),
                    goal_status=goal.get('status') if goal else None, waiting=waiting,
                    worktree=snapshot['worktree'], tab_id=terminal.get('tabId') or tab['id'], leaf_id=tab.get('leafId'),
                    pty_id=tab.get('ptyId'), terminal=terminal.get('handle'), incarnation=terminal.get('incarnationId'),
                    connected=bool(terminal.get('connected')), supported=bool(terminal.get('handle')),
                    agent_state=state, agent_updated_at=agent.get('updatedAt'),
                    _goal={k:goal.get(k) for k in ('goal_id','status','token_budget','tokens_used','time_used_seconds','updated_at_ms')} if goal else None)
                found.append(info)
                binding = self.bindings.get(info['id'])
                if binding and binding.get('terminal') == info['terminal'] and binding.get('incarnation') == info['incarnation']:
                    current_identity = identity(protected_json(pathlib.Path(binding['home']) / 'auth.json'))
                    actual = read_transcript(binding['path'])
                    timestamp = info.get('agent_updated_at') or 0
                    source_alias = False
                    if binding.get('source_path') == info['path']:
                        try: source_alias = os.path.samefile(info['path'],binding['path'])
                        except OSError: pass
                    if current_identity and current_identity['key'] == binding['key'] and actual['id'] == info['id'] and (info['account_key']==binding['key'] or source_alias):
                        # The managed pane may retain its original provider path.
                        # A verified launch plus the exact same physical transcript
                        # binds that stale path to the explicitly attested home.
                        info.update(home=binding['home'],path=binding['path'],account_key=binding['key'])
                        for key in ('status','error_kind','turn_id','event_pos','model','effort','model_provider','approval_policy','sandbox_policy'):
                            if key in actual:info[key]=actual[key]
                        if actual['status']!='failed':info.pop('_screen_quota',None)
                        bound_goal = read_goal(binding['home'], info['id'])
                        info['goal_status'] = bound_goal.get('status') if bound_goal else None
                    elif info['account_key'] != binding['key']:
                        if time.time()-binding.get('verified_at',0)<60:
                            info.update(status='unknown',reserved_keys=[info['account_key'],binding['key']])
                        else:self.bindings.pop(info['id'], None)
        # A resumed idle TUI may not publish a provider hook until its next turn.
        # Retain only a binding attested by local /status in this exact pane.
        known = {s['id'] for s in found}
        for sid,binding in list(self.bindings.items()):
            terminal = next((t for t in terminals if t.get('handle') == binding.get('terminal')
                and t.get('incarnationId') == binding.get('incarnation')),None)
            if sid in known or not binding.get('status_verified') or not terminal:continue
            try:
                home=pathlib.Path(binding['home']);path=pathlib.Path(binding['path'])
                path.relative_to(home/'sessions')
                account=identity(protected_json(home/'auth.json'));info=read_transcript(path)
                if not account or account['key']!=binding['key'] or info['id']!=sid:continue
                screen=self.rpc.call('terminal.read',{'terminal':terminal['handle'],
                    'expectedIncarnationId':binding['incarnation'],'screen':True,'limit':3500})['terminal']
                text='\n'.join(screen.get('tail',[]))
                goal=read_goal(home,sid)
                ready=(terminal.get('connected') and screen.get('source')=='screen'
                    and 'conversation is open in another app' not in text.lower()
                    and any(marker in text for marker in ('Ask Codex','Working')))
                if ready:
                    native=self.rpc.call('terminal.agentStatus',{'terminal':terminal['handle']}).get('agentStatus') or {}
                    state=native.get('status') or native.get('state')
                    if native.get('isRunningAgent') is not True:continue
                else:
                    limited=(info.get('status')!='interrupted' and (not goal or goal.get('status') in ('active','usage_limited'))
                        and ((goal and goal.get('status')=='usage_limited')
                        or (info.get('status')=='failed' and recovery_error(info.get('error_kind')))))
                    if not limited:continue
                    wait=self.rpc.call('terminal.wait',{'terminal':terminal['handle'],
                        'expectedIncarnationId':binding['incarnation'],'for':'exit','timeoutMs':1}).get('wait')
                    if not exited_pane(wait,terminal['handle']):continue
                    state='exited';info['_exited_pane']=True
                    if goal and goal.get('status')=='usage_limited':info.update(status='failed',error_kind='quota')
                info.update(app='orca',title=terminal.get('title') or 'Codex chat',provider='codex',
                    account_key=account['key'],home=str(home),goal_status=goal.get('status') if goal else None,
                    waiting=state in ('waiting','permission','asking','approval','needs-input'),
                    worktree=terminal.get('worktreeId'),tab_id=terminal.get('tabId'),leaf_id=terminal.get('leafId'),
                    pty_id=terminal.get('ptyId'),terminal=terminal['handle'],incarnation=binding['incarnation'],
                    connected=bool(terminal.get('connected')),supported=True,agent_state=state,
                    _goal={k:goal.get(k) for k in ('goal_id','status','token_budget','tokens_used','time_used_seconds','updated_at_ms')} if goal else None)
                found.append(info)
            except (OSError,ValueError):continue
        return found

    def get(self, session_id):
        return next((s for s in self.discover() if s['id'] == session_id), None)

    def submit_local(self, terminal, incarnation, text):
        # Settle text before Enter so a redraw cannot swallow the submission.
        base = {'terminal':terminal, 'expectedIncarnationId':incarnation,
                'requireWriteSettlement':True, 'client':{'id':'session-relay', 'type':'desktop'}}
        result = self.rpc.call('terminal.send', {**base, 'text':text})
        if not write_accepted(result):
            raise RuntimeError('Orca rejected the local text write')
        time.sleep(.4)
        result = self.rpc.call('terminal.send', {**base, 'enter':True})
        if not write_accepted(result):
            raise RuntimeError('Orca did not confirm the local Enter write')

    def wait_for_shell_exit(self, terminal, incarnation, sid):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(.5)
            screen = self.rpc.call('terminal.read', {'terminal':terminal,
                'expectedIncarnationId':incarnation,'screen':True,'limit':3500})['terminal']
            try:
                process = self.rpc.call('terminal.inspectProcess', {'terminal':terminal,
                    'expectedIncarnationId':incarnation,'scanChildProcesses':True})['process']
            except RpcError as exc:
                if exc.code not in ('method_not_found','rpc_method_not_found','unsupported_method'):raise
                process = None
            if shell_exit_confirmed(screen,sid,process,terminal,incarnation):return 'shell'
            # Some managed panes run Codex as the root process. Its exit footer
            # is then the last output and there will never be a PS prompt.
            try:
                wait=self.rpc.call('terminal.wait',{'terminal':terminal,'expectedIncarnationId':incarnation,
                    'for':'exit','timeoutMs':1}).get('wait')
                if exited_pane(wait,terminal):return 'exited'
            except RpcError as exc:
                if exc.code not in ('method_not_found','rpc_method_not_found','unsupported_method'):raise
        raise RuntimeError('Codex exit was not verified in the original PowerShell pane')

    def replace_exited_pane(self, original, fresh, target, destination, command, journal):
        if not all(original.get(k) for k in ('worktree','tab_id','leaf_id')):
            raise RuntimeError('The exited pane has no verified original layout')
        wait=self.rpc.call('terminal.wait',{'terminal':fresh['terminal'],
            'expectedIncarnationId':fresh['incarnation'],'for':'exit','timeoutMs':1}).get('wait')
        if not exited_pane(wait,fresh['terminal']):raise RuntimeError('The original pane is no longer confirmed exited')
        operation=str(uuid.uuid4())
        journal('resume-process-create-intent',{'session':original['id'],'terminal':fresh['terminal'],
            'incarnation':fresh['incarnation'],'target':target['key'],'operation':operation})
        created=self.rpc.call('terminal.create',{'worktree':original['worktree'],
            'tabId':original['tab_id'],'leafId':original['leaf_id'],'title':original.get('title') or 'Resume',
            'command':command,'env':{'CODEX_HOME':str(target['orca_home']),'ORCA_CODEX_HOME':str(target['orca_home'])},
            'shell':'powershell','startupCommandDelivery':'shell-ready','presentation':'background',
            'resumeProviderSession':{'key':'session_id','id':original['id'],'transcriptPath':str(destination)},
            'launchAgent':'codex','clientMutationId':operation,'reconcileExisting':True})['terminal']
        expected_key=original['tab_id']+':'+original['leaf_id']
        if (created.get('worktreeId')!=original['worktree'] or created.get('tabId')!=original['tab_id']
                or created.get('paneKey')!=expected_key or not created.get('handle') or not created.get('incarnationId')
                or created['handle']==fresh['terminal'] or created['incarnationId']==fresh['incarnation']):
            raise RuntimeError('Orca did not confirm a replacement in the original exited pane')
        journal('resume-process-created',{'session':original['id'],'terminal':created['handle'],
            'incarnation':created['incarnationId'],'pty_id':created.get('ptyId'),
            'target':target['key'],'operation':operation})
        return dict(fresh,terminal=created['handle'],incarnation=created['incarnationId'],pty_id=created.get('ptyId'))

    def rotate(self, session_ids, target, journal, *, repair_depleted_idle=False):
        sessions = [self.get(sid) for sid in session_ids]
        if any(s and (proxy_session(s) or s['status'] == 'interrupted') for s in sessions):
            raise RuntimeError('A selected chat uses another provider or was stopped manually')
        if any(not s or s['waiting'] or s.get('is_child') or (not s.get('connected') and not s.get('_exited_pane')) for s in sessions):
            raise RuntimeError('A selected Orca chat is unavailable or waiting for you')
        if any(s.get('goal_status') not in (None, 'active', 'usage_limited') for s in sessions):
            raise RuntimeError('A selected Goal was paused, completed, or reached its own budget')
        if not target.get('orca_id'):
            # A CLI pane can use the user's normal Codex home without a saved
            # Orca profile. Reuse that exact verified home after its reset.
            homes={s['home'] for s in sessions}
            if any(s.get('account_key')!=target['key'] for s in sessions) or len(homes)!=1:
                raise RuntimeError('This destination account is not saved in Orca')
            target=dict(target,orca_home=next(iter(homes)))
        repair_ids=set()
        if repair_depleted_idle:
            for s in sessions:
                if s['status']!='idle':continue
                record=self.accounts.find(s.get('account_key'))
                if not record:raise RuntimeError('The stopped chat account cannot be verified')
                quota=self.accounts.probe(record,force=True,home=s['home'])
                if not quota.get('valid') or not any(isinstance(quota.get(k),(int,float)) and quota[k]<=2 for k in ('five_remaining','weekly_remaining')):
                    raise RuntimeError('Explicit recovery requires a freshly verified depleted account')
                repair_ids.add(s['id'])
        if any(s['id'] not in repair_ids and s['status'] != 'running' and s.get('error_kind') not in ('quota', 'login') and s.get('goal_status')!='usage_limited' for s in sessions):
            raise RuntimeError('A selected chat is no longer working or stopped by a usage/login error')
        for session in sessions: resume_arguments(session)  # Validate before stopping anything.
        target_home = pathlib.Path(target['orca_home'])
        check = identity(protected_json(target_home / 'auth.json'))
        if not check or check['key'] != target['key']: raise RuntimeError('The Orca destination login changed')
        for session in sessions: inherit_project_trust(session['home'], target_home, session['cwd'])
        journal('orca-recovery-context', {'context':{s['id']:dict(s) for s in sessions}})
        if target.get('orca_id'):
            journal('select-account', {'target': target['key']})
            self.rpc.call('accounts.selectCodexForTarget', {'accountId': target['orca_id'], 'target': {'runtime': 'host', 'wslDistro': None}})
            selected = self.rpc.call('accounts.list', {'refreshUsage': False})['codex']
            if selected.get('activeAccountId') != target['orca_id']: raise RuntimeError('Orca did not select the destination account')
        recovered = []
        for original in sessions:
            sid = original['id']
            fresh = self.get(sid)
            if not fresh or fresh['pty_id'] != original['pty_id'] or fresh['incarnation'] != original['incarnation'] or fresh['waiting']:
                raise RuntimeError('The original Orca pane changed before recovery')
            if fresh.get('goal_status') != original.get('goal_status'):
                raise RuntimeError('The Goal changed before recovery')
            if fresh.get('turn_id') != original.get('turn_id') or (fresh['id'] not in repair_ids and fresh['status'] != 'running' and fresh.get('error_kind') not in ('quota','login') and fresh.get('goal_status')!='usage_limited'):
                raise RuntimeError('The selected turn stopped or changed before recovery')
            pause_receipt = None
            stop_receipt = None
            if fresh['status'] == 'running':
                journal('interrupt', {'session': sid, 'terminal': fresh['terminal']})
                self.rpc.call('terminal.send', {'terminal': fresh['terminal'], 'expectedIncarnationId': fresh['incarnation'], 'interrupt': True,
                    'client': {'id': 'session-relay', 'type': 'desktop'}})
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    time.sleep(.5)
                    state = self.rpc.call('terminal.agentStatus', {'terminal': fresh['terminal']}).get('agentStatus') or {}
                    native = state.get('status') or state
                    native_state = native if isinstance(native, str) else native.get('state')
                    stopped = read_transcript(fresh['path'])['status'] != 'running'
                    if stopped and native_state not in ('working', 'running'): break
                else: raise RuntimeError('The running turn did not stop cleanly')
                stopped_info = read_transcript(fresh['path'])
                stop_receipt = (stopped_info.get('turn_id'), stopped_info.get('event_pos'))
            if original.get('goal_status') in ('active','usage_limited'):
                current_goal = read_goal(original['home'], sid)
                expected = original['_goal']
                if not current_goal or current_goal['status'] != original['goal_status'] or current_goal['goal_id'] != expected['goal_id'] or current_goal['token_budget'] != expected['token_budget']:
                    raise RuntimeError('The Goal was paused or changed before Codex exited')
                stamp = max(int(time.time()*1000), current_goal['updated_at_ms']+1)
                journal('pause-goal', {'session':sid, 'goal_id':current_goal['goal_id'], 'updated_at_ms':stamp})
                with database_connection(pathlib.Path(original['home'])/'goals_1.sqlite') as conn:
                    changed=conn.execute("UPDATE thread_goals SET status='paused', updated_at_ms=? WHERE thread_id=? AND goal_id=? AND status=? AND updated_at_ms=?",(stamp,sid,current_goal['goal_id'],current_goal['status'],current_goal['updated_at_ms'])).rowcount
                    if changed != 1: raise RuntimeError('The Goal changed while recovery was preparing')
                pause_receipt=read_goal(original['home'],sid)
            # Keep the shell and pane alive. Hibernating its PTY lets Orca's renderer
            # wake the old process automatically, which can race a replacement.
            journal('quit-codex', {'session': sid, 'terminal': fresh['terminal']})
            if fresh.get('_exited_pane'):
                exit_kind='exited'
            else:
                screen = self.rpc.call('terminal.read', {'terminal':fresh['terminal'], 'screen':True, 'limit':2000})['terminal']
                reminder = '\n'.join(screen.get('tail', []))
                if 'Approaching rate limits' in reminder and 'Keep current model' in reminder and 'esc back' in reminder:
                    self.rpc.call('terminal.send', {'terminal':fresh['terminal'], 'expectedIncarnationId':fresh['incarnation'],
                        'text':'\x1b', 'requireWriteSettlement':True, 'client':{'id':'session-relay','type':'desktop'}})
                    time.sleep(.5)
                    screen = self.rpc.call('terminal.read', {'terminal':fresh['terminal'], 'screen':True, 'limit':2000})['terminal']
                    if 'esc back' in '\n'.join(screen.get('tail', [])):
                        raise RuntimeError('Could not dismiss the quota reminder without changing the model')
                self.submit_local(fresh['terminal'], fresh['incarnation'], '/quit')
                exit_kind=self.wait_for_shell_exit(fresh['terminal'],fresh['incarnation'],sid)
            journal('orca-exit-confirmed', {'session':sid,'terminal':fresh['terminal'],
                'incarnation':fresh['incarnation'],'turn':original.get('turn_id'),
                'stop_receipt':stop_receipt,'pause_receipt':pause_receipt,'exit_kind':exit_kind})
            # Update after stop: the last tool and Goal counters must have settled.
            source_path = pathlib.Path(original['path'])
            stopped_info = read_transcript(source_path)
            if stopped_info['status'] == 'interrupted' and stop_receipt != (stopped_info.get('turn_id'),stopped_info.get('event_pos')):
                raise RuntimeError('The chat was stopped manually during recovery')
            relative = source_path.relative_to(pathlib.Path(original['home']) / 'sessions')
            destination = target_home / 'sessions' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            journal('preserve-session', {'session': sid, 'target': target['key']})
            if destination.exists():
                if read_transcript(destination)['id'] != sid: raise RuntimeError('Conflicting destination transcript')
                if not os.path.samefile(source_path, destination):
                    if destination.read_bytes() != source_path.read_bytes(): raise RuntimeError('The destination has a different version of this chat')
            else:
                try: os.link(source_path, destination)
                except OSError: shutil.copy2(source_path, destination)
            transfer_goal(original['home'], target_home, sid)
            goal = read_goal(target_home, sid)
            if original.get('goal_status') in ('active','usage_limited'):
                if not goal or goal != pause_receipt:
                    raise RuntimeError('The Goal changed during the account handoff')
                # /quit can pause an active Goal. This was our own confirmed quit;
                # restore only its status, preserving every accounting field.
                if goal['status'] == 'paused':
                    with database_connection(target_home / 'goals_1.sqlite') as conn:
                        conn.execute("UPDATE thread_goals SET status='active' WHERE thread_id=? AND goal_id=? AND status='paused'", (sid, goal['goal_id']))
                    goal = read_goal(target_home, sid)
            args = resume_arguments(original)
            # Using a shell command and an explicit home avoids an implicit default-account launch.
            command = '$env:CODEX_HOME=' + powershell_quote(target_home) + '; $env:ORCA_CODEX_HOME=' + powershell_quote(target_home) + '; & ' + powershell_quote(codex_binary()) + ' ' + ' '.join(powershell_quote(a) for a in args)
            if exit_kind=='exited':
                fresh=self.replace_exited_pane(original,fresh,target,destination,command,journal)
            handle = fresh['terminal']
            if exit_kind=='shell':
                journal('resume-process', {'session': sid, 'terminal': handle, 'target': target['key']})
                self.submit_local(handle, fresh['incarnation'], command)
            deadline = time.monotonic() + 35
            earliest_ready = time.monotonic() + 2
            replacement = None
            while time.monotonic() < deadline:
                time.sleep(.7)
                running = self.rpc.call('terminal.isRunningAgent', {'terminal': handle}).get('isRunningAgent')
                screen = self.rpc.call('terminal.read', {'terminal': handle, 'screen': True, 'limit': 6000})['terminal']
                text = '\n'.join(screen.get('tail', []))
                if 'conversation is open in another app' in text.lower(): raise RuntimeError('The original chat is already running in another process')
                if time.monotonic() >= earliest_ready and running and ('Ask Codex' in text or 'Working' in text): break
            else: raise RuntimeError('Codex did not become ready in the same pane')
            # /status is local UI information; it does not start a model turn.
            if read_transcript(destination)['status'] != 'running':
                journal('verify-account', {'session': sid, 'target': target['key']})
                self.submit_local(handle, fresh['incarnation'], '/status')
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    time.sleep(.5)
                    text = '\n'.join(self.rpc.call('terminal.read', {'terminal': handle, 'screen': True, 'limit': 8000})['terminal'].get('tail', []))
                    shown_home = str(target_home).replace(str(pathlib.Path.home()), '~', 1)
                    compact = re.sub(r'\s+', '', text).lower().replace('/', '\\')
                    home_proof = re.sub(r'\s+', '', shown_home).lower().replace('/', '\\') + '\\agents.md'
                    actual = identity(protected_json(target_home / 'auth.json'))
                    if sid in text and actual and actual['key'] == target['key'] and (target['label'].lower() in text.lower() or home_proof in compact): break
                else: raise RuntimeError('Could not verify the resumed conversation and account in Codex status')
            else:
                verified = self.get(sid)
                if not verified or verified['terminal'] != handle or verified['account_key'] != target['key']:
                    raise RuntimeError('Could not verify the account of the automatically resumed Goal')
            observed = self.rpc.call('terminal.agentStatus',{'terminal':handle}).get('agentStatus') or {}
            self.bindings[sid] = {'terminal': handle, 'incarnation': fresh['incarnation'], 'verified_at':time.time(), 'observed_at':observed.get('updatedAt') or 0,
                'key': target['key'], 'home': str(target_home), 'path': str(destination),'source_path':original['path'],
                'status_verified':True}
            atomic_json(LOCALDATA / 'orca-bindings.json', self.bindings)
            replacement = read_transcript(destination)
            if replacement['status'] == 'interrupted' and stop_receipt != (replacement.get('turn_id'),replacement.get('event_pos')):
                raise RuntimeError('The resumed chat was stopped manually')
            current_goal = read_goal(target_home, sid)
            if not goal and current_goal:
                raise RuntimeError('A new Goal was added during recovery')
            if goal and (not current_goal or current_goal['goal_id'] != goal['goal_id'] or current_goal['status'] not in ('active','usage_limited')):
                raise RuntimeError('The Goal changed before continuation')
            # Codex may automatically continue an active Goal on resume. Never add a duplicate turn.
            if replacement['status'] != 'running':
                observed = self.rpc.call('terminal.agentStatus',{'terminal':handle}).get('agentStatus') or {}
                observed_status = observed.get('status') or observed
                observed_state = observed_status if isinstance(observed_status,str) else observed_status.get('state')
                if observed_state in ('waiting','permission','asking','approval','needs-input'):
                    raise RuntimeError('The resumed chat is waiting for user input or approval')
                latest = read_transcript(destination)
                if latest.get('event_pos') != replacement.get('event_pos'):
                    raise RuntimeError('The resumed turn changed before continuation')
                receipt={'session':sid,'terminal':handle,'incarnation':fresh['incarnation'],
                    'turn':replacement.get('turn_id'),'goal':bool(goal)}
                journal('send-continuation', receipt)
                result = self.rpc.call('terminal.send', {'terminal': handle, 'expectedIncarnationId': fresh['incarnation'],
                    'text': '/goal resume' if goal else 'continue', 'enter': True, 'agentPrompt': True,
                    'requireWriteSettlement': True, 'waitSubmitMs': 5000,
                    'client': {'id': 'session-relay', 'type': 'desktop'}},
                    envelope={'orchestrationRequestId': str(uuid.uuid4())})
                if not write_accepted(result): raise RuntimeError('Orca did not confirm the continuation')
                journal('continuation-accepted',receipt)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    time.sleep(.5)
                    current = read_transcript(destination)
                    if current.get('turn_id') and current['turn_id'] != replacement.get('turn_id'):
                        if current['status'] in ('running', 'idle'): break
                        if current['status'] == 'failed': raise RuntimeError('The resumed turn failed')
                else: raise RuntimeError('Continuation was submitted but no new turn was confirmed')
            journal('session-recovered', {'session': sid, 'target': target['key']})
            recovered.append(sid)
        return recovered

    def close(self): pass
