"""requirements.txt が満たされているか確認する(満たしていれば終了コード 0、足りなければ 1)。
環境マーカー(sys_platform など)を評価する。--build を付けると PyInstaller の導入も確認する。"""
import sys
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement

root = Path(__file__).resolve().parent.parent
for line in (root / "requirements.txt").read_text().splitlines():
    line = line.split("#")[0].strip()
    if not line:
        continue
    r = Requirement(line)
    if r.marker is not None and not r.marker.evaluate():
        continue
    try:
        v = metadata.version(r.name)
    except metadata.PackageNotFoundError:
        sys.exit(1)
    if r.specifier and not r.specifier.contains(v, prereleases=True):
        sys.exit(1)
if "--build" in sys.argv:
    try:
        metadata.version("pyinstaller")
    except metadata.PackageNotFoundError:
        sys.exit(1)
sys.exit(0)
