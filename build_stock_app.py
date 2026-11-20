"""Build the portable Windows app with the current public data snapshot."""
from pathlib import Path
import subprocess
import sys
import shutil
import importlib.util

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    seed = ROOT / "outputs" / "stock-report-latest.json"
    if not seed.exists():
        seed = ROOT / "reports" / "latest-data.json"
    if not seed.exists():
        raise SystemExit("请先生成一份行情快照。")
    resources = ROOT / "work" / "app-resources"
    shutil.copytree(ROOT / "app_assets", resources, dirs_exist_ok=True)
    shutil.copyfile(seed, resources / "seed.json")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--windowed", "--noupx",
               "--name", "美股评分助手", "--distpath", str(ROOT / "outputs" / "v1.1.0"),
               "--workpath", str(ROOT / "work" / "app-build"), "--specpath", str(ROOT / "work"),
               "--add-data", str(resources) + ":app_assets",
               "--collect-data", "tzdata", "--collect-data", "certifi", "--collect-all", "curl_cffi", "--collect-all", "py_mini_racer",
               "--add-binary", str(Path(importlib.util.find_spec("py_mini_racer").origin).parent / "mini_racer.dll") + ":.",
               "--copy-metadata", "yfinance", "--exclude-module", "openai",
               str(ROOT / "stock_app.py")]
    icon = ROOT / "app_assets" / "icon.ico"
    if icon.exists():
        command[3:3] = ["--icon", str(icon)]
    subprocess.run(command, cwd=ROOT, check=True)

