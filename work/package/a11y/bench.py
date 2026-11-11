"""Performance test: python bench.py [pages] [links_per_page] [images_per_page]
Generates a synthetic untagged PDF (headings, paragraphs, images, links) and times every stage."""
import sys, os, time, tempfile, resource, io
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from PIL import Image
import engine, validate

def make(path, pages, links, imgs):
    pdfmetrics.registerFont(TTFont("DV", engine.find_font() or "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
    im = Image.effect_noise((160, 90), 60).convert("RGB"); buf = io.BytesIO(); im.save(buf, "PNG"); png = path + ".png"; open(png, "wb").write(buf.getvalue())
    c = canvas.Canvas(path, pagesize=letter)
    for p in range(pages):
        c.setFont("DV", 20); c.drawString(50, 750, f"Chapter {p // 10 + 1}" if p % 10 == 0 else f"Section {p}")
        c.setFont("DV", 10)
        for l in range(45): c.drawString(50, 720 - l * 9.5, f"Page {p} line {l}: the quick brown fox jumps over the lazy dog and keeps running.")
        for k in range(imgs): c.drawImage(png, 400 + (k % 3) * 55, 100 + (k // 3) * 40, 50, 30)
        for k in range(links): c.linkURL(f"https://example.com/{p}/{k}", (50, 700 - k * 9.5 % 600, 250, 710 - k * 9.5 % 600), relative=0)
        c.showPage()
    c.save()

def rss(): return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

if __name__ == "__main__":
    pages, links, imgs = (int(x) for x in (sys.argv[1:] + ["50", "5", "2"])[:3])
    d = tempfile.mkdtemp(); src, out = d + "/s.pdf", d + "/f.pdf"
    t = time.perf_counter(); make(src, pages, links, imgs); print(f"generated {pages} pages, {links} links/page, {imgs} images/page, {os.path.getsize(src)/1e6:.1f} MB  ({time.perf_counter()-t:.1f}s)")
    for name, fn in (("analyze (built-in)", lambda: engine.analyze(src)), ("fix", lambda: engine.fix(src, out)), ("validate original", lambda: validate.run(src, external=False)),
                     ("validate repaired", lambda: validate.run(out, original=src, external=False))):
        t = time.perf_counter(); r = fn(); print(f"{name:20} {time.perf_counter()-t:7.1f}s   peak RSS {rss():6.0f} MB")
    v = validate.run(out, original=src, external=False); print("verdict:", v["verdict"], v["totals"])
