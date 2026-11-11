import pikepdf
p=pikepdf.open('work/source-user.pdf'); pg=p.pages[0]; ins=list(pikepdf.parse_content_stream(pg)); stack=[]
for i,c in enumerate(ins):
 if isinstance(c,pikepdf.ContentStreamInlineImage): continue
 op=str(c.operator); a=c.operands
 if op in ('BDC','BMC'):
  prop=a[1] if op=='BDC' and len(a)>1 else None; mid=prop.get('/MCID') if isinstance(prop,pikepdf.Dictionary) else None; stack.append((str(a[0]),int(mid) if mid is not None else None))
  print('open',i,stack[-1])
 elif op=='EMC':
  if i<130: print('close',i,stack[-1] if stack else None)
  if stack: stack.pop()
 elif op=='BT': print('BT',i,'stack',stack)
