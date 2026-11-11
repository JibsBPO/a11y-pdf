import engine, validate, json
p='work/source-user.pdf'; out='work/repaired-user.pdf'
b=validate.run(p, external=False)
print('BEFORE', json.dumps({c['id']:{'status':c['status'],'findings':[f['msg'] for f in c['findings'][:3]]} for c in b['checks'] if c['status'] in ('failed','error')}))
try:
    x=engine.fix(p,out,do_ocr=False)
    print('FIX', x['title'],len(x['actions']))
    a=validate.run(out,original=p,external=False)
    print('AFTER', json.dumps({c['id']:{'status':c['status'],'findings':[f['msg'] for f in c['findings'][:3]]} for c in a['checks'] if c['status'] in ('failed','error')}))
except Exception as e:
    print(type(e).__name__+': '+str(e))
    raise
