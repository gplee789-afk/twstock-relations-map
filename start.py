"""雙擊「開啟心智圖.bat」後進來的互動介面：輸入代號 → 跑 relations.py → 用瀏覽器打開產生的心智圖。

也可以帶參數，會原樣轉給 relations.py（例如 `開啟心智圖.bat 2330 --depth 2`）。
"""
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / "reports"

NOTICE = """\
公司互動心智圖（本機版）
・會連到 MoneyDJ、證交所、櫃買中心等網站抓資料；不是投資建議，資料可能不完整或過時。
・MoneyDJ 使用條款禁止未經同意以爬蟲下載資料，使用前請先看 README.md 的「免責聲明」，責任由使用者自負。
・產生的心智圖存在 reports 資料夾，請只供自己使用，勿轉載或公開分享。
"""


def run(args: list[str]) -> Path | None:
    started = time.time()
    subprocess.run([sys.executable, str(ROOT / "relations.py"), *args], cwd=ROOT)
    # relations.py 只把路徑印在輸出裡，直接找這次新寫出來的報表比解析輸出可靠
    new = [p for p in REPORT_DIR.glob("relations-*.html") if p.stat().st_mtime >= started - 1]
    return max(new, key=lambda p: p.stat().st_mtime) if new else None


def open_report(args: list[str]):
    if path := run(args):
        print(f"\n用瀏覽器打開：{path}")
        os.startfile(path)
    else:
        print("\n沒有產生心智圖，請看上面的錯誤訊息。")


def main():
    print(NOTICE)
    if len(sys.argv) > 1:
        open_report(sys.argv[1:])
        return
    while True:
        try:
            line = input("輸入代號或公司簡稱（多個用空白隔開，例如 2330 台泥；直接按 Enter 結束）：").strip()
        except EOFError:
            break
        if not line:
            break
        open_report(line.split())
        print()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 主控台預設 cp950，印中文會亂碼
    main()
