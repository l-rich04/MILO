import pymupdf

doc = pymupdf.open("textbooks/electromagnetics.pdf")
print(f"This PDF has {doc.page_count} pages")

page = doc[46]
print(page.get_text())
