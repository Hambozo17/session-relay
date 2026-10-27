from __future__ import annotations
import ctypes, hashlib, json, math, os, pathlib, threading, time, traceback, uuid
from concurrent.futures import ThreadPoolExecutor
from accounts import AccountManager, LOCALDATA, REAL_HOME, ACCOUNT_REFRESH_SECONDS, atomic_json, load_json, normalize_threshold
from orca_adapter import OrcaAdapter
from recovery_policy import recovery_error, proxy_session
from transcript import permission_policy_equal

CONFIG = LOCALDATA / 'config.json'
STATUS = LOCALDATA / 'status.json'
JOURNAL = LOCALDATA / 'journal.json'
VERSION = '1.0.21'

def config():
    try: return load_json(CONFIG)
    except (OSError, ValueError): return {'threshold': 2, 'orca': {'enabled': False, 'sessions': []}, 'codex': {'enabled': False, 'sessions': []}}

def available(quota, threshold):
    above = lambda field: isinstance(quota.get(field), (int, float)) and quota[field] > threshold
    return bool(quota.get('valid') and str(quota.get('plan', '')).lower() != 'free' and
        quota.get('ordinary_allowed', True) and above('weekly_remaining') and
        (quota.get('weekly_only') or above('five_remaining')))

def weekly_fallback(quota):
    return bool(quota.get('weekly_only') or quota.get('fallback_only'))

def can_return_from_weekly(session, quota):
    return bool(weekly_fallback(quota) and session.get('status') == 'running' and
        not proxy_session(session) and not session.get('waiting') and not session.get('is_child') and
        session.get('goal_status') in (None, 'active'))

def destination_needs_check(quota, threshold):
    if str(quota.get('plan', '')).lower() == 'free': return False
    if available(quota, threshold): return True
    if not quota.get('valid'): return time.time()-(quota.get('updated_at') or 0)>=ACCOUNT_REFRESH_SECONDS
    checked=quota.get('updated_at') or 0
    now=time.time()
    # Recheck known depleted accounts when their window resets. A reset time
    # already checked by the server never causes a repeated probe every cycle.
    return now-checked>=ACCOUNT_REFRESH_SECONDS or any(isinstance(quota.get(field),(int,float)) and
        checked<quota[field]<=now for field in ('reset_at','weekly_reset_at'))

def allowance_reset_since(before, quota):
    before = before if isinstance(before,dict) else {}
    generation = quota.get('_allowance_generation', quota.get('allowance_generation', 0))
    old_generation = before.get('allowance_generation', before.get('_allowance_generation', 0))
    if (isinstance(generation,int) and not isinstance(generation,bool) and
            isinstance(old_generation,int) and not isinstance(old_generation,bool) and generation > old_generation):
        return True
    return any(isinstance(quota.get(k),(int,float)) and isinstance(before.get(k),(int,float))
        and quota[k]>before[k] and time.time()>=before[k] for k in ('reset_at','weekly_reset_at'))

def same_account_retry_allowed(app, sessions, quota, attempts):
    # One verified retry per allowance window. A newly failed continuation must
    # wait for another account or a later reset, even if the quota display lags.
    if not sessions or any(not (s.get('status')=='failed' and recovery_error(s.get('error_kind'))
            or s.get('status') in ('failed','idle') and s.get('goal_status')=='usage_limited')
            or s.get('waiting') or s.get('is_child') or proxy_session(s)
            or s.get('goal_status') not in (None,'active','usage_limited') for s in sessions):return False
    ids={s['id'] for s in sessions};key=sessions[0].get('account_key')
    for op in attempts:
        if op.get('app')!=app or op.get('target')!=key or not ids.intersection(op.get('sessions',[])):continue
        matched=ids.intersection(op.get('sessions',[]))
        receipts=[]
        if op.get('state')=='reconciled-after-quota-failure':
            receipts.append(op.get('target_quota') or {})
        if op.get('state')=='complete':
            failed=op.get('quota_failed_sessions',{})
            receipts.extend((failed.get(sid,{}).get('target_quota') or op.get('target_quota') or {})
                for sid in matched)
        if not receipts:continue  # A completed target is not banned until a verified later failure.
        if any(not allowance_reset_since(before,quota) for before in receipts):return False
    return True


def in_place_recovery_eligible(sessions):
    quota_kinds={'quota','usagelimitexceeded','ratelimitexceeded'}
    return bool(sessions) and all(
        s.get('status')=='failed'
        and ''.join(c for c in str(s.get('error_kind') or '').lower() if c.isalnum()) in quota_kinds
        and s.get('goal_status') in (None,'active')
        and not s.get('waiting') and not s.get('is_child') and not proxy_session(s)
        and not s.get('_goal_deferred') and not s.get('_goal_mismatch') and not s.get('_ephemeral')
        for s in sessions)


def same_account_restart_eligible(sessions):
    if not sessions:return False
    quota_kinds={'quota','usagelimitexceeded','ratelimitexceeded'}
    login_kinds={'login','unauthorized','authenticationerror'}
    needs_restart=False
    for session in sessions:
        if (session.get('waiting') or session.get('is_child') or proxy_session(session)
                or session.get('_goal_deferred') or session.get('_goal_mismatch') or session.get('_ephemeral')):
            return False
        kind=''.join(c for c in str(session.get('error_kind') or '').lower() if c.isalnum())
        goal=session.get('goal_status')
        if goal=='usage_limited' and session.get('status') in ('failed','idle'):
            needs_restart=True
            continue
        if (session.get('status')=='failed' and kind in quota_kinds|login_kinds
                and goal in (None,'active')):
            needs_restart=True
            continue
        return False
    return needs_restart

def orca_resumed_context_verified(operation, session):
    before=operation.get('orca_recovery_context',{}).get(session.get('id'))
    if not isinstance(before,dict):return False
    turn=session.get('turn_id')
    old_turn=before.get('turn_id') or operation.get('originals',{}).get(session.get('id'),{}).get('turn')
    return bool(old_turn and turn and turn!=old_turn and session.get('_context_turn_id')==turn
        and before.get('model') and before.get('effort')
        and session.get('model')==before['model'] and session.get('effort')==before['effort']
        and permission_policy_equal(before,session))


def orca_continuation_failed(operation, sessions):
    # An accepted send plus a different, verifiably failed turn is a settled
    # outcome, not an uncertain submission. Re-enter normal account selection.
    if operation.get('phase')!='continuation-accepted':return False
    metadata=operation.get('metadata',{});sid=metadata.get('session')
    ordered=operation.get('sessions',[])
    if sid not in ordered:return False
    position=ordered.index(sid)
    if not set(ordered[:position])<=set(operation.get('recovered_sessions',[])):return False
    rows={s['id']:s for s in sessions}
    contexts=operation.get('orca_recovery_context',{})
    # Later siblings have not entered the mutation loop. Verify their original
    # owners/settings/turns, then let normal policy decide whether to recover.
    for sibling in ordered[position+1:]:
        row=rows.get(sibling);before=contexts.get(sibling)
        if not row or not before or any(row.get(k)!=before.get(k) for k in
                ('turn_id','terminal','incarnation','account_key','home','cwd','model','effort',
                 'model_provider','approval_policy','sandbox_policy')):return False
    row=next((s for s in sessions if s['id']==sid),None)
    before=operation.get('orca_recovery_context',{}).get(sid)
    if (not row or not before or row.get('status')!='failed' or not recovery_error(row.get('error_kind'))
            or not row.get('turn_id') or row['turn_id']==metadata.get('turn')
            or row.get('account_key')!=operation.get('target') or not row.get('connected')
            or row.get('terminal')!=metadata.get('terminal') or row.get('incarnation')!=metadata.get('incarnation')
            or row.get('waiting') or row.get('is_child') or proxy_session(row)
            or row.get('goal_status') not in (None,'active','usage_limited')):return False
    for field in ('cwd','model','effort','model_provider'):
        if row.get(field)!=before.get(field):return False
    if not orca_resumed_context_verified(operation,row):return False
    old_goal=before.get('_goal');new_goal=row.get('_goal')
    if old_goal is None or new_goal is None:return old_goal is None and new_goal is None
    return (all(old_goal.get(k)==new_goal.get(k) for k in ('goal_id','token_budget')) and
        all(isinstance(new_goal.get(k),(int,float)) and new_goal[k]>=old_goal.get(k,0)
            for k in ('tokens_used','time_used_seconds')))

