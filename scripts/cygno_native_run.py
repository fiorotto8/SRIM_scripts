#!/usr/bin/env python3
"""Supervise one owned native TRIM process and its documented completion dialog.

SRIM mode 5 displays 'End of TRIM.DAT calculation' even with TRIMAUTO=1.
Acknowledge only that exact dialog after its printed completed count agrees
with requested histories; never acknowledge errors or unrelated applications.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
from pathlib import Path
import re
import subprocess
import time


def process_windows(pid):
    user=ctypes.windll.user32
    callback_type=ctypes.WINFUNCTYPE(wt.BOOL,wt.HWND,wt.LPARAM)
    user.GetWindowTextW.argtypes=[wt.HWND,wt.LPWSTR,ctypes.c_int]
    user.GetClassNameW.argtypes=[wt.HWND,wt.LPWSTR,ctypes.c_int]
    result=[]
    def read(hwnd):
        text=ctypes.create_unicode_buffer(4096);user.GetWindowTextW(hwnd,text,4096)
        return text.value
    def inspect(hwnd,parameter):
        owner=wt.DWORD();user.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        cls=ctypes.create_unicode_buffer(200);user.GetClassNameW(hwnd,cls,200)
        if owner.value==pid and cls.value=='#32770' and user.IsWindowVisible(hwnd):
            children=[]
            def child(ch,lp):
                children.append({'handle':int(ch),'text':read(ch),'control_id':user.GetDlgCtrlID(ch)});return True
            cb=callback_type(child);user.EnumChildWindows(hwnd,cb,0)
            result.append({'handle':int(hwnd),'title':read(hwnd),'children':children})
        return True
    cb=callback_type(inspect);user.EnumWindows(cb,0)
    return result


def write_json(path,data):
    temp=path.with_suffix('.tmp');temp.parent.mkdir(parents=True,exist_ok=True)
    temp.write_text(json.dumps(data,indent=2),encoding='utf-8')
    for attempt in range(50):
        try:os.replace(temp,path);return
        except PermissionError:
            if attempt==49:raise
            time.sleep(.1)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exe',type=Path,required=True);parser.add_argument('--cwd',type=Path,required=True)
    parser.add_argument('--timeout',type=float,required=True);parser.add_argument('--ions',type=int,required=True)
    parser.add_argument('--log',type=Path,required=True)
    args=parser.parse_args()
    if os.name!='nt':raise RuntimeError('This supervisor runs under native Windows Python')
    start=time.monotonic();process=subprocess.Popen([str(args.exe)],cwd=args.cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    state={'pid':process.pid,'executable':str(args.exe),'cwd':str(args.cwd),'requested_ions':args.ions,'completed_dialog_ions':None,'status':'running','dialogs':[]}
    try:
        while process.poll() is None:
            dialogs=process_windows(process.pid)
            for dialog in dialogs:
                text=' '.join(child['text'] for child in dialog['children'])
                if dialog['title']=='End of TRIM.DAT calculation':
                    match=re.search(r'Calculation Complete\s*\((\d+) Ions?\)',text)
                    if not match or int(match.group(1))!=args.ions:
                        raise RuntimeError('TRIM completion count does not match requested histories: '+text)
                    state['completed_dialog_ions']=int(match.group(1))
                    # This is the completion acknowledgement, never an error or permission dialog.
                    buttons=[child for child in dialog['children'] if child['text'].strip().replace('&','')=='OK']
                    if len(buttons)!=1:raise RuntimeError('Ambiguous completion acknowledgement button')
                    ctypes.windll.user32.PostMessageW(wt.HWND(dialog['handle']),0x0111,buttons[0]['control_id'],wt.HWND(buttons[0]['handle']))
                    state['dialogs'].append({'title':dialog['title'],'text':text,'action':'acknowledged verified completion','control_id':buttons[0]['control_id']})
                else:
                    raise RuntimeError('Unexpected native TRIM dialog: '+dialog['title']+' '+text)
            state['elapsed_s']=time.monotonic()-start
            write_json(args.log,state)
            if time.monotonic()-start>args.timeout:raise TimeoutError('Native TRIM runtime exceeded configured timeout')
            time.sleep(.25)
        stdout,stderr=process.communicate(timeout=10)
        if process.returncode:raise RuntimeError(f'Native TRIM exit status {process.returncode}: '+stderr.decode('latin-1',errors='replace'))
        state.update(status='complete',exit_code=process.returncode,elapsed_s=time.monotonic()-start)
        write_json(args.log,state)
        return 0
    except BaseException as error:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.communicate()
        state.update(status='failed',elapsed_s=time.monotonic()-start,error=str(error))
        write_json(args.log,state)
        raise

if __name__=='__main__':raise SystemExit(main())
