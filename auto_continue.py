"""Durable, opt-in continuation of stopped native chats, independent of Goals."""
import time, uuid
from accounts import atomic_json, load_json
from recovery_policy import proxy_session, recovery_error

RETRY_SECONDS = 180


def stopped_chat(row):
    return bool(row.get('status') in ('idle','failed') and row.get('_runtime_status') in ('idle','notLoaded','systemError')
        and row.get('_owner') and row.get('turn_id') and row.get('_settings_fingerprint') and row.get('supported')
        and not any(row.get(k) for k in ('waiting','is_child','_ephemeral','_goal_deferred','_goal_mismatch'))
        and row.get('goal_status') not in ('paused','budget_limited','usage_limited') and not proxy_session(row))


class AutoContinue:
    def __init__(self,path):
        self.path=path;self.invalid=False
        try:self.data=load_json(path)
        except FileNotFoundError:self.data={}
        except (OSError,ValueError):self.data={};self.invalid=True
        if not isinstance(self.data,dict):self.data={};self.invalid=True
        self.data.setdefault('sessions',{});self.data.setdefault('quota_failures',[])
        if (not isinstance(self.data['sessions'],dict) or not isinstance(self.data['quota_failures'],list)
                or any(not isinstance(r,dict) for r in self.data['quota_failures'])
                or any(not isinstance(r,dict) or r.get('phase') not in ('intent','confirmed','rejected')
                    for r in self.data['sessions'].values())):
            self.data={'sessions':{},'quota_failures':[]};self.invalid=True

    def save(self):atomic_json(self.path,self.data)

    def pending_ids(self):
        return {sid for sid,r in self.data['sessions'].items() if r.get('phase')=='intent'}

    def failures(self):return list(self.data['quota_failures'])

    def _confirm(self,sid,receipt,row):
        receipt.update(phase='confirmed',new_turn=row['turn_id'])
        if recovery_error(row.get('error_kind')):
            receipt['quota_recorded']=True
            key=receipt['message_id']
            if not any(op.get('id')==key for op in self.data['quota_failures']):
                self.data['quota_failures'].append({'id':key,'app':'codex','state':'reconciled-after-quota-failure',
                    'sessions':[sid],'target':receipt['account'],'target_quota':dict(receipt['target_quota'])})
        self.save()

    def reconcile(self,rows,current):
        if self.invalid:return
        for row in rows:
            receipt=self.data['sessions'].get(row['id'],{})
            if ((receipt.get('phase')=='intent' or receipt.get('phase')=='confirmed' and recovery_error(row.get('error_kind')) and not receipt.get('quota_recorded'))
                    and row.get('account_key')==current==receipt.get('account')
                    and row.get('_owner') and row.get('_settings_fingerprint')==receipt.get('settings')
                    and row.get('turn_id') and row['turn_id']!=receipt.get('turn')
                    and row.get('_client_user_message_id')==receipt.get('message_id')):
                self._confirm(row['id'],receipt,row)

    def drive(self,rows,setting,accounts,adapter,threshold,blocked,available,recoveries=()):
        if self.invalid or not setting.get('enabled') or not setting.get('continue_on_stop'):return 0
        sent=0;selected=set(setting.get('sessions',[]));current=accounts.current_desktop_key()
        for row in rows:
            sid=row['id'];last=self.data['sessions'].get(sid,{})
            if (sid not in selected or sid in blocked or last.get('phase')=='intent' or not stopped_chat(row)
                    or row.get('account_key')!=current or recovery_error(row.get('error_kind'))):continue
            if last.get('turn')==row['turn_id'] and last.get('phase')=='confirmed':continue
            stamp=last.get('at')
            stamps=[stamp] if isinstance(stamp,(int,float)) else []
            # The normal quota/login recovery also sends a continuation. Its
            # durable confirmation shares this cooldown, including after restart.
            for operation in recoveries:
                if operation.get('app')!='codex':continue
                if (sid not in operation.get('recovered_sessions',[]) and not
                        (operation.get('state')=='complete' and sid in operation.get('sessions',[]))):continue
                recovered_at=operation.get('recovered_at',{}).get(sid,operation.get('updated_at'))
                if isinstance(recovered_at,(int,float)):stamps.append(recovered_at)
            if stamps and time.time()<max(stamps)+RETRY_SECONDS:continue
            record=accounts.find(current)
            if not record:continue
            cached=getattr(accounts,'quotas',{}).get(current)
            if cached is not None and not available(cached,threshold):continue
            quota=accounts.probe_desktop(record,force=True)
            if accounts.current_desktop_key()!=current or not available(quota,threshold):continue
            receipt={'phase':'intent','turn':row['turn_id'],'settings':row['_settings_fingerprint'],
                'account':current,'message_id':str(uuid.uuid4()),'at':time.time(),
                'target_quota':{k:quota.get(k) for k in ('reset_at','weekly_reset_at')}}
            receipt['target_quota']['allowance_generation']=quota.get('_allowance_generation',0)
            self.data['sessions'][sid]=receipt;self.save()
            try:
                result=adapter.continue_stopped(row,receipt['message_id'])
            except Exception as exc:
                from desktop_adapter import DesktopStoppedStateError
                if isinstance(exc,DesktopStoppedStateError):
                    receipt['phase']='rejected';self.save()
                # An uncertain request remains an intent across monitor restarts.
                continue
            accepted=dict(row,turn_id=result.get('turn_id'),error_kind=result.get('error_kind'))
            if (accepted['turn_id'] and accepted['turn_id']!=receipt['turn']
                    and result.get('client_user_message_id')==receipt['message_id']
                    and result.get('settings_fingerprint')==receipt['settings']):
                self._confirm(sid,receipt,accepted);sent+=1
        return sent