def allowance_wait_message(quotas):
    now=time.time()
    resets=[q[k] for q in quotas for k in ('reset_at','weekly_reset_at')
            if isinstance(q.get(k),(int,float)) and q[k]>now]
    if resets:
        stamp=time.strftime('%H:%M',time.localtime(min(resets)))
        return f'Waiting for allowance reset (next {stamp}); selected chats stay queued; checking every minute'
    return 'Waiting for an available account or allowance reset; selected chats stay queued; checking every minute'

def quota_wait_identity(session):
    fields=('id','turn_id','account_key','cwd','model','effort','model_provider','approval_policy',
        'sandbox_policy','_settings_fingerprint')
    return hashlib.sha256(json.dumps({k:session.get(k) for k in fields},sort_keys=True).encode()).hexdigest()

def verified_screen_wait(session, receipt):
    return bool(receipt and receipt==quota_wait_identity(session) and session.get('_screen_quota')
        and session.get('status')=='failed' and recovery_error(session.get('error_kind'))
        and not session.get('waiting') and not session.get('is_child') and not proxy_session(session)
        and session.get('goal_status') in (None,'active','usage_limited'))

def needs_switch(session, quota, threshold):
    # An OpenAI login does not control a proxy provider's quota.
    if proxy_session(session): return False
    if session.get('status') == 'interrupted': return False
    if session.get('waiting') or session.get('is_child') or session.get('goal_status') not in (None, 'active', 'usage_limited'): return False
    # A user can resume a turn while the persisted Goal still says limited.
    # Keep that live turn working until fresh quota or a turn failure needs recovery.
    if session.get('goal_status') == 'usage_limited' and session.get('status') != 'running': return True
    if session.get('status') == 'failed' and recovery_error(session.get('error_kind')):
        if session.get('_screen_quota'):
            return any(isinstance(quota.get(k), (int, float)) and quota[k] <= threshold for k in ('five_remaining', 'weekly_remaining'))
        return True
    if session.get('status') != 'running': return False
    return any(isinstance(quota.get(k), (int, float)) and quota[k] <= threshold for k in ('five_remaining', 'weekly_remaining'))

def choose_destination(accounts, app, outgoing, occupied, uncertain, threshold, five_hour_only=False):
    records=[r for r in accounts.records if r['key']!=outgoing and r['key'] not in uncertain
        and (app!='orca' or r.get('orca_id'))]
    # Exhaust the five-hour tier, including its last shared account, before
    # considering any weekly fallback. Each app still owns its own handoff.
    for fallback in ((False,) if five_hour_only else (False, True)):
        for sharing in (False,True):
            candidates=[r for r in records if (r['key'] in occupied)==sharing
                and destination_needs_check(accounts.quotas.get(r['key'],{}),threshold)
                and weekly_fallback(accounts.quotas.get(r['key'],{})) == fallback]
            candidates.sort(key=lambda r:(accounts.quotas.get(r['key'],{}).get('five_remaining') or 0,
                accounts.quotas.get(r['key'],{}).get('weekly_remaining') or 0),reverse=True)
            for record in candidates:
                home=record.get('orca_home') if app=='orca' else record['home']
                quota=(accounts.probe_desktop(record,force=True) if app=='codex' and hasattr(accounts,'probe_desktop')
                    else accounts.probe(record,force=True,home=home))
                # A fresh response may move an account to the other tier.
                accounts.quotas[record['key']] = quota
                if available(quota,threshold) and weekly_fallback(quota) == fallback:return record,sharing
    return None,False

def confirmed_interruptions(operation):
    saved=operation.get('interruption_receipts',{})
    if operation.get('phase') == 'desktop-close-intent':
        saved=operation.get('metadata',{}).get('originals',{})
    result=[dict(value,session=sid,account=operation.get('outgoing')) for sid,value in saved.items()
            if value.get('relay_interrupted') and value.get('goal') in (None,'active')]
    if operation.get('phase')=='desktop-interrupt-confirmed':
        latest=operation.get('metadata',{})
        if latest.get('session') not in {r.get('session') for r in result}:result.append(latest)
    return result

def quota_failure_before_handoff(session, operations):
    kind=''.join(c for c in str(session.get('error_kind') or '').lower() if c.isalnum())
    started=session.get('_turn_started_at_ms')
    if (session.get('status')!='failed' or session.get('goal_status') not in (None,'active') or
        kind not in ('quota','usagelimitexceeded','ratelimitexceeded') or
        not isinstance(started,(int,float)) or started<=0):return False
    for op in operations:
        if (op.get('app')!='codex' or op.get('state')!='complete'
                or op.get('target')!=session.get('account_key') or op.get('outgoing')==op.get('target')):continue
        before=op.get('originals',{}).get(session.get('id'),{}) or op.get('recovery_context',{}).get(session.get('id'),{})
        if before.get('turn') and before['turn']!=session.get('turn_id'):continue
        stamp=op.get('started_at',op.get('updated_at'))
        if isinstance(stamp,(int,float)) and stamp*1000>started:return True
    return False


def quota_failure_after_handoff(session, operation):
    """Identify a later native quota/login failure on a completed account attempt."""
    app=operation.get('app')
    idle_usage_goal=(app=='codex' and session.get('status')=='idle'
        and session.get('goal_status')=='usage_limited')
    if (app not in ('codex','orca') or operation.get('state')!='complete'
            or session.get('id') not in operation.get('sessions',[])
            or session.get('provider') not in (None,'codex')
            or (not idle_usage_goal and (session.get('status')!='failed' or not recovery_error(session.get('error_kind'))))
            or session.get('account_key')!=operation.get('target')
            or session.get('waiting') or session.get('is_child') or proxy_session(session)
            or session.get('goal_status') not in (None,'active','usage_limited')
            or not session.get('turn_id')):
        return None
    if idle_usage_goal:
        kind=''.join(c for c in str(session.get('error_kind') or '').lower() if c.isalnum())
        if (session.get('provider')!='codex' or kind not in ('','quota','usagelimitexceeded','ratelimitexceeded')
                or session.get('_goal_deferred') or session.get('_goal_mismatch') or session.get('_ephemeral')):
            return None
    sid=session['id']
    if app=='orca' and session.get('_screen_quota'):
        return None  # Supplemental screen text has no native turn-bound error receipt.
    contexts=(operation.get('recovery_context') or {}) if app=='codex' else (operation.get('orca_recovery_context') or {})
    before=contexts.get(sid) or (operation.get('originals') or {}).get(sid) or {}
    old_turn=before.get('turn') or before.get('turn_id')
    if not old_turn or session.get('turn_id')==old_turn:
        return None
    if app=='codex':
        settings=before.get('settings') or before.get('_settings_fingerprint')
        if not settings or session.get('_settings_fingerprint')!=settings or not session.get('_owner'):
            return None
        if idle_usage_goal:
            before_goal=before.get('goal_record')
            after_goal=session.get('_goal_record')
            if (not isinstance(before_goal,dict) or not isinstance(after_goal,dict)
                    or after_goal.get('status')!='usage_limited'
                    or before_goal.get('deferred') or after_goal.get('deferred')):
                return None
            from desktop_adapter import DesktopAdapter
            if not all(DesktopAdapter._goal_accounting_valid(goal) for goal in (before_goal,after_goal)):
                return None
            comparable_goal=dict(before_goal)
            if comparable_goal.get('status')=='active':
                comparable_goal['status']='usage_limited'
            if not DesktopAdapter._goal_progress_preserved(comparable_goal,after_goal):
                return None
        pane_receipt={}
    else:
        fields=('cwd','model','effort','model_provider','approval_policy','sandbox_policy')
        if (any(field not in before for field in fields)
                or any(session.get(field)!=before.get(field) for field in fields[:4])
                or not permission_policy_equal(before,session)):
            return None
        target_pane=(operation.get('orca_target_panes') or {}).get(sid) or {}
        expected_terminal=target_pane.get('terminal') or before.get('terminal')
        expected_incarnation=target_pane.get('incarnation') or before.get('incarnation')
        expected_pty=(target_pane.get('pty_id') if 'pty_id' in target_pane else before.get('pty_id'))
        if (session.get('connected') is not True or not expected_terminal or not expected_incarnation
                or session.get('terminal')!=expected_terminal or session.get('incarnation')!=expected_incarnation
                or not session.get('pty_id') or (expected_pty and session.get('pty_id')!=expected_pty)
                or session.get('provider')!='codex' or session.get('account_key')!=operation.get('target')
                or target_pane.get('target',operation.get('target'))!=operation.get('target')):
            return None
        settings={field:before.get(field) for field in fields}
        pane_receipt={key:session.get(key) for key in ('terminal','incarnation','pty_id','account_key')}
    started=session.get('_turn_started_at_ms')
    handoff_at=operation.get('started_at',operation.get('updated_at'))
    if idle_usage_goal and (not isinstance(started,(int,float)) or isinstance(started,bool)
            or not math.isfinite(started) or not isinstance(handoff_at,(int,float))
            or isinstance(handoff_at,bool) or not math.isfinite(handoff_at)
            or started<=handoff_at*1000):
        return None
    if (isinstance(started,(int,float)) and isinstance(handoff_at,(int,float))
            and started<=handoff_at*1000):
        return None
    existing=operation.get('quota_failed_sessions',{}).get(sid)
    if existing and existing.get('turn')==session['turn_id']:
        return None
    return {
        'turn':session['turn_id'],
        'error_kind':session.get('error_kind') or ('quota' if idle_usage_goal else None),
        'settings':settings,
        'pane':pane_receipt,
        'turn_started_at_ms':started,
        'failed_at':time.time(),
        'target_quota':dict(operation.get('target_quota') or {}),
    }


