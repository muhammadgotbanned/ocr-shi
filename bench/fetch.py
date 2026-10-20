"""Download the benchmark's pages and lines (listed in manifest.json) into bench/data/.

    python bench/fetch.py

The files come from the OpenITI OCR gold standard on GitHub, pinned to one commit, so
every run scores exactly the same images against exactly the same text.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"


def main() -> int:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    repo = manifest["source"].replace("https://github.com/", "")
    base = f"https://raw.githubusercontent.com/{repo}/{manifest['commit']}/"
    wanted = [l["image"] for l in manifest["lines"]] + [l["text"] for l in manifest["lines"]] + [p["image"] for p in manifest["pages"]]
    todo = [rel for rel in wanted if not (DATA / rel).exists()]

    def get(rel: str) -> str:
        dest = DATA / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(base + rel, timeout=60) as resp:
            tmp = dest.with_suffix(dest.suffix + ".part")
            tmp.write_bytes(resp.read())
            tmp.replace(dest)
        return rel

    failed = 0
    with ThreadPoolExecutor(8) as pool:
        for fut in [pool.submit(get, rel) for rel in todo]:
            try:
                fut.result()
            except Exception as exc:  # report and keep going
                failed += 1
                print(f"  ! {exc}", file=sys.stderr)
    print(f"{len(wanted) - failed}/{len(wanted)} files in {DATA}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
