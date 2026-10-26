from __future__ import annotations
import base64, ctypes, hashlib, json, math, os, pathlib, queue, sqlite3, subprocess, threading, time, tomllib, uuid
from ctypes import wintypes
from contextlib import contextmanager
from local_rpc import HIDDEN, OrcaRpc

APPDATA = pathlib.Path(os.environ.get('APPDATA', pathlib.Path.home()))
LOCALDATA = pathlib.Path(os.environ.get('LOCALAPPDATA', pathlib.Path.home())) / 'SessionRelay'
REAL_HOME = pathlib.Path.home() / '.codex'
ACCOUNT_REFRESH_SECONDS = 60

def plain_path(value):
    value = str(value)
    if value.startswith('\\\\?\\UNC\\'): value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'): value = value[4:]
    return pathlib.Path(value)

@contextmanager
def database_connection(*args, **kwargs):
    connection = sqlite3.connect(*args, **kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()

def load_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8-sig'))

def atomic_json(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8', newline='\n') as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.flush(); os.fsync(file.fileno())
        # Windows readers (including the panel and antivirus) can briefly deny
        # delete sharing. Keep the old complete document until replacement works.
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 9: raise
                time.sleep(min(.01 * 2**attempt, .25))
    finally:
        tmp.unlink(missing_ok=True)

def inherit_project_trust(source_home, target_home, cwd):
    source = pathlib.Path(source_home) / 'config.toml'
    target = pathlib.Path(target_home) / 'config.toml'
    original = tomllib.loads(source.read_text(encoding='utf-8-sig'))
    normalize = lambda p: os.path.normcase(os.path.normpath(p))
    trusted = [p for p, settings in original.get('projects', {}).items()
        if settings.get('trust_level') == 'trusted' and (normalize(cwd) == normalize(p) or normalize(cwd).startswith(normalize(p) + os.sep))]
    if not trusted: raise RuntimeError('The original project trust decision cannot be verified')
    content = target.read_text(encoding='utf-8-sig') if target.exists() else ''
    config = tomllib.loads(content)
    key = max(trusted, key=len)
    current = config.get('projects', {}).get(key)
    if current and current.get('trust_level') != 'trusted': raise RuntimeError('The destination has a different project trust decision')
    if current: return
    # A JSON quoted basic string is also a valid TOML key for Windows paths.
    content += '\n[projects.' + json.dumps(key) + ']\ntrust_level = "trusted"\n'
    tomllib.loads(content)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        os.replace(temporary, target)
    finally: temporary.unlink(missing_ok=True)

def protected_json(path):
    data = load_json(path)
    if not isinstance(data, dict) or data.get('format') != 'codexbar.secure-file':
        return data
    if data.get('version') != 1 or data.get('protection') not in ('windows-dpapi-user', 'windows-dpapi-machine'):
        raise ValueError('Unsupported protected account store')
    if os.name != 'nt':
        raise ValueError('Windows account protection is required')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    raw = base64.b64decode(data['payload'])
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, output = Blob(len(raw), buf), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError('Could not open this Windows account store')
    try:
        return json.loads(ctypes.string_at(output.data, output.size).decode('utf-8'))
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))

def jwt_claims(token):
    try:
        part = token.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))
    except (ValueError, KeyError, IndexError, AttributeError):
        return {}

def identity(auth):
    tokens = auth.get('tokens') or {}
    claims = jwt_claims(tokens.get('id_token', ''))
    access = jwt_claims(tokens.get('access_token', ''))
    profile = claims.get('https://api.openai.com/profile', {})
    scope = access.get('https://api.openai.com/auth', {})
    subject = claims.get('sub') or access.get('sub') or scope.get('user_id')
    account_id = tokens.get('account_id') or scope.get('chatgpt_account_id')
    email = claims.get('email') or profile.get('email')
    if not subject or not account_id or not tokens.get('access_token'):
        return None
    key = hashlib.sha256((str(subject) + ':' + str(account_id)).encode()).hexdigest()[:24]
    return {'key': key, 'subject': subject, 'account_id': account_id, 'label': email or 'Saved account'}

def codex_binary():
    root = APPDATA / 'npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe'
    if root.exists():
        return str(root)
    candidates = list((LOCALDATA.parent / 'OpenAI/Codex/bin').glob('*/codex.exe'))
    if not candidates:
        raise RuntimeError('Codex CLI executable is missing')
    return str(max(candidates, key=lambda p: p.stat().st_mtime))

