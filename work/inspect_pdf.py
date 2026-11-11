import pikepdf, pymupdf, engine, validate, collections, json
for path in ('work/source-user.pdf','work/repaired-user.pdf'):
 print('\nFILE',path)
 d=pymupdf.open(path); p=pikepdf.open(path); page=p.pages[0]
 ins,units,m=engine.scan_page(page)
 print('page units',len(units),'untagged',[(u['kind'],u.get('mcid'),u['i0'],u['i1']) for u in units if not u['tagged']][:20])
 print('text len',len(d[0].get_text()),'images',len(d[0].get_images()),'text preview',repr(d[0].get_text()[:200]))
 forms=[]
 def rec(res,seen=set()):
  for n,x in (res.get('/XObject') or {}).items():
   if str(x.get('/Subtype'))=='/Form' and x.objgen not in seen:
    seen.add(x.objgen); forms.append((str(n),x)); rec(x.get('/Resources',res),seen)
 rec(page.Resources)
 print('forms',len(forms))
 for n,x in forms[:12]:
  try: ids=engine.stream_mcids(x,x.get('/Resources',page.Resources))
  except Exception as e: ids=[str(e)]
  print(' form',n,x.objgen,'ids',ids,'sp',x.get('/StructParents'))
 print('tree refs',[(k,[(p,m,len(o)) for (p,m),o in validate.walk_struct(p,{page.obj.objgen:0})[1].items()]) for k in []])
 p.close();d.close()
