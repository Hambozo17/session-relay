"""Fingerprint migration for exact, verified built-in Desktop mode templates."""
import copy
import hashlib
import json
import ntpath
import pathlib
import re

DEFAULT_INSTRUCTIONS = json.loads(pathlib.Path(__file__).with_name('desktop-settings-policy.json').read_text(encoding='utf-8'))['default_instructions']


def builtin_default(settings):
    mode = settings.get('collaborationMode') or {}
    text = (mode.get('settings') or {}).get('developer_instructions')
    return mode.get('mode') == 'default' and isinstance(text,str) and text.replace('\r\n','\n') in DEFAULT_INSTRUCTIONS


def fingerprint(settings, sid, *, normalize_builtin=True, normalize_effort=True, normalize_roots=True):
    value = copy.deepcopy(settings)
    mode_settings = (value.get('collaborationMode') or {}).get('settings') or {}
    if normalize_effort and value.get('effort') is None and mode_settings.get('model') == value.get('model') and isinstance(mode_settings.get('reasoning_effort'),str):
        value['effort'] = mode_settings['reasoning_effort']
    if normalize_builtin and builtin_default(value):
        value['collaborationMode']['settings']['developer_instructions'] = None
    permissions = value.get('permissions') or {}
    sandbox = permissions.get('sandboxPolicy') or value.get('sandboxPolicy') or {}
    if sandbox.get('type') in ('dangerFullAccess','danger-full-access'):
        normalize = lambda path: re.sub('/+','/',str(path).replace('\\','/')).lower()
        prefix = normalize(pathlib.Path.home()/'.codex/visualizations') + '/'
        def generated(root):
            text=normalize(root)
            tail=text[len(prefix):].rstrip('/') if text.startswith(prefix) else ''
            return isinstance(root,str) and re.fullmatch(r'\d{4}/\d{2}/\d{2}/'+re.escape(sid.lower()),tail) is not None
        for holder in (value,permissions):
            if isinstance(holder.get('runtimeWorkspaceRoots'),list):
                holder['runtimeWorkspaceRoots']=[r for r in holder['runtimeWorkspaceRoots'] if not generated(r)]
                if normalize_roots:
                    def absolute(root):
                        if not isinstance(root,str):return None
                        text=ntpath.normpath(root).replace('\\','/').lower().rstrip('/')
                        return text if re.match(r'^[a-z]:/',text) or re.fullmatch(r'[a-z]:',text) and len(root)>2 else None
                    roots=holder['runtimeWorkspaceRoots'];keys=[absolute(root) for root in roots]
                    holder['runtimeWorkspaceRoots']=[root for root,key in zip(roots,keys)
                        if not key or not any(parent and key!=parent and key.startswith(parent+'/') for parent in keys)]
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def migrate_operation(operation):
    changed=False
    for sid,context in operation.get('recovery_context',{}).items():
        snapshot=context.get('settings_snapshot')
        if not snapshot:continue
        old=context.get('settings')
        new=fingerprint(snapshot,sid)
        if old == new or old not in {fingerprint(snapshot,sid,normalize_builtin=b,normalize_effort=e,normalize_roots=r)
                for b in (False,True) for e in (False,True) for r in (False,True)}:continue
        context['settings']=new
        context['settings_normalization']='Exact built-in Default template, same-model implicit effort, and redundant full-access descendant roots; raw snapshot and selected settings preserved.'
        receipt=operation.get('interruption_receipts',{}).get(sid)
        if receipt and receipt.get('settings')==old:receipt['settings']=context['settings']
        metadata=operation.get('metadata',{})
        receipt=metadata.get('originals',{}).get(sid)
        if receipt and receipt.get('settings')==old:receipt['settings']=context['settings']
        if metadata.get('session')==sid and metadata.get('settings')==old:metadata['settings']=context['settings']
        changed=True
    return changed
