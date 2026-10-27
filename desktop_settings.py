"""Fingerprint migration for exact, verified built-in Desktop mode templates."""
import copy
import hashlib
import json
import ntpath
import os
import pathlib
import re

DEFAULT_INSTRUCTIONS = json.loads(pathlib.Path(__file__).with_name('desktop-settings-policy.json').read_text(encoding='utf-8'))['default_instructions']


def builtin_default(settings):
    mode = settings.get('collaborationMode') or {}
    text = (mode.get('settings') or {}).get('developer_instructions')
    return mode.get('mode') == 'default' and isinstance(text,str) and text.replace('\r\n','\n') in DEFAULT_INSTRUCTIONS


def git_workspace_identity(root):
    """Identify an exact Git main/worktree root; never widen restricted policies."""
    if not isinstance(root,str) or not pathlib.Path(root).is_absolute(): return None
    try:
        marker=pathlib.Path(root)/'.git'
        if marker.is_dir(): common=marker.resolve(strict=True)
        elif marker.is_file() and marker.stat().st_size<=4096:
            text=marker.read_text(encoding='utf-8').strip()
            if not text.startswith('gitdir: '):return None
            admin=(marker.parent/text[8:]).resolve(strict=True)
            link=admin/'commondir';back=admin/'gitdir'
            if not link.is_file() or not back.is_file() or max(link.stat().st_size,back.stat().st_size)>4096:return None
            common=(admin/link.read_text(encoding='utf-8').strip()).resolve(strict=True)
            if (admin.parent!=common/'worktrees' or
                    pathlib.Path(back.read_text(encoding='utf-8').strip()).resolve(strict=True)!=marker.resolve(strict=True)):
                return None
        else:return None
        if not common.is_dir() or not (common/'HEAD').is_file() or not (common/'config').is_file():return None
        return os.path.normcase(str(common)).replace('\\','/').lower()
    except (OSError,ValueError,UnicodeError):return None


def fingerprint(settings, sid, *, normalize_builtin=True, normalize_effort=True, normalize_roots=True, normalize_git_roots=True):
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
                if normalize_git_roots:
                    roots=[('git-common:'+identity) if (identity:=git_workspace_identity(r)) else r
                        for r in holder['runtimeWorkspaceRoots']]
                    holder['runtimeWorkspaceRoots']=[r for i,r in enumerate(roots)
                        if not isinstance(r,str) or not r.startswith('git-common:') or r not in roots[:i]]
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def migrate_operation(operation):
    changed=False
    for sid,context in operation.get('recovery_context',{}).items():
        snapshot=context.get('settings_snapshot')
        if not snapshot:continue
        old=context.get('settings')
        new=fingerprint(snapshot,sid)
        if old == new or old not in {fingerprint(snapshot,sid,normalize_builtin=b,normalize_effort=e,normalize_roots=r,normalize_git_roots=g)
                for b in (False,True) for e in (False,True) for r in (False,True) for g in (False,True)}:continue
        context['settings']=new
        context['settings_normalization']='Exact built-in Default template, same-model implicit effort, and verified Git main/worktree or redundant full-access roots; raw snapshot and selected settings preserved.'
        receipt=operation.get('interruption_receipts',{}).get(sid)
        if receipt and receipt.get('settings')==old:receipt['settings']=context['settings']
        metadata=operation.get('metadata',{})
        receipt=metadata.get('originals',{}).get(sid)
        if receipt and receipt.get('settings')==old:receipt['settings']=context['settings']
        if metadata.get('session')==sid and metadata.get('settings')==old:metadata['settings']=context['settings']
        changed=True
    return changed
