from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import pymupdf
c=canvas.Canvas("t_untagged.pdf",pagesize=letter)
c.setFont("Helvetica-Bold",24); c.drawString(72,720,"Quarterly Report")
c.setFont("Helvetica",12); c.drawString(72,690,"This is the first paragraph of body text for the report.")
c.drawString(72,675,"It continues on a second line with more words in it today.")
c.setFont("Helvetica-Bold",16); c.drawString(72,630,"Results")
c.setFont("Helvetica",12); c.drawString(72,610,"The results were good and the team is happy with them.")
c.rect(72,400,200,100,fill=1); c.linkURL("https://example.com",(72,560,200,575),relative=0); c.drawString(72,563,"Visit example")
c.showPage(); c.save()
d=pymupdf.open("t_untagged.pdf"); pix=d[0].get_pixmap(dpi=150); pix.save("scan.png")
s=pymupdf.open(); p=s.new_page(width=612,height=792); p.insert_image(p.rect,filename="scan.png"); s.save("t_scan.pdf")
