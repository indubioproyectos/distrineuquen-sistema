import json,sys,re,html
raw=json.load(open(sys.argv[1])); j=None
for x in raw:
    try: j=json.loads(x['text']); break
    except: pass
xml=j['data']['xml']
def txt(s): return html.unescape(re.sub(r'<[^>]+>','',s)).strip()
tok=re.finditer(r"<paragraph ([^>]*)>(.*?)</paragraph>|<blockquote [^>]*>(.*?)</blockquote>|<listItem ([^>]*)>(.*?)</listItem>", xml, re.S)
fichas=[]; cur=None
for m in tok:
    if m.group(1) is not None:
        a,b=m.group(1),m.group(2)
        if "heading='3'" in a: cur={'titulo':txt(b),'parrafos':[],'mensaje':[],'tareas':[]}; fichas.append(cur); continue
        if "heading='2'" in a: cur=None; continue
        if cur is not None: cur['parrafos'].append(txt(b))
    elif m.group(3) is not None and cur is not None:
        cur['mensaje'].append([txt(p) for p in re.findall(r"<paragraph[^>]*>(.*?)</paragraph>", m.group(3), re.S)])
    elif m.group(4) is not None and cur is not None:
        cur['tareas'].append(("[x] " if "checked='true'" in m.group(4) else "[ ] ")+txt(m.group(5)))
json.dump(fichas, open(sys.argv[2],'w'), ensure_ascii=False, indent=1)
print(len(fichas), j['data'].get('complete'))
