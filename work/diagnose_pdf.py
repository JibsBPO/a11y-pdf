import engine, validate, json
p='work/source-user.pdf'; o='work/repaired-user.pdf'
b=validate.run(p, external=False); a=validate.run(o, original=p, external=False)
print('BEFORE',json.dumps([(c['id'],c['status'],len(c['findings'])) for c in b['checks'] if c['status']!='passed']))
print('AFTER',json.dumps([(c['id'],c['status'],len(c['findings'])) for c in a['checks'] if c['status']!='passed']))
r=engine.fix(p,'work/repaired2.pdf',do_ocr=False)
print('ACTIONS',json.dumps(r['actions'],ensure_ascii=False))
