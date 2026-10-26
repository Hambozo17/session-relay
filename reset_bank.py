"""Consume only an included reset through Orca's scoped, idempotent API."""
import json, math, time, uuid
from accounts import LOCALDATA, atomic_json, load_json

TARGET = {'runtime':'host','wslDistro':None}

def all_weekly_exhausted(records, quotas, now=None):
    """A five-hour threshold or a reservation cannot authorize a reset."""
    now=time.time() if now is None else now
    supported=0
    for record in records:
        quota=quotas.get(record['key'],{})
        checked=quota.get('updated_at')
        if not isinstance(checked,(int,float)) or not math.isfinite(checked) or not -5 <= now-checked <= 180:
            return False
        if quota.get('plan')=='free' and not quota.get('error'):
            continue
        # A failed quota request does not prove that this account is exhausted.
        if not quota.get('valid'):return False
        weekly=quota.get('weekly_remaining')
        if not isinstance(weekly,(int,float)) or not math.isfinite(weekly) or weekly>0:
            return False
        supported+=1
    return supported>0

def reset_authorized(accounts):
    try:enabled=load_json(LOCALDATA/'config.json').get('included_resets',False)
    except (OSError,ValueError):enabled=False
    return bool(enabled and all_weekly_exhausted(accounts.records,accounts.quotas))

def save_operation(ledger,operation):
    atomic_json(ledger,operation)
    atomic_json(LOCALDATA/'reset-operations'/(operation['idempotencyKey']+'.json'),operation)

def compact(value):
    if isinstance(value,float) and value.is_integer(): value=int(value)
    if isinstance(value,list): value=[json.loads(compact(v)) for v in value]
    return json.dumps(value,ensure_ascii=False,separators=(',',':'))

def expected_scope(snapshot, account_id):
    account = next((a for a in snapshot['codex']['accounts'] if a['id']==account_id),None)
    limits = snapshot.get('rateLimits',{}).get('codex') or {}
    credits = limits.get('rateLimitResetCredits') or {}
    if not account or snapshot['codex'].get('activeAccountId') != account_id or limits.get('provider') != 'codex': return None
    if credits.get('availableCount',0) <= 0 or limits.get('status') != 'ok': return None
    exhausted=any(w and isinstance(w.get('usedPercent'),(int,float)) and w['usedPercent']>=98
                  for w in (limits.get('session'),limits.get('weekly')))
    if not exhausted:return None
    rows = [[c.get('status'),c.get('expiresAt'),c.get('grantedAt')] for c in credits.get('credits',[])]
    rows.sort(key=compact)
    window=lambda w: [w.get('usedPercent'),w.get('windowMinutes'),w.get('resetsAt')] if w else None
    revision='v1:'+compact([credits['availableCount'],credits.get('totalEarnedCount'),credits.get('nextExpiresAt'),rows,
                            window(limits.get('session')),window(limits.get('weekly')),limits.get('updatedAt')])
    if len(revision)>4096 or not isinstance(account.get('updatedAt'),int): return None
    return {'target':TARGET,'accountId':account_id,'accountRevision':account['updatedAt'],'offerRevision':revision}

def restore_allowance(accounts, records, reserved, threshold, available):
    rpc=getattr(accounts,'rpc',None)
    if rpc is None: return None
    ledger=LOCALDATA/'included-reset.json'
    try: previous=load_json(ledger)
    except (OSError,ValueError): previous={}
    if previous.get('state') in ('in-progress','uncertain'):
        raise RuntimeError('An included reset needs confirmation before another reset can be used')
    if previous.get('state') == 'awaiting-allowance':
        record = next((r for r in records if r['key'] == previous.get('key')), None)
        if record and available(accounts.probe(record,True),threshold):
            previous['state']='complete';atomic_json(ledger,previous)
            return record if record['key'] not in reserved else None
        raise RuntimeError('Waiting for the previously applied included reset; no additional reset will be used')
    if not reset_authorized(accounts):
        raise RuntimeError('Banked resets require every account to have verified 0% weekly allowance')
    # Cached zeroes are only a reason to check again, never to spend a reset.
    # Refresh every account, including accounts reserved by either application.
    for record in accounts.records:
        accounts.probe(record,True)
        if not reset_authorized(accounts):
            raise RuntimeError('Banked reset cancelled: an account has weekly allowance or an unconfirmed quota')
    snapshot=rpc.call('accounts.list',{'refreshUsage':False})
    active=snapshot['codex'].get('activeAccountId')
    offers={row['accountId']:row.get('rateLimits',{}).get('rateLimitResetCredits',{}).get('availableCount',0)
            for row in snapshot.get('rateLimits',{}).get('inactiveCodexAccounts',[])}
    offers[active]=snapshot.get('rateLimits',{}).get('codex',{}).get('rateLimitResetCredits',{}).get('availableCount',0)
    candidates=[r for r in records if r['key'] not in reserved and r.get('orca_id') and offers.get(r['orca_id'],0)>0
                and accounts.quotas.get(r['key'],{}).get('valid')]
    for record in candidates:
        # A concurrent natural reset makes spending a saved reset unnecessary.
        if available(accounts.probe(record,True),threshold):return record
        selected_id=record['orca_id'];operation=None
        try:
            rpc.call('accounts.selectCodexForTarget',{'accountId':selected_id,'target':TARGET})
            fresh=rpc.call('accounts.list',{'refreshUsage':True},timeout=65)
            scope=expected_scope(fresh,selected_id)
            if not scope:continue
            # Re-check the live preference and every account, including those in
            # use by the other app. A user can disable resets during this RPC.
            if not reset_authorized(accounts):
                raise RuntimeError('Banked reset cancelled: weekly exhaustion is not verified for all accounts')
            operation={'idempotencyKey':str(uuid.uuid4()),'expectedScope':scope,'key':record['key'],
                       'state':'in-progress','updated_at':time.time()}
            save_operation(ledger,operation)
            result=rpc.call('accounts.consumeCodexResetCredit',{'idempotencyKey':operation['idempotencyKey'],'expectedScope':scope},timeout=65)
            outcome=result.get('outcome');status=result.get('status')
            operation.update(outcome=outcome,status=status,state='awaiting-allowance' if outcome in ('reset','alreadyRedeemed') else
                             'complete' if outcome in ('noCredit','nothingToReset') or status=='rejectedBeforeProvider' else 'uncertain')
            save_operation(ledger,operation)
            if operation['state']=='uncertain':raise RuntimeError('Included reset outcome could not be confirmed')
            if outcome in ('reset','alreadyRedeemed'):
                for _ in range(3):
                    if available(accounts.probe(record,True),threshold):
                        operation['state']='complete';save_operation(ledger,operation)
                        return record
                    time.sleep(1)
                raise RuntimeError('Included reset was applied but allowance has not recovered yet')
        except Exception:
            if operation and operation['state']=='in-progress':
                operation['state']='uncertain';save_operation(ledger,operation)
            raise
        finally:
            current=rpc.call('accounts.list',{'refreshUsage':False})['codex'].get('activeAccountId')
            if active and current==selected_id and active!=selected_id:
                rpc.call('accounts.selectCodexForTarget',{'accountId':active,'target':TARGET})
    return None