class Probe:
    """A short-lived app-server; no model turn or tool operation is started."""
    def __init__(self, home):
        self.home = pathlib.Path(home)

    def call(self, calls):
        env = dict(os.environ, CODEX_HOME=str(self.home))
        env.pop('ELECTRON_RUN_AS_NODE', None)
        process = subprocess.Popen([codex_binary(), 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env, creationflags=HIDDEN)
        from helper_job import HelperJob
        try: job = HelperJob(process)
        except Exception:
            process.terminate(); process.wait(timeout=5)
            for stream in (process.stdin, process.stdout): stream.close()
            raise
        messages = queue.Queue()
        def read():
            try:
                for line in process.stdout:
                    try: messages.put(json.loads(line))
                    except ValueError: pass
            finally:
                messages.put({'closed': True})
                process.stdout.close()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        counter = 0
        def request(method, params=None):
            nonlocal counter
            counter += 1
            req = {'id': counter, 'method': method}
            if params is not None: req['params'] = params
            process.stdin.write(json.dumps(req) + '\n'); process.stdin.flush()
            deadline = time.monotonic() + 35
            while time.monotonic() < deadline:
                try: msg = messages.get(timeout=min(1, max(.01, deadline-time.monotonic())))
                except queue.Empty: continue
                if msg.get('closed'): raise RuntimeError('Codex account probe disconnected')
                if msg.get('id') != counter: continue
                if 'error' in msg: raise RuntimeError('Codex refused this account probe')
                return msg.get('result')
            raise TimeoutError('Codex account probe timed out')
        try:
            request('initialize', {'clientInfo': {'name': 'session_relay', 'version': '1.0.0'}, 'capabilities': {'experimentalApi': True}})
            process.stdin.write('{"method":"initialized"}\n'); process.stdin.flush()
            return [request(method, params) for method, params in calls]
        finally:
            job.close()
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait()
            for file in (process.stdin, process.stdout):
                # A configured child can inherit the helper's stdout pipe. Closing
                # that stream while the reader owns its lock freezes the monitor.
                # The reader owns final cleanup if it has not reached EOF yet.
                if file is process.stdout:
                    reader.join(timeout=.5)
                    if reader.is_alive(): continue
                if file: file.close()

def quota_windows(raw, weekly_fallback=False):
    windows = raw.get('rateLimits') or {}
    mapped = raw.get('rateLimitsByLimitId') or {}
    if 'codex' in mapped: windows = mapped['codex']
    plan = windows.get('planType') or raw.get('planType')
    result = {'valid': True, 'plan': plan, 'five_remaining': None, 'weekly_remaining': None, 'reset_at': None, 'updated_at': time.time()}
    for key in ('primary', 'secondary'):
        window = windows.get(key)
        if not isinstance(window, dict): continue
        minutes = window.get('windowDurationMins', window.get('window_duration_mins'))
        used = window.get('usedPercent', window.get('used_percent'))
        if not isinstance(used, (int, float)) or not 0 <= used <= 100: continue
        if minutes == 300:
            result['five_remaining'] = 100 - used
            result['reset_at'] = window.get('resetsAt')
        elif minutes == 10080:
            result['weekly_remaining'] = 100 - used
            result['weekly_reset_at'] = window.get('resetsAt')
    result['weekly_only'] = (plan == 'pro' or weekly_fallback) and result['five_remaining'] is None and result['weekly_remaining'] is not None
    result['fallback_only'] = bool(weekly_fallback or result['weekly_only'])
    if result['five_remaining'] is None and not result['weekly_only']: result['valid'] = False
    if result['weekly_remaining'] is None: result['valid'] = False
    ordinary = windows.get('ordinaryUsageAllowed', windows.get('ordinary_allowed',
        raw.get('ordinaryUsageAllowed', raw.get('ordinary_allowed', True))))
    result['ordinary_allowed'] = ordinary is not False
    return result


def normalize_threshold(value=2):
    """Normalize the user recovery threshold to the monitor's supported range."""
    try:
        threshold = float(value)
    except (TypeError, ValueError, OverflowError):
        return 2.0
    if not math.isfinite(threshold):
        return 2.0
    return max(0.0, min(20.0, threshold))


def _quota_values_complete(quota):
    if not isinstance(quota, dict) or quota.get('valid') is not True:
        return False
    weekly = quota.get('weekly_remaining')
    if not isinstance(weekly, (int, float)) or isinstance(weekly, bool) or not math.isfinite(weekly):
        return False
    if quota.get('weekly_only') is True:
        return True
    five = quota.get('five_remaining')
    return (isinstance(five, (int, float)) and not isinstance(five, bool)
        and math.isfinite(five))


def _quota_capacity_state(quota, threshold=2):
    """Return depleted/usable only for a complete, verified supported quota."""
    if not _quota_values_complete(quota):
        return None
    threshold = normalize_threshold(threshold)
    weekly = quota.get('weekly_remaining')
    if weekly <= threshold:
        return 'depleted'
    weekly_only=quota.get('weekly_only') is True
    if not weekly_only:
        five = quota.get('five_remaining')
        if five <= threshold:
            return 'depleted'
    if quota.get('ordinary_allowed', True) is not True or str(quota.get('plan', '')).lower() == 'free':
        return None
    return 'usable'


def _carry_allowance_generation(previous, result, threshold=2):
    """Add durable reset evidence without changing quota fields on failure."""
    previous = previous if isinstance(previous, dict) else {}
    result = dict(result)
    threshold = normalize_threshold(threshold)
    generation = previous.get('_allowance_generation', 0)
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        generation = 0
    depleted = previous.get('_allowance_depleted')
    armed_threshold = previous.get('_allowance_depleted_threshold')
    if isinstance(armed_threshold, (int, float)) and not isinstance(armed_threshold, bool) and math.isfinite(armed_threshold):
        armed_threshold = normalize_threshold(armed_threshold)
    else:
        armed_threshold = None
    legacy_zero_proof = depleted is True and armed_threshold is None
    if legacy_zero_proof:
        armed_threshold = 0.0
    previous_state = _quota_capacity_state(previous, threshold)
    previous_complete = _quota_values_complete(previous)

    if not isinstance(depleted, bool):
        # Older quota-cache entries have no generation metadata. Reconstruct
        # only a verified depleted state; invalid and partial records stay unknown.
        depleted = True if previous_state == 'depleted' else False if previous_state == 'usable' else None
        armed_threshold = threshold if depleted is True else None
    elif previous_complete:
        # Re-evaluate complete cached usage at the current threshold. This keeps
        # a settings change from looking like a quota reset in either direction.
        if previous_state == 'depleted':
            depleted = True
            if not legacy_zero_proof:
                armed_threshold = threshold
        elif previous_state == 'usable':
            depleted = False
            armed_threshold = None
        elif armed_threshold != threshold and not legacy_zero_proof:
            # A complete but unclassifiable record (for example, ordinary use
            # blocked above threshold) cannot safely carry a proof across a
            # threshold change.
            depleted = None
            armed_threshold = None
    elif depleted is True:
        if legacy_zero_proof:
            # Before threshold metadata existed, a true flag could only have
            # been armed by a literal zero and is valid at every supported
            # threshold, including when the latest probe is invalid.
            armed_threshold = 0.0
        elif armed_threshold is None or armed_threshold > threshold:
            depleted = None
            armed_threshold = None

    state = _quota_capacity_state(result, threshold)
    if state == 'depleted':
        depleted = True
        armed_threshold = threshold
    elif state == 'usable':
        if depleted:
            generation += 1
        depleted = False
        armed_threshold = None
    result['_allowance_generation'] = generation
    result['_allowance_depleted'] = depleted
    if depleted is True:
        result['_allowance_depleted_threshold'] = armed_threshold
    else:
        result.pop('_allowance_depleted_threshold', None)
    return result


def _configured_threshold():
    try:
        saved = load_json(LOCALDATA / 'config.json')
        value = saved.get('threshold', 2) if isinstance(saved, dict) else 2
    except (OSError, ValueError, TypeError):
        value = 2
    return normalize_threshold(value)

class AccountManager:
    def __init__(self, rpc=None):
        self.rpc = rpc or OrcaRpc()
        self.records = []
        try: self.quotas = load_json(LOCALDATA / 'quota-cache.json')
        except (OSError, ValueError): self.quotas = {}
        self.lock = threading.RLock()
        self.probe_locks = {}

    def inventory(self):
        records = []
        try:
            store = protected_json(APPDATA / 'CodexBar/codex-accounts/accounts.json')
            for account in store.get('accounts', []):
                home = account.get('codexHomePath') or account.get('codex_home_path')
                if home: records.append({'id': str(account.get('id')), 'home': str(home), 'sources': ['CodexBar']})
        except (OSError, ValueError, KeyError): pass
        try:
            snapshot = self.rpc.call('accounts.list', {'refreshUsage': False})
            for account in snapshot.get('codex', {}).get('accounts', []):
                if account.get('managedHomeRuntime') not in ('host', None): continue
                home = APPDATA / 'orca/codex-accounts' / account['id'] / 'home'
                records.append({'id': account['id'], 'orca_id': account['id'], 'home': str(home), 'sources': ['Orca']})
        except Exception:
            # Reuse only a previously discovered inventory, re-reading each auth
            # below. This preserves saved Orca-only accounts when its UI is closed.
            try:
                records.extend(r for r in load_json(LOCALDATA/'account-inventory.json')
                    if isinstance(r,dict) and r.get('home') and r.get('orca_id') and 'Orca' in r.get('sources',[]))
            except (OSError, ValueError, TypeError): pass
        unique = {}
        for record in records:
            try:
                home = pathlib.Path(record['home']).resolve(strict=True)
                if home.is_symlink() or not home.is_dir(): continue
                record['home'] = str(home)
                verified = identity(protected_json(home / 'auth.json'))
                if not verified: continue
                record.update(verified, valid=True)
                existing = unique.get(record['key'])
                if existing:
                    existing['sources'] = sorted(set(existing['sources'] + record['sources']))
                    if record.get('orca_id'):
                        existing['orca_id'] = record['orca_id']; existing['orca_home'] = record['home']
                    existing.setdefault('homes', []).append(record['home'])
                else:
                    record['homes'] = [record['home']]
                    if record.get('orca_id'): record['orca_home'] = record['home']
                    unique[record['key']] = record
            except (OSError, ValueError): continue
        with self.lock:
            self.records = list(unique.values())
            atomic_json(LOCALDATA/'account-inventory.json',self.records)
        return self.records

    def current_desktop_key(self):
        try:
            current = identity(protected_json(REAL_HOME / 'auth.json'))
            return current['key'] if current else None
        except (OSError, ValueError): return None

    def find(self, key):
        return next((a for a in self.records if a['key'] == key), None)

    def weekly_fallback(self, record):
        try: return record['key'] in load_json(LOCALDATA/'config.json').get('weekly_only_accounts', [])
        except (OSError, ValueError, TypeError): return False

    def probe(self, record, force=False, home=None):
        with self.lock:
            guard = self.probe_locks.setdefault(record['key'], threading.Lock())
        with guard:
            return self._probe(record, force, home)

    def _probe(self, record, force=False, home=None):
        with self.lock:
            cached = self.quotas.get(record['key'])
            if cached and not force and time.time() - cached['updated_at'] < ACCOUNT_REFRESH_SECONDS: return cached
        try:
            home = pathlib.Path(home or record.get('orca_home') or record['home'])
            raw, check = Probe(home).call([('account/rateLimits/read', None), ('account/read', {'refreshToken': False})])
            verified = identity(protected_json(home / 'auth.json'))
            if not verified or verified['key'] != record['key']: raise ValueError('Account identity changed')
            if raw.get('accountId') and raw['accountId'] != verified['account_id']: raise ValueError('Service account identity mismatch')
            # A missing five-hour window is allowed only for a verified weekly
            # plan or an account explicitly designated by the user. Missing
            # weekly usage or a failed login can never imply usable allowance.
            account = check.get('account') if isinstance(check, dict) else None
            if not raw.get('planType') and isinstance(account, dict):
                raw = dict(raw, planType=account.get('planType'))
            result = quota_windows(raw, self.weekly_fallback(record))
            result['error'] = None
        except Exception:
            result = {'valid': False, 'updated_at': time.time(), 'error': 'Login or quota check failed', 'five_remaining': None, 'weekly_remaining': None}
            if cached and cached.get('plan'): result['plan'] = cached['plan']
            result['fallback_only'] = self.weekly_fallback(record)
        return self._store_probe_result(record['key'], result, cached)

    def _store_probe_result(self, key, result, previous=None):
        with self.lock:
            if previous is None:
                previous = self.quotas.get(key)
            result = _carry_allowance_generation(previous, result, _configured_threshold())
            self.quotas[key] = result
            atomic_json(LOCALDATA / 'quota-cache.json', self.quotas)
            return result

    def _desktop_auth(self, record):
        auth = protected_json(pathlib.Path(record['home']) / 'auth.json')
        verified = identity(auth)
        if not verified or verified['key'] != record['key']: raise RuntimeError('Destination login changed')
        cached = LOCALDATA / 'desktop-auth' / (record['key'] + '.json')
        if cached.exists():
            saved = protected_json(cached)
            check = identity(saved)
            if check and check['key']==record['key'] and (saved.get('last_refresh') or '')>(auth.get('last_refresh') or ''):
                auth=saved
        return auth

    def probe_desktop(self, record, force=True):
        # Desktop destination checks run in Desktop-owned homes. Reading a saved
        # Orca login must not cause a probe to rewrite that Orca profile.
        with self.lock:guard=self.probe_locks.setdefault(record['key'],threading.Lock())
        with guard:
            home=LOCALDATA/'desktop-probes'/record['key']
            try:
                auth=self._desktop_auth(record)
                atomic_json(home/'auth.json',auth)
                quota=self._probe(record,force,home)
                refreshed=protected_json(home/'auth.json');check=identity(refreshed)
                if check and check['key']==record['key']:
                    protect_backup(LOCALDATA/'desktop-auth'/(record['key']+'.json'),json.dumps(refreshed).encode('utf-8'))
                return quota
            except Exception:
                result={'valid':False,'updated_at':time.time(),'error':'Login or quota check failed','five_remaining':None,'weekly_remaining':None}
                return self._store_probe_result(record['key'], result)

    def activate_desktop(self, record):
        # The Desktop adapter must have already confirmed normal application exit.
        known = self.find(record['key'])
        if not known or record['home'] not in known['homes']: raise RuntimeError('Unknown destination account')
        auth = self._desktop_auth(record)
        verified = identity(auth)
        if not verified or verified['key'] != record['key']: raise RuntimeError('Destination login changed')
        backup = LOCALDATA / 'auth-backups'
        backup.mkdir(parents=True, exist_ok=True)
        current = REAL_HOME / 'auth.json'
        if current.exists():
            # Keep the pre-switch auth on this user's machine, protected with DPAPI.
            protect_backup(backup / ('auth-' + str(int(time.time())) + '-' + uuid.uuid4().hex[:6] + '.json'), current.read_bytes())
            outgoing = protected_json(current)
            outgoing_identity = identity(outgoing)
            if outgoing_identity:
                # Keep refreshed credentials in Desktop-owned storage only.
                # A Desktop switch never writes an Orca account's auth file.
                protect_backup(LOCALDATA/'desktop-auth'/(outgoing_identity['key']+'.json'),current.read_bytes())
        atomic_json(current, auth)
        if self.current_desktop_key() != record['key']: raise RuntimeError('Desktop account activation did not verify')
        return record['key']

def protect_backup(path, raw):
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, output = Blob(len(raw), buf), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    if not crypt.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise RuntimeError('Could not protect the authentication backup')
    try:
        atomic_json(path, {'format': 'codexbar.secure-file', 'version': 1, 'protection': 'windows-dpapi-user',
            'payload': base64.b64encode(ctypes.string_at(output.data, output.size)).decode()})
    finally:
        kernel = ctypes.WinDLL('kernel32'); kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))