def quota_failed_target_keys(app, sessions, attempts, quotas):
    ids={s.get('id') for s in sessions}
    blocked=set()
    for operation in attempts:
        target=operation.get('target')
        if operation.get('app')!=app or not target:continue
        if operation.get('state')=='reconciled-after-quota-failure':
            if ids.intersection(operation.get('sessions',[])) and not allowance_reset_since(
                    operation.get('target_quota') or {},quotas.get(target,{})):
                blocked.add(target)
        if operation.get('state')!='complete':continue
        failed=operation.get('quota_failed_sessions',{})
        for sid in ids.intersection(operation.get('sessions',[])):
            receipt=failed.get(sid)
            if receipt and not allowance_reset_since(receipt.get('target_quota') or
                    operation.get('target_quota') or {},quotas.get(target,{})):
                blocked.add(target)
    return blocked

def desktop_pause_superseded_by_quota(operation, sessions, current_key):
    # A pause attempt targets one exact turn. If a later turn has already failed
    # on quota, the old pause cannot authorize a continuation or affect that
    # newer turn. Release only the old pre-restart operation, then reassess.
    if operation.get('phase')!='desktop-interrupt-intent' or current_key!=operation.get('outgoing'):return False
    metadata=operation.get('metadata',{})
    row=next((s for s in sessions if s['id']==metadata.get('session')),None)
    if (not row or row.get('account_key')!=current_key or not row.get('_owner') or row.get('waiting')
        or row.get('is_child') or row.get('status')!='failed' or not recovery_error(row.get('error_kind'))
        or row.get('goal_status') not in (None,'active','usage_limited') or not row.get('turn_id')
        or row['turn_id']==metadata.get('turn') or row.get('_settings_fingerprint')!=metadata.get('settings')):return False
    if metadata.get('goal_record'):
        from desktop_adapter import DesktopAdapter
        old=dict(metadata['goal_record']);new=row.get('_goal_record')
        if new and new.get('status')=='usage_limited' and old.get('status')=='active':old['status']='usage_limited'
        if not DesktopAdapter._goal_progress_preserved(old,new):return False
    return True

def desktop_recovery_confirmed(operation, sessions, current_key):
    if current_key != operation.get('target'):return False
    contexts=operation.get('recovery_context',{})
    rows={s['id']:s for s in sessions}
    if not operation.get('sessions'):return False
    from desktop_adapter import DesktopAdapter
    for sid in operation['sessions']:
        row=rows.get(sid);before=contexts.get(sid)
        if not row or not before or row.get('account_key')!=current_key or not row.get('_owner') or row.get('waiting'):
            return False
        if row.get('status') not in ('running','idle') or not row.get('turn_id') or row['turn_id']==before.get('turn'):
            return False
        metadata=operation.get('metadata',{})
        if operation.get('phase')=='desktop-continuation-intent' and metadata.get('session')==sid:
            if not metadata.get('operation') or row.get('_client_user_message_id')!=metadata['operation']:return False
        if row.get('_settings_fingerprint')!=before.get('settings'):return False
        goal=dict(before['goal_record']) if before.get('goal_record') else None
        if goal and goal['status']=='usage_limited':goal['status']='active'
        if not DesktopAdapter._goal_progress_preserved(goal,row.get('_goal_record')):return False
    return True


def desktop_pause_superseded_by_running(operation, sessions, current_key):
    if operation.get('phase')!='desktop-interrupt-intent' or current_key!=operation.get('outgoing'):return False
    metadata=operation.get('metadata',{})
    row=next((s for s in sessions if s['id']==metadata.get('session')),None)
    if (not row or row.get('account_key')!=current_key or not row.get('_owner') or row.get('waiting')
            or row.get('is_child') or row.get('status')!='running' or row.get('goal_status') not in (None,'active')
            or not row.get('turn_id') or row['turn_id']==metadata.get('turn')
            or row.get('_settings_fingerprint')!=metadata.get('settings')):return False
    from desktop_adapter import DesktopAdapter
    return DesktopAdapter._goal_progress_preserved(metadata.get('goal_record'),row.get('_goal_record'))


def desktop_restart_superseded_by_usage(operation, sessions, current_key):
    from desktop_adapter import pending_desktop_recovery
    contexts=dict(operation.get('recovery_context',{}));changed=False
    rows={row['id']:row for row in sessions}
    if any(not desktop_startup_row_verified(contexts.get(sid),rows.get(sid),current_key)
            for sid in operation.get('sessions',[])):return False
    for row in sessions:
        before=contexts.get(row['id'])
        if (before and row.get('turn_id') and row['turn_id']!=before.get('turn')
                and not proxy_session(row) and row.get('status')=='failed' and recovery_error(row.get('error_kind'))):
            if row.get('_screen_quota') or not desktop_startup_new_turn(operation,before,row):return False
            contexts[row['id']]=dict(before,turn=row['turn_id']);changed=True
    return changed and pending_desktop_recovery(dict(operation,recovery_context=contexts),sessions,current_key) is not None

def desktop_startup_goal_preserved(before, row):
    # A native turn may run while the DB Goal still says usage_limited.
    # Normalize only active/usage_limited; preserve identity and accounting.
    from desktop_adapter import DesktopAdapter
    old=before.get('goal_record');new=row.get('_goal_record')
    if old is None or new is None:
        return old is None and new is None and row.get('goal_status') is None
    if (not all(DesktopAdapter._goal_accounting_valid(g) for g in (old,new))
            or old.get('status') not in ('active','usage_limited')
            or new.get('status') not in ('active','usage_limited')
            or row.get('goal_status')!=new.get('status') or old.get('deferred') or new.get('deferred')):
        return False
    return DesktopAdapter._goal_progress_preserved(dict(old,status=new['status']),new)


def desktop_startup_row_verified(before, row, current_key):
    return bool(before and before.get('turn') and before.get('settings') and row
        and row.get('_owner') and row.get('supported') and row.get('account_key')==current_key
        and row.get('provider') in (None,'codex') and not proxy_session(row)
        and not any(row.get(k) for k in ('waiting','is_child','_goal_deferred','_goal_mismatch','_ephemeral'))
        and row.get('turn_id') and row.get('_settings_fingerprint')==before['settings']
        and desktop_startup_goal_preserved(before,row))


def desktop_startup_new_turn(operation, before, row):
    stamp=operation.get('updated_at');started=row.get('_turn_started_at_ms')
    return bool(row.get('turn_id')!=before.get('turn')
        and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
            for v in (stamp,started)) and started>stamp*1000)


def desktop_restart_failed_before_send(operation, sessions, current_key):
    if (operation.get('phase') not in ('desktop-open-thread-intent','desktop-reopen-intent')
            or not current_key or current_key!=operation.get('target') or not operation.get('sessions')):
        return False
    rows={s['id']:s for s in sessions}
    for sid in operation['sessions']:
        row=rows.get(sid);before=operation.get('recovery_context',{}).get(sid)
        if (not desktop_startup_row_verified(before,row,current_key)
                or row.get('status')!='failed' or not recovery_error(row.get('error_kind'))
                or row.get('_screen_quota')
                or (row['turn_id']!=before['turn'] and not desktop_startup_new_turn(operation,before,row))):
            return False
    return True


