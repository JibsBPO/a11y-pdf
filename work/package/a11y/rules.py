"""Rule catalogue: PAC / Matterhorn Protocol / WCAG 2.x mapped to detection + auto-fix.
auto: 'full' = fixed deterministically, 'assist' = fixed with heuristics/AI, human should review, 'manual' = detect only."""
def R(mh, wcag, sev, title, how, auto): return dict(mh=mh, wcag=wcag, sev=sev, title=title, how=how, auto=auto)
RULES = {
 "NO_TAGS":        R("01-004","1.3.1","critical","Document has no structure tree (untagged PDF)","Auto-tag: wrap every BT/ET text run, image and path in marked content, build StructTreeRoot/Document/ParentTree, headings from font-size tiers.","assist"),
 "MARKED":         R("06-001","1.3.1","critical","MarkInfo /Marked is not true","Set Root.MarkInfo.Marked = true.","full"),
 "CONTENT_UNTAGGED":R("01-005","1.3.1","critical","Content is neither tagged nor marked as Artifact (PAC 'Content' failures)","Wrap text in /P|/H# MCIDs, images in /Figure, paths/backgrounds in /Artifact; Form XObjects that hold text or images are flattened into the page first so their content is tagged, not hidden.","full"),
 "SCANNED":        R("01-005","1.1.1","critical","Page is a scanned image with no real text","Tesseract OCR -> invisible embedded-font text layer, scan image becomes Artifact, OCR lines become tagged P/H.","assist"),
 "LANG":           R("11-006","3.1.1","major","Document natural language (Lang) missing","Detect language from text (stop-word scoring) or use chosen default; set Root.Lang.","full"),
 "TITLE":          R("07-001","2.4.2","major","No document title (XMP dc:title / Info Title)","Use first H1 / largest text on page 1 / filename; write Info.Title and XMP dc:title.","full"),
 "DISPLAY_TITLE":  R("07-002","2.4.2","major","ViewerPreferences/DisplayDocTitle is not true","Set DisplayDocTitle = true.","full"),
 "XMP_UA":         R("05-003","4.1.2","major","XMP metadata missing pdfuaid:part = 1","Write XMP packet with pdfuaid:part=1, dc:title, dc:language.","full"),
 "TABS":           R("28-004","2.4.3","major","Page /Tabs is not /S (tab order follows structure)","Set Tabs=/S on every page.","full"),
 "ROLEMAP":        R("02-001","4.1.2","major","Non-standard structure type not mapped to a standard type (PAC 'Role mapping')","Resolve custom roles by name (Heading1->H1, Paragraph->P ...) and write RoleMap; break circular maps.","assist"),
 "HEADING_FIRST":  R("14-002","1.3.1","major","First heading is not H1","Promote heading chain so first heading is H1.","full"),
 "HEADING_SKIP":   R("14-003","1.3.1","major","Heading levels skipped (e.g. H1 -> H3)","Clamp each heading to previous level + 1; plain /H becomes a numbered level.","full"),
 "FIG_ALT":        R("13-004","1.1.1","critical","Figure has no Alt / ActualText (PAC 'Alternative Descriptions')","AI vision (ANTHROPIC_API_KEY) -> OCR text of the figure -> placeholder flagged for review. Tiny/decorative figures become Artifacts.","assist"),
 "FIG_ALT_BAD":    R("13-004","1.1.1","major","Figure alt text is non-descriptive (file name, 'image', single glyph)","Regenerate alt text as above.","assist"),
 "TH_SCOPE":       R("15-003","1.3.1","major","TH cell has no Scope attribute","Add Table attribute Scope=Column (first row) / Row (first column).","full"),
 "TABLE_NOTH":     R("15-001","1.3.1","major","Table has no header cells","Promote first-row TD cells to TH + Scope=Column (flagged for review).","assist"),
 "LINK_ALT":       R("28-011","2.4.4","major","Link annotation has no /Contents description","Use URI / text under link rectangle as Contents.","full"),
 "ANNOT_UNTAGGED": R("28-002","1.3.1","major","Annotation not enclosed in a structure element (OBJR)","Create Link elem + OBJR, set /StructParent, rebuild ParentTree.","full"),
 "FORM_TU":        R("28-005","3.3.2","major","Form field has no /TU tooltip","Use field name / nearby text as TU.","assist"),
 "FONT_EMBED":     R("31-001","1.3.1","major","Font is not embedded","Simple fonts: substitute a metric-compatible font (Liberation, or Arial/Times/Courier on Windows), subset and embed it; an unknown family follows the font's flags, else the family used most in the document. Symbol/ZapfDingbats/ToUnicode-mapped symbolic fonts and Identity-H CID fonts with a ToUnicode map: rebuilt from the character table with a substitute that has those characters. Fonts whose codes carry no character information: re-export from the source application.","assist"),
 "BOOKMARKS":      R("—","2.4.5","minor","Long document (>=20 pages) has no bookmarks","Build outline from heading hierarchy.","full"),
 "PARENTTREE":     R("09-004","1.3.1","major","ParentTree/StructParents inconsistent","Rebuild ParentTree from the final structure tree.","full"),
 "CONTRAST":       R("—","1.4.3","minor","Possible low text contrast (needs source change)","Report only.","manual"),
}
