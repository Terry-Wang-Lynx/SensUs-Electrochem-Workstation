#!/usr/bin/env python3
"""Guard the GUI script against references to missing DOM elements."""

from __future__ import annotations

import re
from pathlib import Path


GUI_DIR = Path(__file__).parents[1] / "pa_host" / "gui"


def test_every_app_dollar_id_exists_in_index_html() -> None:
    script = (GUI_DIR / "app.js").read_text(encoding="utf-8")
    document = (GUI_DIR / "index.html").read_text(encoding="utf-8")
    referenced_ids = set(re.findall(r"\$\('([^']+)'\)", script))
    document_ids = set(re.findall(r'\bid="([^"]+)"', document))

    missing = sorted(referenced_ids - document_ids)
    assert not missing, f"app.js references missing index.html ids: {missing}"