def desktop_restart_superseded_by_later_turns(operation, sessions, current_key):
    # Before any prompt submission, a later verified turn makes the old startup
    # intent obsolete, including after a subsequent/manual account change.
    # This retires the old intent; normal policy decides whether to recover now.
    if (operation.get('phase') not in ('desktop-open-thread-intent','desktop-reopen-intent')
            or not current_key or not operation.get('sessions')
            or not isinstance(operation.get('updated_at'),(int,float))):return False
    rows={s['id']:s for s in sessions}
    for sid in operation['sessions']:
        row=rows.get(sid);before=operation.get('recovery_context',{}).get(sid)
        if (not desktop_startup_row_verified(before,row,current_key)
                or not desktop_startup_new_turn(operation,before,row)):return False
        running=row.get('status')=='running' and row.get('goal_status') in (None,'active','usage_limited')
        failed=(row.get('status')=='failed' and recovery_error(row.get('error_kind'))
                and not row.get('_screen_quota') and row.get('goal_status') in (None,'active','usage_limited'))
        if not (running or failed):return False
    return True

def desktop_close_superseded_by_completed_turn(operation, sessions, current_key):
    # Retire a failed Quit intent after the user subsequently completed the
    # original chat. This never restarts the app or continues that idle chat.
    if (operation.get('phase')!='desktop-close-intent' or not current_key
            or current_key not in (operation.get('outgoing'),operation.get('target'))
            or not operation.get('sessions') or not isinstance(operation.get('updated_at'),(int,float))):return False
    rows={s['id']:s for s in sessions}
    for sid in operation['sessions']:
        row=rows.get(sid);before=operation.get('recovery_context',{}).get(sid)
        if (not row or not before or row.get('status')!='idle' or row.get('goal_status') is not None
                or row.get('_goal_record') is not None or not row.get('_owner') or not row.get('supported')
                or row.get('account_key')!=current_key or row.get('waiting') or row.get('is_child')
                or row.get('_settings_fingerprint')!=before.get('settings') or not row.get('turn_id')
                or row['turn_id']==before.get('turn') or not isinstance(row.get('_turn_started_at_ms'),(int,float))
                or row['_turn_started_at_ms']<=operation['updated_at']*1000):return False
    return True

