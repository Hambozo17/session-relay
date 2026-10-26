from __future__ import annotations
import os, pathlib, subprocess, sys, time, tkinter as tk
from tkinter import ttk
from accounts import LOCALDATA, ACCOUNT_REFRESH_SECONDS, atomic_json, load_json
from controller import CONFIG, STATUS, available, config
from local_rpc import HIDDEN
from watchdog import ensure_monitor

ROOT = pathlib.Path(__file__).resolve().parent
PANEL_VERSION = '1.0.19'


def window_geometry(root):
    left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        work = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(work), 0):
            left, top, right, bottom = work.left, work.top, work.right, work.bottom
    width, height = min(1140, right-left-48), min(780, bottom-top-72)
    return width, height, left+(right-left-width)//2, top+16

def ensure_service():
    task = subprocess.run(['schtasks.exe','/Run','/TN','SessionRelayMonitor'], capture_output=True, creationflags=HIDDEN)
    if task.returncode: ensure_monitor()

class Panel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('Session Relay · Orca & Codex')
        width, height, x, y = window_geometry(self.root)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.root.minsize(min(760, width), min(520, height))
        self.root.configure(bg='#101827')
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self.trees = {}; self.buttons = {}; self.messages = {}; self.selected = {}
        self.searches = {}; self.chat_rows = {}; self.hints = {}; self.cards = {}
        style = ttk.Style(); style.theme_use('clam')
        style.configure('Treeview', background='#1b2637', fieldbackground='#1b2637', foreground='#e3eaf5',
            rowheight=29, borderwidth=0)
        style.configure('Treeview.Heading', background='#29364b', foreground='#e3eaf5', padding=6)
        style.map('Treeview', background=[('selected', '#176c70')])
        style.configure('Relay.TNotebook', background='#101827', borderwidth=0, tabmargins=(0,0,0,6))
        style.configure('Relay.TNotebook.Tab', background='#29364b', foreground='#e3eaf5', padding=(18,9))
        style.map('Relay.TNotebook.Tab', background=[('selected','#176c70')], foreground=[('selected','#ffffff')],
            padding=[('selected',(18,9)),('!selected',(18,9))])

        header=tk.Frame(self.root,bg='#101827')
        header.grid(row=0,column=0,sticky='ew',padx=20,pady=(16,12))
        header.columnconfigure(0,weight=1)
        tk.Label(header,text='Session Relay',bg='#101827',fg='#f4f6fc',font=('Segoe UI',20,'bold')).grid(row=0,column=0,sticky='w')
        self.subtitle=tk.Label(header,text='Choose chats to monitor, then enable Orca or Codex.',
            bg='#101827',fg='#aebbd0',font=('Segoe UI',10),anchor='w',justify='left')
        self.subtitle.grid(row=1,column=0,sticky='ew',pady=(3,0))
        header.bind('<Configure>',lambda event:self.subtitle.configure(wraplength=max(160,event.width)))

        self.tabs=ttk.Notebook(self.root,style='Relay.TNotebook')
        self.tabs.grid(row=1,column=0,sticky='nsew',padx=20)
        self.chats_page=tk.Frame(self.tabs,bg='#101827')
        self.accounts_page=tk.Frame(self.tabs,bg='#101827')
        self.options_page=tk.Frame(self.tabs,bg='#101827')
        self.tabs.add(self.chats_page,text='Chats')
        self.tabs.add(self.accounts_page,text='Accounts')
        self.tabs.add(self.options_page,text='Options')
        self.chats_page.rowconfigure(0,weight=1)
        for column, app in enumerate(('orca','codex')):
            self.chats_page.columnconfigure(column,weight=1,uniform='apps')
            card=tk.Frame(self.chats_page,bg='#1b2637',padx=12,pady=12)
            card.grid(row=0,column=column,sticky='nsew',padx=(0,6) if column==0 else (6,0))
            card.columnconfigure(0,weight=1);card.rowconfigure(3,weight=1)
            self.cards[app]=card
            tk.Label(card,text='Orca' if app=='orca' else 'Codex Desktop',bg='#1b2637',fg='#fff',
                font=('Segoe UI',16,'bold')).grid(row=0,column=0,sticky='w')
            message=tk.Label(card,text='Connecting to monitor…',bg='#1b2637',fg='#aebbd0',
                wraplength=320,width=1,height=3,justify='left',anchor='nw',font=('Segoe UI',9))
            message.grid(row=1,column=0,sticky='ew',pady=(5,5));self.messages[app]=message
            search_row=tk.Frame(card,bg='#1b2637');search_row.grid(row=2,column=0,sticky='ew',pady=(0,8))
            search_row.columnconfigure(1,weight=1)
            tk.Label(search_row,text='Search',bg='#1b2637',fg='#d8e1ef',font=('Segoe UI',9)).grid(row=0,column=0,padx=(0,8))
            search=tk.StringVar();self.searches[app]=search
            ttk.Entry(search_row,textvariable=search,width=12).grid(row=0,column=1,sticky='ew')
            search.trace_add('write',lambda *_args,a=app:self.filter_chats(a))
            listing=tk.Frame(card,bg='#1b2637');listing.grid(row=3,column=0,sticky='nsew')
            listing.columnconfigure(0,weight=1);listing.rowconfigure(0,weight=1)
            tree=ttk.Treeview(listing,columns=('monitor','state','goal'),show='tree headings',selectmode='extended',height=4)
            tree.heading('#0',text='Chat');tree.heading('monitor',text='Monitor')
            tree.heading('state',text='Status');tree.heading('goal',text='Goal')
            tree.column('#0',width=215,minwidth=150)
            tree.column('monitor',width=68,minwidth=68,stretch=False,anchor='center')
            tree.column('state',width=105,minwidth=85,stretch=False)
            tree.column('goal',width=80,minwidth=75,stretch=False)
            scroll_y=ttk.Scrollbar(listing,orient='vertical',command=tree.yview)
            scroll_x=ttk.Scrollbar(listing,orient='horizontal',command=tree.xview)
            tree.configure(yscrollcommand=scroll_y.set,xscrollcommand=scroll_x.set)
            tree.grid(row=0,column=0,sticky='nsew');scroll_y.grid(row=0,column=1,sticky='ns')
            scroll_x.grid(row=1,column=0,sticky='ew');self.trees[app]=tree
            tree.bind('<Button-1>',lambda event,a=app:self.click_chat(a,event))
            tree.bind('<space>',lambda event,a=app:self.click_chat(a,event,keyboard=True))
            hint='Click a chat to turn monitoring on or off.'
            hint+=' Open saved chats in '+('Orca' if app=='orca' else 'Codex')+' to attach them.'
            label=tk.Label(card,text=hint,bg='#1b2637',fg='#8f9eb5',font=('Segoe UI',9),
                justify='left',anchor='w',height=2,width=1,wraplength=320)
            label.grid(row=4,column=0,sticky='ew',pady=(6,6));self.hints[app]=label
            button=tk.Button(card,text='Enable '+app.title(),command=lambda a=app:self.toggle(a),
                bg='#59d6b4',fg='#101827',activebackground='#91eacf',font=('Segoe UI',11,'bold'),
                relief='flat',pady=7)
            button.grid(row=5,column=0,sticky='ew');self.buttons[app]=button
            card.bind('<Configure>',lambda event,a=app:self.resize_card(a,event.width))

        self.accounts_page.columnconfigure(0,weight=1);self.accounts_page.rowconfigure(1,weight=1)
        self.account_heading=tk.Label(self.accounts_page,text='Saved accounts · refresh every 1 minute',
            bg='#101827',fg='#d8e1ef',font=('Segoe UI',12,'bold'),anchor='w',justify='left',width=1)
        self.account_heading.grid(row=0,column=0,sticky='ew',pady=(8,10))
        account_listing=tk.Frame(self.accounts_page,bg='#101827');account_listing.grid(row=1,column=0,sticky='nsew')
        account_listing.rowconfigure(0,weight=1);account_listing.columnconfigure(0,weight=1)
        self.accounts=ttk.Treeview(account_listing,columns=('source','five','week','login','checked'),show='tree headings',height=8)
        self.accounts.heading('#0',text='Account');self.accounts.column('#0',width=300,minwidth=220)
        for key,title,column_width in (('source','Saved in',120),('five','5h remaining',115),
                ('week','Weekly remaining',135),('login','Status',210),('checked','Checked',85)):
            self.accounts.heading(key,text=title)
            self.accounts.column(key,width=column_width,minwidth=column_width,stretch=key in ('source','login'))
        account_scroll_y=ttk.Scrollbar(account_listing,orient='vertical',command=self.accounts.yview)
        account_scroll_x=ttk.Scrollbar(account_listing,orient='horizontal',command=self.accounts.xview)
        self.accounts.configure(yscrollcommand=account_scroll_y.set,xscrollcommand=account_scroll_x.set)
        self.accounts.grid(row=0,column=0,sticky='nsew');account_scroll_y.grid(row=0,column=1,sticky='ns')
        account_scroll_x.grid(row=1,column=0,sticky='ew')
        self.accounts_page.bind('<Configure>',lambda event:self.account_heading.configure(wraplength=max(160,event.width)))

        self.options_page.columnconfigure(0,weight=1);self.options_page.rowconfigure(4,weight=1)
        tk.Label(self.options_page,text='Automation preferences',bg='#101827',fg='#f4f6fc',
            font=('Segoe UI',12,'bold')).grid(row=0,column=0,sticky='w',pady=(12,8))
        initial=config()
        self.resets=tk.BooleanVar(value=initial.get('included_resets',False))
        self.preserve_running=tk.BooleanVar(value=initial.get('codex',{}).get('preserve_running',False))
        self.option_controls=[]
        for row,(label,variable,option) in enumerate((
                ('Use included resets only when ALL accounts have 0% weekly allowance',self.resets,'included_resets'),
                ('Preserve all running Codex chats during app restart',self.preserve_running,'preserve_running')),start=1):
            check=tk.Checkbutton(self.options_page,text=label,variable=variable,
                command=lambda o=option,v=variable:self.preference(o,v.get()),bg='#101827',fg='#d8e1ef',
                activebackground='#101827',selectcolor='#29364b',font=('Segoe UI',10),anchor='w',justify='left')
            check.grid(row=row,column=0,sticky='ew',pady=4);self.option_controls.append(check)
        tk.Label(self.options_page,text='Recent activity',bg='#101827',fg='#f4f6fc',
            font=('Segoe UI',12,'bold')).grid(row=3,column=0,sticky='w',pady=(18,8))
        activity_frame=tk.Frame(self.options_page,bg='#101827');activity_frame.grid(row=4,column=0,sticky='nsew')
        activity_frame.columnconfigure(0,weight=1);activity_frame.rowconfigure(0,weight=1)
        self.activity=tk.Text(activity_frame,bg='#1b2637',fg='#d8e1ef',relief='flat',font=('Segoe UI',10),
            wrap='word',state='disabled',height=4,width=1,padx=10,pady=10)
        activity_scroll=ttk.Scrollbar(activity_frame,orient='vertical',command=self.activity.yview)
        self.activity.configure(yscrollcommand=activity_scroll.set)
        self.activity.grid(row=0,column=0,sticky='nsew');activity_scroll.grid(row=0,column=1,sticky='ns')
        self.activity_text=None
        self.options_page.bind('<Configure>',self.resize_options)

        self.log=tk.Label(self.root,text='Closing this window leaves monitoring running.',bg='#101827',fg='#aebbd0',
            justify='left',anchor='w',font=('Segoe UI',9),height=2,width=1)
        self.log.grid(row=2,column=0,sticky='ew',padx=20,pady=(8,10))
        self.root.bind('<Configure>',self.resize_footer)
        # Reserve space for every fixed control plus at least two chat rows.
        # Font scaling changes the required height; the table absorbs resizing.
        self.root.update_idletasks()
        minimum_height=max(520,self.root.winfo_reqheight()-58)
        self.root.minsize(min(760,width),min(minimum_height,height))
        self.refreshing=False
        self.root.after(300,self.refresh)

    def resize_card(self, app, width):
        wrap=max(120,width-28)
        self.messages[app].configure(wraplength=wrap)
        self.hints[app].configure(wraplength=wrap)

    def resize_options(self, event):
        for control in self.option_controls:control.configure(wraplength=max(160,event.width-36))

    def resize_footer(self, event):
        if event.widget is self.root:self.log.configure(wraplength=max(160,event.width-40))

    def render_chats(self, app, rows, setting):
        tree=self.trees[app]; selected=set(setting.get(app,{}).get('sessions',[]))
        query=self.searches[app].get().strip().casefold()
        rows=[r for r in rows if not query or query in (r.get('title','')+' '+r['id']).casefold()]
        ids={r['id'] for r in rows}
        for old in tree.get_children():
            if old not in ids:tree.delete(old)
        for row in rows:
            values=('☑' if row['id'] in selected else '☐',row['status'],row.get('goal_status') or '—')
            if tree.exists(row['id']):tree.item(row['id'],text=row['title'][:75],values=values)
            else:tree.insert('', 'end',iid=row['id'],text=row['title'][:75],values=values)
        chosen=[r['id'] for r in rows if r['id'] in selected]
        if set(tree.selection())!=set(chosen):tree.selection_set(chosen)

    def filter_chats(self, app):
        self.render_chats(app,self.chat_rows.get(app,[]),config())

    def click_chat(self, app, event, keyboard=False):
        tree=self.trees[app]
        sid=tree.focus() if keyboard else tree.identify_row(event.y)
        if not sid:return
        setting=config();part=setting.setdefault(app,{})
        selected=list(part.get('sessions',[]))
        if sid in selected:selected.remove(sid)
        else:selected.append(sid)
        part['sessions']=selected
        atomic_json(CONFIG,setting)
        tree.focus(sid)
        self.render_chats(app,self.chat_rows.get(app,[]),setting)
        return 'break'

    def select(self, app, event=None):
        if self.refreshing: return
        tree=self.trees[app]
        if event is not None:
            if getattr(event,'keysym',None):
                if event.keysym not in ('Up','Down','Home','End','space'):return
            elif not tree.identify_row(event.y):
                # Clicking blank space/header must not erase monitoring.
                tree.selection_set([sid for sid in config().get(app,{}).get('sessions',[]) if tree.exists(sid)])
                return
        setting=config();part=setting.setdefault(app,{})
        hidden = [sid for sid in part.get('sessions',[]) if not self.trees[app].exists(sid)]
        part['sessions']=hidden+list(self.trees[app].selection());atomic_json(CONFIG,setting)

    def preference(self, option, value):
        setting=config()
        if option=='preserve_running':setting.setdefault('codex',{})[option]=value
        else:setting[option]=value
        atomic_json(CONFIG,setting)

    def toggle(self, app):
        setting=config();part=setting.setdefault(app,{})
        selected=list(part.get('sessions',[]))
        if not part.get('enabled') and not selected:
            self.messages[app].configure(text='Select one or more chats first.');return
        part.update(enabled=not part.get('enabled',False),sessions=selected);atomic_json(CONFIG,setting)
        self.update_status()

    def refresh(self):
        self.update_status()
        self.root.after(2500,self.refresh)

    def update_status(self):
        self.refreshing=True
        try:
            status=load_json(STATUS);setting=config()
            try:threshold=max(0,min(20,float(setting.get('threshold',2))))
            except (TypeError,ValueError):threshold=2
            self.resets.set(setting.get('included_resets',False))
            self.preserve_running.set(setting.get('codex',{}).get('preserve_running',False))
            for app,tree in self.trees.items():
                rows=status.get('selectable_sessions',{}).get(app,status.get('sessions',{}).get(app,[]))
                rows=list(rows)
                known={r['id'] for r in rows}
                previous={r['id']:r for r in self.chat_rows.get(app,[])}
                for sid in setting.get(app,{}).get('sessions',[]):
                    if sid not in known:
                        rows.append({'id':sid,'title':previous.get(sid,{}).get('title') or 'Selected chat '+sid[:8],
                            'status':'Waiting to attach','goal_status':None,'attached':False})
                self.chat_rows[app]=rows
                self.render_chats(app,rows,setting)
                enabled=setting.get(app,{}).get('enabled',False)
                self.buttons[app].configure(text=('Disable ' if enabled else 'Enable ')+('Orca' if app=='orca' else 'Codex'),bg='#e5ad73' if enabled else '#59d6b4')
                count=len(setting.get(app,{}).get('sessions',[]))
                self.messages[app].configure(text=status.get('messages',{}).get(app,'Waiting')+f' · {count} selected')
            account_ids={row['key'] for row in status.get('accounts',[])}
            for old in self.accounts.get_children():
                if old not in account_ids:self.accounts.delete(old)
            interval=status.get('refresh',{}).get('interval_seconds',ACCOUNT_REFRESH_SECONDS)
            minutes=interval//60
            self.account_heading.configure(text=f'Saved accounts · refresh every {minutes} minute'+('s' if minutes!=1 else '')+
                (' · refreshing…' if status.get('refresh',{}).get('running') else ''))
            for row in status.get('accounts',[]):
                percent=lambda value:'—' if value is None else f'{value:g}%'
                fallback=row.get('fallback_only') or row.get('weekly_only')
                label=row.get('error') or ('Weekly fallback' if fallback else 'Available' if row.get('valid') else 'No supported allowance')
                if row.get('valid') and not available(row,threshold):label='Waiting for allowance reset'
                if fallback and row.get('weekly_remaining') == 0:label='Weekly fallback · exhausted'
                checked=time.strftime('%H:%M:%S',time.localtime(row['checked_at'])) if row.get('checked_at') else '—'
                values=(', '.join(row['sources']),'Weekly only' if row.get('weekly_only') else percent(row.get('five_remaining')),percent(row.get('weekly_remaining')),label,checked)
                if self.accounts.exists(row['key']):self.accounts.item(row['key'],text=row['label'],values=values)
                else:self.accounts.insert('', 'end',iid=row['key'],text=row['label'],values=values)
            logs=status.get('logs',[])
            self.log.configure(text='Monitoring continues when this window is closed. Open Options for recent activity.')
            activity='\n\n'.join(l['time']+'  '+l['message'] for l in logs) or 'No recent activity.'
            if activity != self.activity_text:
                position=self.activity.yview()
                at_bottom=not position or position[1]>=.99
                self.activity.configure(state='normal');self.activity.delete('1.0','end')
                self.activity.insert('1.0',activity);self.activity.configure(state='disabled')
                if at_bottom:self.activity.see('end')
                else:self.activity.yview_moveto(position[0])
                self.activity_text=activity
        except (OSError,ValueError,KeyError): pass
        finally:
            self.refreshing=False

if __name__=='__main__':
    if os.name=='nt':
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.CreateMutexW.restype=ctypes.c_void_p
        panel_mutex=kernel.CreateMutexW(None,False,'Local\\SessionRelayPanel')
        if ctypes.get_last_error()==183:
            window = ctypes.windll.user32.FindWindowW(None,'Session Relay · Orca & Codex')
            ctypes.windll.user32.ShowWindow(window,9)
            ctypes.windll.user32.SetForegroundWindow(window)
            raise SystemExit(0)
    ensure_service();panel=Panel();panel.root.mainloop()
