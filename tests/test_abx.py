"""ABX 聴き比べ: 集計・p値・X の隠蔽・プロファイル切り替え要求。"""
import os, sys, random
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PyQt6.QtWidgets import QApplication
app = QApplication([])
from miyamizu.gui.main_window import ABXDialog, abx_pvalue

fails = 0
def check(name, ok, extra=""):
    global fails
    print(("PASS " if ok else "FAIL ") + name, extra); fails += (not ok)

check("p値: 10/10 = 1/1024", abs(abx_pvalue(10, 10) - 1 / 1024) < 1e-12)
check("p値: 5/10 は約62%", abs(abx_pvalue(5, 10) - 0.623046875) < 1e-9)
check("p値: 試行0回は1", abx_pvalue(0, 0) == 1.0)
d = ABXDialog(["共有", "排他"], rng=random.Random(1))
got = []
d.switch_requested.connect(got.append)
d._listen("A"); d._listen("B")
check("A/B を聴くとそれぞれのプロファイルへ切り替え要求", got == ["共有", "排他"], str(got))
x = d.x_is
d._listen("X")
check("X は A か B のどちらかのプロファイル", got[-1] == ("共有" if x == "A" else "排他"))
ok_before = d.correct
d._answer(x)
check("正解の集計", d.trials == 1 and d.correct == 1)
d._answer("A" if d.x_is == "B" else "B")
check("不正解の集計", d.trials == 2 and d.correct == 1 and "1 / 2" in d.lbl.text())
xs = set()
for _ in range(40):
    d._answer("A"); xs.add(d.x_is)
check("X は毎回ランダム(A/B 両方が出る)", xs == {"A", "B"})
d.reset()
check("リセット", d.trials == 0 and d.correct == 0)
print("ALL PASS" if not fails else f"{fails} FAIL")
sys.exit(1 if fails else 0)
