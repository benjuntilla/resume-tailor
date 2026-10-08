"""Stand-in for ``rendercv render base.yaml``: writes a one-page PDF of the live lines.

A resume source containing ``BROKEN`` fails to build, like an invalid RenderCV file.
"""

import sys
from pathlib import Path

import pymupdf


def main() -> int:
    source = Path(sys.argv[-1]).read_text()
    if "BROKEN" in source:
        sys.stderr.write("rendercv: validation error\n")
        return 1
    live = [line.strip() for line in source.splitlines() if line.strip() and not line.strip().startswith("#")]
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(page.rect + (36, 36, -36, -36), "\n".join(live[:60]), fontsize=7)
    doc.save("Alex_Doe_resume.pdf")
    return 0


if __name__ == "__main__":
    sys.exit(main())
