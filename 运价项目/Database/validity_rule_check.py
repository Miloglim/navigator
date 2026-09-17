# -*- coding: utf-8 -*-
"""有效期解析规则的表驱动验证。

这是 index.html 里 btValidity 的**同一套规则的 Python 誊写**，只用来跑用例，
不参与任何生产路径（本机没有 node，JS 没法直接执行，所以用这个方式验规则本身）。
改 JS 规则时记得同步改这里，否则用例通过不代表线上行为。
"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
NOW_YEAR = 2026          # 与服务端「今天按东八区」一致
FAILS = []


def pad(n):
    return "%02d" % n


def iso(y, m, d):
    return "%d-%s-%s" % (y, pad(m), pad(d))


def bt_validity(raw, etd_hint=""):
    s = (raw or "").strip()
    if not s:
        return ("", "", "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return (s, "", "只给了起始日，未填截止")
    pair = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*(?:→|~|-|/|到|至)\s*(\d{4}-\d{2}-\d{2})", s)
    if pair:
        return (pair.group(1), pair.group(2), "")
    nums = re.findall(r"\d+", re.sub(r"[年月日.]", " ", s))
    if not nums:
        return ("", "", "看不懂这个有效期写法")
    y_hint = int(etd_hint[:4]) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", etd_hint or "") else 0
    yr = y_hint or NOW_YEAR

    def fix(m, d):
        return 1 <= m <= 12 and 1 <= d <= 31

    if len(nums) == 2 and all(len(n) == 8 for n in nums):
        f = lambda n: iso(int(n[0:4]), int(n[4:6]), int(n[6:8]))
        g = lambda n: fix(int(n[4:6]), int(n[6:8]))
        return (f(nums[0]), f(nums[1]), "") if g(nums[0]) and g(nums[1]) \
            else ("", "", "看不懂这个有效期写法")
    if len(nums) == 2:
        m, d = int(nums[0]), int(nums[1])
        return (iso(yr, m, d), "", "只给了起始日，未填截止") if fix(m, d) \
            else ("", "", "看不懂这个有效期写法")
    if len(nums) == 3:
        m, d1, d2 = map(int, nums)
        return (iso(yr, m, d1), iso(yr, m, d2), "") if fix(m, d1) and fix(m, d2) and d2 >= d1 \
            else ("", "", "看不懂这个有效期写法")
    if len(nums) >= 4:
        t = list(map(int, nums))
        y1, y2, m1, d1, m2, d2 = yr, yr, t[0], t[1], t[2], t[3]
        if len(nums[0]) == 4:
            y1 = t[0]
            y2 = t[3] if len(nums) >= 5 else y1
            if len(nums) >= 5:
                m1, d1, m2, d2 = t[1], t[2], t[4], t[5]
            else:
                m1, d1, m2, d2 = t[1], t[2], t[1] + 0, t[3]   # 2026.9.10-2026.9.16 之外的畸形输入
        if not fix(m1, d1) or not fix(m2, d2):
            return ("", "", "看不懂这个有效期写法")
        if m2 < m1:
            y2 = y1 + 1
        a, b = iso(y1, m1, d1), iso(y2, m2, d2)
        return (a, b, "") if b >= a else ("", "", "有效期截止早于起始")
    return ("", "", "看不懂这个有效期写法")


CASES = [
    ("9.10-9.16", "", "2026-09-10", "2026-09-16", ""),
    ("9.10~9.16", "", "2026-09-10", "2026-09-16", ""),
    ("9/10-9/16", "", "2026-09-10", "2026-09-16", ""),
    ("9月10日-9月16日", "", "2026-09-10", "2026-09-16", ""),
    ("9.10 - 9.16", "", "2026-09-10", "2026-09-16", ""),
    ("9.10-16", "", "2026-09-10", "2026-09-16", ""),
    ("2026-09-10 → 2026-09-16", "", "2026-09-10", "2026-09-16", ""),
    ("2026-09-10/2026-09-16", "", "2026-09-10", "2026-09-16", ""),
    ("2026-09-10至2026-09-16", "", "2026-09-10", "2026-09-16", ""),
    ("20260910-20260916", "", "2026-09-10", "2026-09-16", ""),
    ("9.20", "", "2026-09-20", "", "只给了起始日，未填截止"),
    ("12.28-1.5", "", "2026-12-28", "2027-01-05", ""),          # 跨年自动加一年
    ("12.28-1.5", "2027-01-05", "2027-12-28", "2028-01-05", ""),  # 年份跟 ETD 推
    ("9.16-9.10", "", "", "", "有效期截止早于起始"),
    ("本周内有效", "", "", "", "看不懂这个有效期写法"),
    ("", "", "", "", ""),
]

for raw, etd, want_f, want_t, want_n in CASES:
    got = bt_validity(raw, etd)
    ok = got == (want_f, want_t, want_n)
    if not ok:
        FAILS.append(raw)
    print(("  OK   " if ok else "  FAIL ") + ("%-22s etd=%-11s → %s" % (raw or "(空)", etd or "-", got))
          + ("" if ok else "  期望 %s" % ((want_f, want_t, want_n),)))

print("\n结果:", "全部通过" if not FAILS else "失败 %d 项 -> %s" % (len(FAILS), FAILS))
sys.exit(1 if FAILS else 0)