def read_goal(home, session_id):
    database = plain_path(home) / 'goals_1.sqlite'
    if not database.exists(): return None
    try:
        with database_connection(database.as_uri() + '?mode=ro', uri=True, timeout=2) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT * FROM thread_goals WHERE thread_id=?', (session_id,)).fetchone()
            return dict(row) if row else None
    except sqlite3.Error as exc: raise RuntimeError('Goal state is unavailable; recovery is paused') from exc

def transfer_goal(source_home, target_home, session_id):
    source = plain_path(source_home) / 'goals_1.sqlite'
    target = plain_path(target_home) / 'goals_1.sqlite'
    if not source.exists() or source.resolve() == target.resolve(): return
    goal = read_goal(source_home, session_id)
    if not goal: return
    # Copy only this stopped session's Goal and preserve its exact accounting.
    with database_connection(source.as_uri() + '?mode=ro', uri=True) as old:
        schema = old.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL").fetchall()
        with database_connection(target, timeout=5) as new:
            new.execute('BEGIN IMMEDIATE')
            existing_tables = {row[0] for row in new.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name, sql in schema:
                if name not in existing_tables and name != 'sqlite_sequence': new.execute(sql)
            if '_sqlx_migrations' not in existing_tables:
                for row in old.execute('SELECT * FROM _sqlx_migrations'):
                    new.execute('INSERT INTO _sqlx_migrations VALUES (' + ','.join('?' for _ in row) + ')', row)
            columns = [row[1] for row in new.execute('PRAGMA table_info(thread_goals)')]
            if set(columns) != set(goal): raise RuntimeError('Goal database version mismatch')
            current = new.execute('SELECT goal_id,tokens_used FROM thread_goals WHERE thread_id=?', (session_id,)).fetchone()
            if current and (current[0] != goal['goal_id'] or current[1] > goal['tokens_used']):
                raise RuntimeError('The target contains a newer or different Goal')
            new.execute('INSERT OR REPLACE INTO thread_goals ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', [goal[c] for c in columns])
