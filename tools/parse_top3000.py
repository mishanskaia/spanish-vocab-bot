"""One-off: turn the top-3000 .docx (word / translation, grouped by topic) into static/top3000.json.

Kept in the repo so the list can be regenerated if the source document changes.
Usage: python tools/parse_top3000.py <path to .docx>
"""
import json
import re
import sys
import zipfile
from pathlib import Path

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "top_3000_spanish_words.docx")
OUT = Path(__file__).resolve().parent.parent / "static" / "top3000.json"


def cell_text(fragment: str) -> str:
    # `<w:t[^>]*>` would also match <w:tcPr>, <w:tbl> and friends — the space is what
    # separates a text run's own attributes from a different tag that starts with "t".
    return "".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", fragment, re.S)).strip()


def main():
    xml = zipfile.ZipFile(SRC).read("word/document.xml").decode("utf-8")
    body = re.search(r"<w:body>(.*)</w:body>", xml, re.S).group(1)
    blocks = re.findall(r"<w:tbl>.*?</w:tbl>|<w:p[ >].*?</w:p>", body, re.S)

    words, categories, category, seen = [], [], None, set()
    for block in blocks:
        if block.startswith("<w:tbl"):
            for row in re.findall(r"<w:tr[ >].*?</w:tr>", block, re.S):
                cells = [cell_text(c) for c in re.findall(r"<w:tc>.*?</w:tc>", row, re.S)]
                if len(cells) < 3 or not cells[0].isdigit():
                    continue  # header row
                es, ru = cells[1], cells[2]
                key = es.lower()
                if not es or not ru or key in seen:
                    continue
                seen.add(key)
                words.append({"n": int(cells[0]), "es": es, "ru": ru, "cat": category})
            continue
        # "1. Приветствия и базовые фразы" before each table; the same lines appear in the
        # table of contents with a count — "… (39)" — which is why the count is optional here.
        heading = re.match(r"^(\d{1,2})\.\s+(.+?)(?:\s+\((\d+)\))?$", cell_text(block))
        if heading:
            category = heading.group(2)
            if category not in categories:
                categories.append(category)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        json.dumps({"categories": categories, "words": words}, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"{len(words)} words, {len(categories)} categories → {OUT}")


if __name__ == "__main__":
    main()
