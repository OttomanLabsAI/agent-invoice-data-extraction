"""Rebuild public/downloads/invoice-agent-updater.zip from the files in updater/ (dev only).

Fixed timestamps keep the zip byte-for-byte stable when nothing changed, so git only sees real changes.
"""

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["update.bat", "update.command", "updater.py", "updater.html", "README.txt"]
OUT = ROOT / "public" / "downloads" / "invoice-agent-updater.zip"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            info = zipfile.ZipInfo(f"Invoice agent/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(".command") else 0o644) << 16
            archive.writestr(info, (ROOT / "updater" / name).read_bytes())
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
