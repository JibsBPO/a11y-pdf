import pikepdf,validate,forms,engine
p=pikepdf.open('work/source-user.pdf'); page=p.pages[0]; res=forms.inherited(page.obj,'/Resources') or {}
print('VALIDATOR',validate.scan_content(page))
print('PROPS',res.get('/Properties'))
for c in pikepdf.parse_content_stream(page):
 if not isinstance(c,pikepdf.ContentStreamInlineImage) and str(c.operator) in ('BDC','BMC'):
  a=c.operands; prop=a[1] if len(a)>1 else None
  print('BDC',str(a[0]) if a else '',repr(prop),type(prop).__name__,'resolve', (res.get('/Properties') or {}).get(str(prop)) if isinstance(prop,pikepdf.Name) else None)