class Controller:
    def __init__(self, accounts=None, adapters=None):
        self.accounts = accounts or AccountManager()
        if adapters is None:
            from desktop_adapter import DesktopAdapter
            adapters = {'orca': OrcaAdapter(self.accounts), 'codex': DesktopAdapter(self.accounts)}
        self.adapters = adapters
        self.logs = []
        try:
            self.logs = [json.loads(line) for line in (LOCALDATA/'events.jsonl').read_text(encoding='utf-8').splitlines()[-60:]]
        except (OSError,ValueError):
            try:self.logs=load_json(STATUS).get('logs',[])[-60:]
            except (OSError,ValueError):pass
        self.last_inventory = 0
        self.lock = threading.RLock()
        self.attention = {}
        self.journal = None
        self.last_refresh = 0
        self.unresolved = []
        self.interruption_receipts = []
        self.completed_handoffs = []
        self.recovery_attempts = []
        try:self.quota_waits=load_json(LOCALDATA/'pending-quota.json')
        except (OSError,ValueError):self.quota_waits={}
        if not isinstance(self.quota_waits,dict):self.quota_waits={}
        self.refresh_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix='account-refresh')
        self.refresh_futures = []
        self.refresh_jobs = {}
        self.refresh_lock = threading.RLock()
        self.active_keys = set()
        self.refresh_started = 0
        self.refresh_completed = 0
        self.stage = 'starting'
        self.cycle_started = 0
        self.cycle_completed = 0
        self.keep_awake = False
        self._health_stop = threading.Event()
        self._published_discovered = None
        self._published_messages = {}
        for file in list((LOCALDATA / 'operations').glob('*.json')) + [JOURNAL]:
            try:
                previous = load_json(file)
                if previous.get('id') not in {op.get('id') for op in self.recovery_attempts}:
                    self.recovery_attempts.append(previous)
                from desktop_settings import migrate_operation
                if previous.get('app')=='codex' and migrate_operation(previous):
                    atomic_json(file,previous)
                if previous.get('app')=='codex' and previous.get('state')=='complete' and previous.get('outgoing')!=previous.get('target'):
                    if previous.get('id') not in {op['id'] for op in self.completed_handoffs}:self.completed_handoffs.append(previous)
                if confirmed_interruptions(previous):
                    if previous.get('id') not in {op['id'] for op in self.interruption_receipts}: self.interruption_receipts.append(previous)
                if previous.get('state') in ('in-progress', 'needs-attention'):
                    self.attention[previous['app']] = 'A handoff was interrupted. Check this chat before enabling recovery again.'
                    if previous.get('id') not in {op['id'] for op in self.unresolved}:self.unresolved.append(previous)
            except (OSError, ValueError, KeyError): pass

    def note(self, message):
        event={'time':time.strftime('%H:%M:%S'),'timestamp':time.time(),'message':message}
        self.logs.append(event)
        self.logs = self.logs[-60:]
        try:
            path=LOCALDATA/'events.jsonl'
            if path.exists() and path.stat().st_size>1024*1024:
                os.replace(path,path.with_suffix('.previous.jsonl'))
            with path.open('a',encoding='utf-8') as output:output.write(json.dumps(event,ensure_ascii=False)+'\n')
        except OSError:pass

    def diagnose(self, stage, exc):
        # Record where the check failed without persisting raw RPC/chat payloads.
        value={'timestamp':time.time(),'stage':stage,'error_type':type(exc).__name__,
            'frames':[{'file':pathlib.Path(f.filename).name,'line':f.lineno,'function':f.name}
                for f in traceback.extract_tb(exc.__traceback__)[-6:]]}
        try:atomic_json(LOCALDATA/'diagnostics.json',value)
        except OSError:pass
        self.note(f'{stage}: check failed ({type(exc).__name__}); retrying.')

    def health_loop(self):
        try:
            while not self._health_stop.is_set():
                try:
                    if hasattr(self.accounts,'records'):self.refresh_accounts(self.active_keys)
                except Exception as exc:self.diagnose('background account refresh',exc)
                try:atomic_json(LOCALDATA/'monitor-health.json',{'updated_at':time.time(),'pid':os.getpid(),
                    'version':VERSION,'stage':self.stage,'cycle_started':self.cycle_started,
                    'cycle_completed':self.cycle_completed,'keep_awake':self.keep_awake,
                    'handoff_phase':self.journal.get('phase') if self.journal and self.journal.get('state')=='in-progress' else None})
                except OSError:pass
                if os.name=='nt':ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if self.keep_awake else 0x80000000)
                self._health_stop.wait(3)
        finally:
            if os.name=='nt':ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)

    def journal_phase(self, phase, metadata):
        if phase=='orca-recovery-context':
            self.journal['orca_recovery_context']=metadata['context']
        if phase in ('resume-process-created','resume-process'):
            sid=metadata.get('session')
            if sid:
                receipts=self.journal.setdefault('orca_target_panes',{})
                previous=receipts.get(sid,{})
                before=(self.journal.get('orca_recovery_context') or {}).get(sid,{})
                terminal=metadata.get('terminal') or previous.get('terminal') or before.get('terminal')
                replaced=bool(terminal and before.get('terminal') and terminal!=before.get('terminal'))
                receipts[sid]={
                    'target':metadata.get('target') or self.journal.get('target'),
                    'terminal':terminal,
                    'incarnation':metadata.get('incarnation') or previous.get('incarnation') or before.get('incarnation'),
                    'pty_id':metadata.get('pty_id') or previous.get('pty_id') or (None if replaced else before.get('pty_id')),
                }
        if phase=='orca-exit-confirmed':
            self.journal.setdefault('orca_exit_receipts',{})[metadata['session']]=metadata
        if phase=='desktop-recovery-context':
            self.journal['recovery_context']=metadata['context']
        if phase == 'desktop-close-intent' and metadata.get('context'):
            self.journal['recovery_context'] = metadata['context']
            receipts=self.journal.setdefault('interruption_receipts',{})
            for sid,value in metadata.get('originals',{}).items():
                receipts[sid]={**receipts.get(sid,{}),**value,
                    'goal_record':metadata['context'].get(sid,{}).get('goal_record')}
        if phase=='desktop-interrupt-confirmed':
            self.journal.setdefault('interruption_receipts',{})[metadata['session']]={
                'turn':metadata['turn'],'settings':metadata['settings'],'goal':metadata.get('goal'),
                'goal_record':metadata.get('goal_record'),'relay_interrupted':True,
                'confirmation':metadata.get('confirmation','native-acknowledgement')}
        self.journal['phase'] = phase
        if phase in ('session-recovered','desktop-session-recovered'):
            recovered = self.journal.setdefault('recovered_sessions',[])
            if metadata['session'] not in recovered:recovered.append(metadata['session'])
        self.journal['metadata'] = metadata
        self.journal['updated_at'] = time.time()
        self.save_journal()
        self.stage=phase
        if self._published_discovered is not None:
            messages=dict(self._published_messages)
            messages[self.journal['app']]='Recovering chats: '+phase.replace('desktop-','').replace('-',' ')
            self.publish(self._published_discovered,messages)

    def save_journal(self):
        atomic_json(LOCALDATA / 'operations' / (self.journal['id'] + '.json'), self.journal)
        atomic_json(JOURNAL, self.journal)

    def _retry_desktop_open(self, operation, rows):
        # Navigation only. Keep the PRE-SEND phase and its timestamp intact so
        # retries cannot turn an old failure into evidence of a new submission.
        if operation.get('phase') not in ('desktop-open-thread-intent','desktop-reopen-intent'):return False
        setting=config().get('codex',{});ids=operation.get('sessions',[])
        current=self.accounts.current_desktop_key()
        if (not setting.get('enabled') or not ids or not set(ids)<=set(setting.get('sessions',[]))
                or not current or current!=operation.get('target') or not self.accounts.find(current)):
            return False
        by_id={row['id']:row for row in rows};missing=[]
        for sid in ids:
            before=operation.get('recovery_context',{}).get(sid);row=by_id.get(sid)
            if not before or not before.get('turn') or not before.get('settings'):return False
            goal=before.get('goal_record')
            if proxy_session(before.get('settings_snapshot') or {}):return False
            if not desktop_startup_goal_preserved(before,{'_goal_record':goal,'goal_status':goal.get('status') if goal else None}):return False
            if row:
                # Missing ownership alone may be repaired by reopening. Other
                # observed changes or a manual stop must remain protected.
                if not desktop_startup_row_verified(before,dict(row,_owner=True),current):return False
                receipt=operation.get('interruption_receipts',{}).get(sid,{})
                own_stop=(row.get('status')=='interrupted' and receipt.get('relay_interrupted')
                    and receipt.get('turn')==row.get('turn_id') and receipt.get('settings')==before['settings'])
                if not (row.get('status')=='running' or own_stop or
                        row.get('status')=='failed' and recovery_error(row.get('error_kind'))):return False
            if not row or not row.get('_owner'):missing.append(sid)
        if not missing:return False
        self.keep_awake=True
        self.attention['codex']='Waiting for original Codex chats to open; retrying every minute.'
        now=time.time();last=operation.get('desktop_reopen_retry_at')
        if isinstance(last,(int,float)) and now-last<60:return True
        try:
            operation['desktop_reopen_retry_at']=now
            operation['desktop_reopen_retry_count']=operation.get('desktop_reopen_retry_count',0)+1
            atomic_json(LOCALDATA/'operations'/(operation['id']+'.json'),operation)
            if JOURNAL.exists() and load_json(JOURNAL).get('id')==operation['id']:atomic_json(JOURNAL,operation)
            # Recheck user selection and account immediately before navigation.
            setting=config().get('codex',{})
            if (setting.get('enabled') and set(ids)<=set(setting.get('sessions',[]))
                    and self.accounts.current_desktop_key()==current):
                self.adapters['codex'].request_reopen(missing)
        except Exception as exc:
            self.diagnose('reopening original Codex chats',exc)
        return True

    def _record_completed_quota_failures(self, discovered, settings):
        codex_settings=settings.get('codex',{})
        orca_settings=settings.get('orca',{})
        for app in ('orca','codex'):
            app_settings=orca_settings if app=='orca' else codex_settings
            if not app_settings.get('enabled'):
                continue
            selected=set(app_settings.get('sessions',[]))
            operations=[op for op in self.recovery_attempts
                if op.get('app')==app and op.get('state')=='complete']
            rows={row.get('id'):row for row in discovered.get(app,[])
                if row.get('id') in selected}
            for sid,session in rows.items():
                if any(op.get('quota_failed_sessions',{}).get(sid,{}).get('turn')==session.get('turn_id')
                        for op in operations):
                    continue
                candidates=[]
                for operation in operations:
                    evidence=quota_failure_after_handoff(session,operation)
                    if evidence is not None:
                        stamp=operation.get('started_at',operation.get('updated_at',0))
                        candidates.append((stamp if isinstance(stamp,(int,float)) else 0,operation,evidence))
                if not candidates:
                    continue
                _stamp,operation,evidence=max(candidates,key=lambda item:item[0])
                operation_id=operation.get('id')
                try:
                    for candidate in self.recovery_attempts+self.completed_handoffs:
                        if candidate.get('id')==operation_id:
                            candidate.setdefault('quota_failed_sessions',{})[sid]=evidence
                    if self.journal and self.journal.get('id')==operation_id:
                        self.journal.setdefault('quota_failed_sessions',{})[sid]=evidence
                    atomic_json(LOCALDATA/'operations'/(str(operation_id)+'.json'),operation)
                    if JOURNAL.exists():
                        latest=load_json(JOURNAL)
                        if latest.get('id')==operation_id:
                            atomic_json(JOURNAL,operation)
                except (OSError,ValueError,TypeError) as exc:
                    self.attention[app]='Could not save quota-failure evidence; recovery is paused for review.'
                    self.diagnose('saving completed-attempt quota evidence',exc)
                    return

    def publish(self, discovered, messages):
        self._published_discovered=discovered
        self._published_messages=dict(messages)
        rows = []
        for record in self.accounts.records:
            quota = self.accounts.quotas.get(record['key'], {})
            if str(quota.get('plan', '')).lower() == 'free': continue
            rows.append({'key': record['key'], 'label': record['label'], 'sources': record['sources'],
                'checked_at':quota.get('updated_at'),
                'ordinary_allowed':quota.get('ordinary_allowed',True),
                **{key: quota.get(key) for key in ('valid', 'plan', 'five_remaining', 'weekly_remaining', 'weekly_only', 'fallback_only', 'reset_at', 'weekly_reset_at', 'error')}})
        public = {app: [{key: s.get(key) for key in ('id','title','status','goal_status','waiting','model','effort','supported','account_key')}
            for s in values] for app, values in discovered.items()}
        choices = {}
        for app, adapter in self.adapters.items():
            if hasattr(adapter, 'selectable_sessions'):
                try: choices[app] = adapter.selectable_sessions(discovered.get(app, []))
                except Exception: pass
        try:atomic_json(STATUS, {'updated_at':time.time(), 'pid':os.getpid(), 'messages':messages,
            'sessions':public, 'accounts':rows, 'logs':self.logs, 'attention':self.attention, 'version':VERSION,
            'selectable_sessions':choices,
            'refresh':{'running':bool(self.refresh_futures), 'last_completed':self.refresh_completed, 'interval_seconds':ACCOUNT_REFRESH_SECONDS}})
        except OSError:
            # Status is a display snapshot, not a mutation receipt. A locked
            # panel file must not cancel a safely journaled handoff.
            if not getattr(self,'_status_write_failed',False):self.note('Status display is temporarily locked; recovery checks continue.')
            self._status_write_failed=True
        else:self._status_write_failed=False

    def refresh_accounts(self, active_keys):
        with self.refresh_lock:
            now=time.time()
            for key,job in list(self.refresh_jobs.items()):
                if job.done():
                    try:job.result()
                    except Exception:pass  # Failed login rows are retried too.
                    del self.refresh_jobs[key]
                    self.refresh_completed=now
            initial=not self.refresh_jobs and now-self.refresh_started>=ACCOUNT_REFRESH_SECONDS
            for record in self.accounts.records:
                key=record['key'];quota=self.accounts.quotas.get(key,{})
                checked=quota.get('updated_at') or 0
                due=now-checked>=ACCOUNT_REFRESH_SECONDS or any(isinstance(quota.get(k),(int,float)) and
                    checked<quota[k]<=now for k in ('reset_at','weekly_reset_at'))
                if key in self.refresh_jobs or not (initial or due):continue
                # The monitor owns recent selected-account probes. If its cycle
                # stalls, stale selected accounts still receive background checks.
                if key in active_keys and now-checked<ACCOUNT_REFRESH_SECONDS:continue
                self.refresh_jobs[key]=self.refresh_pool.submit(self.accounts.probe,record,True)
            self.refresh_futures=list(self.refresh_jobs.values())
            if self.refresh_futures:self.refresh_started=now

    def cycle(self):
        self.cycle_started=time.time()
        self.stage='reading configuration'
        cfg = config()
        threshold = normalize_threshold(cfg.get('threshold', 2))
        if time.time() - self.last_inventory > ACCOUNT_REFRESH_SECONDS:
            self.stage='reading saved accounts'
            self.accounts.inventory(); self.last_inventory = time.time()
        discovered, messages = {}, {}
        for app, adapter in self.adapters.items():
            self.stage='checking '+app+' chats'
            try:
                discovered[app] = adapter.discover()
                messages[app] = 'Ready' if discovered[app] else 'Open a Codex chat in this application'
            except Exception as exc:
                discovered[app] = []
                messages[app] = 'Application is unavailable'
                self.diagnose(self.stage,exc)
        self.keep_awake=any(cfg.get(app,{}).get('enabled') and any(
            s['id'] in cfg.get(app,{}).get('sessions',[]) and
            (s.get('status')=='running' or s.get('goal_status')=='usage_limited' or
             (s.get('status')=='failed' and recovery_error(s.get('error_kind')))) for s in rows)
            for app,rows in discovered.items())
        active_keys = {s.get('account_key') for app, rows in discovered.items() if cfg.get(app, {}).get('enabled')
            for s in rows if s['id'] in cfg.get(app, {}).get('sessions', [])}
        self.active_keys=active_keys
        self.refresh_accounts(active_keys)
        self.publish(discovered, {app: self.attention.get(app, 'Checking selected chats and allowances') for app in self.adapters})
        for operation in self.interruption_receipts:
            for metadata in confirmed_interruptions(operation):
                row = next((s for s in discovered.get('codex', []) if s['id'] == metadata.get('session')), None)
                from desktop_adapter import DesktopAdapter
                goal_matches=(not metadata.get('goal_record') or (row and DesktopAdapter._goal_progress_preserved(metadata['goal_record'],row.get('_goal_record'))))
                if (row and row.get('status') == 'interrupted' and row.get('turn_id') == metadata.get('turn')
                    and row.get('_settings_fingerprint') == metadata.get('settings') and row.get('goal_status') in (None,'active')
                    and goal_matches and not row.get('waiting') and row.get('account_key') == metadata.get('account')):
                    self.adapters['codex'].recovery_receipts[row['id']] = (row['turn_id'], row['_settings_fingerprint'])
        self.keep_awake=self.keep_awake or any(cfg.get(app,{}).get('enabled') and any(
            row['id'] in cfg.get(app,{}).get('sessions',[]) and row.get('status')=='interrupted' and
            getattr(self.adapters[app],'recovery_receipts',{}).get(row['id'])==(row.get('turn_id'),row.get('_settings_fingerprint'))
            for row in rows) for app,rows in discovered.items())
        retrying_desktop=False
        for operation in list(self.unresolved):
            app=operation['app'];phase=operation.get('phase');metadata=operation.get('metadata',{})
            safe=phase in ('prepare','desktop-recovery-context')
            failed_after_send=app=='orca' and orca_continuation_failed(operation,discovered.get(app,[]))
            safe=safe or failed_after_send
            superseded=(app=='codex' and desktop_pause_superseded_by_quota(operation,discovered.get(app,[]),self.accounts.current_desktop_key()))
            resumed=(app=='codex' and desktop_pause_superseded_by_running(operation,discovered.get(app,[]),self.accounts.current_desktop_key()))
            safe=safe or resumed
            safe=safe or superseded
            recovered=(app=='codex' and operation.get('recovery_context') and phase in
                ('desktop-continuation-intent','desktop-session-recovered')
                and desktop_recovery_confirmed(operation,discovered.get(app,[]),self.accounts.current_desktop_key()))
            safe=safe or bool(recovered)
            setting=config().get(app,{})
            startup_selected=(setting.get('enabled') and bool(operation.get('sessions'))
                and set(operation['sessions'])<=set(setting.get('sessions',[])))
            later=(app=='codex' and startup_selected and self.accounts.find(self.accounts.current_desktop_key()) is not None
                and desktop_restart_superseded_by_later_turns(operation,discovered.get(app,[]),self.accounts.current_desktop_key()))
            safe=safe or later
            startup_failed=(app=='codex' and startup_selected and self.accounts.find(self.accounts.current_desktop_key()) is not None
                and desktop_restart_failed_before_send(operation,discovered.get(app,[]),self.accounts.current_desktop_key()))
            safe=safe or startup_failed
            new_startup_failure=bool((later or startup_failed) and self.accounts.current_desktop_key()==operation.get('target')
                and any(row['id'] in operation['sessions'] and row.get('status')=='failed'
                    and desktop_startup_new_turn(operation,operation.get('recovery_context',{}).get(row['id'],{}),row)
                    for row in discovered.get(app,[])))
            finished=(app=='codex' and self.accounts.find(self.accounts.current_desktop_key()) is not None
                and desktop_close_superseded_by_completed_turn(operation,discovered.get(app,[]),self.accounts.current_desktop_key()))
            safe=safe or finished
            if app=='codex' and not safe:
                from desktop_adapter import pending_desktop_recovery
                rows=discovered.get(app,[]);current_key=self.accounts.current_desktop_key()
                pending=pending_desktop_recovery(operation,rows,current_key)
                if pending is not None:
                    by_id={row['id']:row for row in rows}
                    if any(not desktop_startup_row_verified(operation.get('recovery_context',{}).get(sid),by_id.get(sid),current_key)
                            for sid in operation.get('sessions',[])):pending=None
                setting=config().get(app,{})
                if startup_selected and desktop_restart_superseded_by_usage(operation,rows,current_key):
                    for row in rows:
                        receipt=operation.get('interruption_receipts',{}).get(row['id'],{})
                        if row.get('status')=='interrupted' and receipt.get('relay_interrupted') and receipt.get('turn')==row.get('turn_id') and receipt.get('settings')==row.get('_settings_fingerprint'):
                            self.adapters[app].recovery_receipts[row['id']]=(row['turn_id'],row['_settings_fingerprint'])
                    safe=True
                    new_startup_failure=any(row['id'] in operation['sessions'] and row.get('status')=='failed'
                        and desktop_startup_new_turn(operation,operation.get('recovery_context',{}).get(row['id'],{}),row)
                        for row in rows)
                    self.note('Codex: a new usage/login failure after startup requires another available account.')
                if not safe and self._retry_desktop_open(operation,rows):retrying_desktop=True
                if not safe and pending and startup_selected:
                    self.keep_awake=True
                    target=self.accounts.find(operation.get('target'))
                    if target:
                        self.stage='checking delayed recovery allowance'
                        quota=(self.accounts.probe_desktop(target,force=True) if hasattr(self.accounts,'probe_desktop')
                               else self.accounts.probe(target,force=True,home=REAL_HOME))
                        if quota.get('valid') and any(isinstance(quota.get(k),(int,float)) and quota[k]<=threshold for k in ('five_remaining','weekly_remaining')):
                            for row in rows:
                                if row['id'] in pending and row.get('status')=='interrupted':
                                    self.adapters[app].recovery_receipts[row['id']]=(row['turn_id'],row['_settings_fingerprint'])
                            safe=True
                            self.note('Codex: the verified restart account is depleted; checking another saved account.')
                        if available(quota,threshold):
                            # This phase proves no continuation was submitted. Record
                            # the submission intent durably before the native send.
                            self.journal=operation;operation['state']='in-progress';self.save_journal()
                            self.note('Codex: finishing the verified account switch after delayed startup.')
                            try:
                                self.adapters[app].resume_in_place(pending,self.journal_phase,recovery_operation=dict(operation))
                                operation['recovered_sessions']=list(operation['sessions'])
                                safe=True;recovered=True
                            except Exception as exc:
                                operation['state']='needs-attention';self.attention[app]='Codex startup recovery failed; checking again.'
                                self.diagnose(self.stage,exc);self.save_journal()
            if app == 'orca':
                # A user may have recovered the chat manually in a new pane.
                # Verify the actual native account; never replay that handoff.
                rows = {s['id']:s for s in discovered.get(app, [])}
                safe = bool(operation.get('sessions')) and all(
                    sid in rows and rows[sid].get('account_key') == operation.get('target')
                    and rows[sid].get('status') in ('running', 'idle')
                    and not rows[sid].get('waiting') and not rows[sid].get('is_child') and not proxy_session(rows[sid])
                    and orca_resumed_context_verified(operation,rows[sid])
                    and rows[sid].get('connected') and (sid in operation.get('recovered_sessions',[])
                        or rows[sid].get('terminal') != (operation.get('originals',{}).get(sid,{}).get('terminal') or metadata.get('terminal'))
                        or (operation.get('originals',{}).get(sid,{}).get('turn') and rows[sid].get('turn_id') != operation['originals'][sid]['turn']))
                    for sid in operation.get('sessions', [])) or safe
            if phase=='desktop-interrupt-confirmed':
                row=next((s for s in discovered[app] if s['id']==metadata.get('session')),None)
                from desktop_adapter import DesktopAdapter
                goal_matches=not metadata.get('goal_record') or (row and DesktopAdapter._goal_progress_preserved(metadata['goal_record'],row.get('_goal_record')))
                if row and goal_matches and row.get('status')=='interrupted' and row.get('turn_id')==metadata.get('turn') and row.get('_settings_fingerprint')==metadata.get('settings') and row.get('goal_status')=='active' and not row.get('waiting'):
                    self.adapters[app].recovery_receipts[row['id']] = (row['turn_id'],row['_settings_fingerprint'])
                    row['controller_interrupted']=True;safe=True
            if not safe and phase == 'desktop-close-intent' and app == 'codex' and self.accounts.current_desktop_key() == operation.get('outgoing'):
                rows = {s['id']:s for s in discovered.get(app, [])}
                originals = metadata.get('originals', {})
                safe = bool(operation.get('sessions'))
                for sid in operation.get('sessions', []):
                    row = rows.get(sid)
                    receipt = getattr(self.adapters[app], 'recovery_receipts', {}).get(sid)
                    original = originals.get(sid)
                    if not row or row.get('waiting') or not row.get('_owner') or row.get('account_key') != operation.get('outgoing'):
                        safe = False; break
                    if original and (row.get('turn_id') != original['turn'] or row.get('_settings_fingerprint') != original['settings'] or row.get('goal_status') != original['goal']):
                        safe = False; break
                    if original and original.get('relay_interrupted') and row.get('status') == 'interrupted' and row.get('goal_status') in (None, 'active'):
                        receipt = (row.get('turn_id'), row.get('_settings_fingerprint'))
                        self.adapters[app].recovery_receipts[sid] = receipt
                    if not (row.get('goal_status') == 'usage_limited' or (row.get('status') == 'interrupted' and receipt == (row.get('turn_id'), row.get('_settings_fingerprint')))):
                        safe = False; break
            if safe:
                operation['state']='complete' if recovered else 'reconciled-after-quota-failure' if failed_after_send or new_startup_failure else 'reconciled-before-continuation'
                if startup_failed:
                    operation['reconciliation']='Verified original owners stopped on quota/login failure before any continuation; reassessing normal recovery.'
                if failed_after_send:self.note('Orca: continuation reached a new usage/login failure; checking available accounts again.')
                if finished:
                    operation['reconciliation']='Failed Quit superseded by a later completed native turn with unchanged settings and no active Goal; no restart or continuation replayed.'
                    self.note('Codex: cleared an old Quit failure after verifying the chat subsequently completed; leaving it idle.')
                if later:
                    operation['reconciliation']='Startup intent superseded by later native turns with preserved settings and Goal accounting; no old prompt replayed.'
                    operation['reconciliation_evidence']={'account':self.accounts.current_desktop_key(),
                        'turns':{row['id']:row.get('turn_id') for row in discovered.get(app,[]) if row['id'] in operation['sessions']},
                        'verified_at':time.time()}
                    self.note('Codex: retired an old startup failure after verifying newer turns; checking current quota recovery.')
                if superseded:
                    operation['reconciliation']='Original pause superseded by a newer verified quota/login failure; no pause or continuation replayed.'
                    self.note('Codex: a newer quota/login failure superseded the earlier pause attempt; checking recovery again.')
                if resumed:
                    operation['reconciliation']='A verified new running turn superseded the earlier pause attempt; preserve its work and confirmed sibling pauses.'
                    self.note('Codex: the chat resumed in a verified new turn; preserving it and recovering the relay-paused chats.')
                if recovered:
                    operation['recovered_sessions']=list(operation['sessions'])
                    self.note('Codex: verified the recovered chats after delayed startup.')
                atomic_json(LOCALDATA/'operations'/(operation['id']+'.json'),operation)
                if JOURNAL.exists() and load_json(JOURNAL).get('id')==operation['id']:atomic_json(JOURNAL,operation)
                self.unresolved.remove(operation)
                if not any(o['app']==app for o in self.unresolved):self.attention.pop(app,None)
        self._record_completed_quota_failures(discovered,cfg)
        if not STATUS.exists():
            atomic_json(STATUS, {'updated_at': time.time(), 'pid': os.getpid(), 'messages': {a:'Checking saved account allowances…' for a in self.adapters},
                'sessions': {a:[{k:s.get(k) for k in ('id','title','status','goal_status','waiting','model','effort','supported','account_key')} for s in values] for a,values in discovered.items()}, 'accounts':[], 'logs':[], 'attention':{}})
        for app, adapter in self.adapters.items():
            setting = cfg.get(app, {})
            if not setting.get('enabled'):
                continue
            if app in self.attention and app != 'orca' and not (app=='codex' and retrying_desktop
                    and all(op.get('phase') in ('desktop-open-thread-intent','desktop-reopen-intent')
                        for op in self.unresolved if op['app']==app)):
                messages[app] = self.attention[app]; continue
            selected = set(setting.get('sessions', []))
            blocked = {sid for op in self.unresolved if op['app'] == app for sid in op.get('sessions', []) if sid not in op.get('recovered_sessions',[])}
            sessions = [s for s in discovered[app] if s['id'] in selected and s['id'] not in blocked]
            if not selected:
                messages[app] = 'Select chats before enabling automation'; continue
            if not sessions:
                messages[app] = 'Selected chats need handoff recovery review' if blocked & selected else 'Waiting for selected chats to attach; check their panes for an exit or ownership conflict'; continue
            other = 'codex' if app == 'orca' else 'orca'
            reserved = {s.get('account_key') for s in discovered.get(other, []) if s.get('account_key')}
            if other == 'codex': reserved.add(self.accounts.current_desktop_key())
            groups = {}
            return_groups = set()
            current_quotas = {}
            for session in sessions:
                if session.get('supported') is False or (session.get('connected') is False and not session.get('_exited_pane')):continue
                key = session.get('account_key')
                record = self.accounts.find(key)
                if not record: continue
                home = session.get('home') if app == 'orca' else REAL_HOME
                probe_key = (key, str(home))
                if probe_key not in current_quotas:
                    self.stage='checking '+app+' allowance'
                    current_quotas[probe_key] = self.accounts.probe(record, force=True, home=home)
                quota = current_quotas[probe_key]
                pending_key=app+':'+session['id']
                eligible=needs_switch(session,quota,threshold)
                if eligible and session.get('_screen_quota') and session.get('status')=='failed':
                    receipt=quota_wait_identity(session)
                    if self.quota_waits.get(pending_key)!=receipt:
                        self.quota_waits[pending_key]=receipt
                        atomic_json(LOCALDATA/'pending-quota.json',self.quota_waits)
                pending=verified_screen_wait(session,self.quota_waits.get(pending_key))
                if pending_key in self.quota_waits and not pending:
                    self.quota_waits.pop(pending_key,None)
                    atomic_json(LOCALDATA/'pending-quota.json',self.quota_waits)
                receipt=getattr(adapter,'recovery_receipts',{}).get(session['id'])
                if eligible or pending or (receipt==(session.get('turn_id'),session.get('_settings_fingerprint')) and session['status']=='interrupted'):
                    groups.setdefault(key, []).append(session)
                elif can_return_from_weekly(session, quota) and any(
                    r['key'] != key and (app != 'orca' or r.get('orca_id')) and
                    destination_needs_check(self.accounts.quotas.get(r['key'], {}), threshold) and
                    not weekly_fallback(self.accounts.quotas.get(r['key'],{})) for r in self.accounts.records):
                    groups.setdefault(key, []).append(session)
                    return_groups.add(key)
            messages[app] = f'Monitoring {len(sessions)} selected chat(s)'
            for outgoing, affected in groups.items():
                # A real usage/login failure still follows normal recovery if
                # another working chat on the same account requested a return.
                returning = outgoing in return_groups and all(can_return_from_weekly(s,
                    current_quotas.get((outgoing, str(s.get('home') if app == 'orca' else REAL_HOME)), {})) for s in affected)
                in_place=(app=='codex' and available(current_quotas.get((outgoing,str(REAL_HOME)),{}),threshold)
                    and all(quota_failure_before_handoff(s,self.completed_handoffs) for s in affected))
                try:
                    self.stage='checking '+other+' account ownership'
                    other_sessions = self.adapters[other].discover()
                    reserved = {s.get('account_key') for s in other_sessions if s.get('account_key')}
                    uncertain={key for s in other_sessions for key in s.get('reserved_keys',[]) if key}
                    reserved.update(uncertain)
                    if other == 'codex': reserved.add(self.accounts.current_desktop_key())
                except Exception as exc:
                    self.diagnose(self.stage,exc)
                    # A stopped Desktop still has a known saved login; reserve it conservatively.
                    if other == 'codex': reserved = {self.accounts.current_desktop_key()};uncertain=set()
                    else:
                        messages[app] = 'Waiting until the Orca account can be checked'
                        continue
                messages[app] = 'Checking available saved accounts'
                self.publish(discovered, messages)
                self.stage='checking available saved accounts'
                uncertain.update(quota_failed_target_keys(app,affected,self.recovery_attempts,self.accounts.quotas))
                # The background refresh owns inventory probes. Failed saved
                # credentials must not stall every active chat for minutes.
                if in_place:target,sharing=self.accounts.find(outgoing),False
                else:target,sharing=choose_destination(self.accounts,app,outgoing,reserved,uncertain,threshold,five_hour_only=returning)
                if not target and not returning:
                    quota=next((q for (key,_),q in current_quotas.items() if key==outgoing),{})
                    if (outgoing not in uncertain and available(quota,threshold)
                            and same_account_retry_allowed(app,affected,quota,self.recovery_attempts)):
                        target=self.accounts.find(outgoing)
                        in_place=app=='codex' and in_place_recovery_eligible(affected)
                if returning and not target:
                    messages[app] = f'Monitoring {len(sessions)} selected chat(s); using weekly fallback'
                    continue
                if sharing:self.note(f'{app.title()}: using the other app\'s account because no separate usable account remains.')
                if not target and cfg.get('included_resets',False):
                    from reset_bank import restore_allowance
                    messages[app]='Checking included usage resets';self.publish(discovered,messages)
                    try:
                        target=restore_allowance(self.accounts,[r for r in self.accounts.records if (app!='orca' or r.get('orca_id'))],reserved,threshold,available)
                        if target:self.note('Applied an included usage reset; allowance verified.')
                    except Exception as exc:
                        messages[app]=str(exc)[:160];continue
                if not target:
                    messages[app] = allowance_wait_message(self.accounts.quotas.values())
                    continue
                if target['key']==outgoing:
                    # Other account checks may have taken time. The original
                    # account is a destination too, and needs a final fresh check.
                    home=(target.get('orca_home') or affected[0].get('home')) if app=='orca' else REAL_HOME
                    quota=self.accounts.probe(target,force=True,home=home)
                    if not available(quota,threshold):
                        messages[app]=allowance_wait_message(self.accounts.quotas.values())
                        continue
                # Re-read selection immediately before any mutation.
                current_cfg = config().get(app, {})
                requested_ids = [s['id'] for s in affected]
                same_account_recovery_ids=(requested_ids if app=='codex' and target['key']==outgoing
                    and same_account_restart_eligible(affected) else None)
                if app == 'codex' and not in_place and current_cfg.get('preserve_running',False):
                    # Desktop's normal Quit affects its entire process. Preserve
                    # every healthy running chat, including proxy providers.
                    extra=[s for s in discovered[app] if s['id'] not in requested_ids and (s.get('status')=='running' or
                           (s.get('status')=='interrupted' and getattr(adapter,'recovery_receipts',{}).get(s['id']) ==
                            (s.get('turn_id'),s.get('_settings_fingerprint'))))
                           and not s.get('waiting') and not s.get('is_child') and s.get('goal_status') in (None,'active')]
                    affected=affected+extra
                ids = [s['id'] for s in affected]
                if not current_cfg.get('enabled') or not set(requested_ids) <= set(current_cfg.get('sessions', [])): continue
                self.journal = {'id': uuid.uuid4().hex, 'state': 'in-progress', 'app': app,
                    'sessions': ids, 'outgoing': outgoing, 'target': target['key'], 'phase': 'prepare', 'updated_at': time.time(),
                    'originals':{s['id']:{'terminal':s.get('terminal'),'turn':s.get('turn_id')} for s in affected}}
                self.journal['started_at']=self.journal['updated_at']
                self.journal['target_quota']={k:self.accounts.quotas.get(target['key'],{}).get(k)
                    for k in ('reset_at','weekly_reset_at')}
                self.journal['target_quota']['allowance_generation'] = self.accounts.quotas.get(target['key'],{}).get('_allowance_generation',0)
                self.recovery_attempts.append(self.journal)
                self.save_journal()
                self.note(f'{app.title()}: '+('returning from weekly fallback to verified five-hour allowance.' if returning else
                    'continuing after the verified account switch' if in_place else f'switching account for {len(ids)} chat(s).'))
                try:
                    if in_place:adapter.resume_in_place(ids,self.journal_phase)
                    elif same_account_recovery_ids:
                        adapter.rotate(ids,target,self.journal_phase,same_account_recovery=True,
                            same_account_recovery_ids=same_account_recovery_ids)
                    else:adapter.rotate(ids, target, self.journal_phase)
                    self.journal['state'] = 'complete'
                    if app=='codex' and outgoing!=target['key']:self.completed_handoffs.append(dict(self.journal))
                    self.note(f'{app.title()}: recovered {len(ids)} chat(s).')
                    messages[app] = f'Recovered {len(ids)} chat(s); monitoring'
                except Exception as exc:
                    self.diagnose(self.stage,exc)
                    # Never replay a partially confirmed mutation automatically.
                    self.journal['state'] = 'needs-attention'
                    self.attention[app] = str(exc)[:180]
                    self.unresolved.append(self.journal)
                    messages[app] = self.attention[app]
                    self.note(f'{app.title()}: recovery stopped for review.')
                self.save_journal()
                if app in self.attention and app != 'orca': break
        self.publish(discovered, messages)
        self.stage='waiting for next check'
        self.cycle_completed=time.time()

    def run(self):
        self.keep_awake=any(config().get(a,{}).get('enabled') and config().get(a,{}).get('sessions') for a in self.adapters)
        health=threading.Thread(target=self.health_loop,name='monitor-health',daemon=True)
        health.start()
        while True:
            try: self.cycle()
            except Exception as exc:
                self.diagnose(self.stage,exc)
                self.publish(self._published_discovered or {},{a:'Check failed during '+self.stage+'; retrying' for a in self.adapters})
            if (LOCALDATA/'stop-request.json').exists():
                (LOCALDATA/'stop-request.json').unlink(missing_ok=True)
                for adapter in self.adapters.values():adapter.close()
                self.refresh_pool.shutdown(wait=False, cancel_futures=True)
                self._health_stop.set();health.join(timeout=4)
                return
            time.sleep(5)

def service():
    LOCALDATA.mkdir(parents=True, exist_ok=True)
    mutex = None
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        mutex = kernel.CreateMutexW(None, False, 'Local\\SessionRelayMonitor')
        if ctypes.get_last_error() == 183: return
    Controller().run()

if __name__ == '__main__': service()
