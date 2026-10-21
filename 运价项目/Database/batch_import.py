# -*- coding: utf-8 -*-
"""xlsx / csv → 批量导入用 CSV（固定 18 列 + 表头），给网页「批量导入」粘贴或上传用。

    python batch_import.py --file 周表.xlsx                # 打印到屏幕
    python batch_import.py --file 周表.xlsx --out 待导入.csv
    python batch_import.py --file 周表.xlsx --post         # 顺手把结果贴到剪贴板

表头能认出 3 列以上就按列名对位（Excel 里换了顺序也不怕）；认不出就按固定列序原样搬。
业务校验和归一都在页面/服务端做，这个脚本只搬格子，不改值。
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
COLS = ['船司', '起运港', '目的港', '20GP', '40HQ/HC', '40NOR', '45HQ/HC', '有效期', 'Freetime',
        '备注', '航线', '船期ETD', 'T/T航程', '亏舱费', '来源群', '发送人',
        '消息原文', '序号', '更新目标']
# Excel 里常见的别名表头
ALIAS = {
    '承运人': '船司', '船公司': '船司', 'carrier': '船司',
    '起运港/POL': '起运港', 'POL': '起运港', '起运': '起运港',
    '目的港/POD': '目的港', 'POD': '目的港', '目的': '目的港',
    '40HQ': '40HQ/HC', '40HC': '40HQ/HC', '40GP': '40HQ/HC', '40NOR/HC': '40NOR',
    '45HQ': '45HQ/HC', '45HC': '45HQ/HC',
    '免箱期': 'Freetime', 'F/T': 'Freetime', 'Freetime(天)': 'Freetime', 'Free Time': 'Freetime',
    '航程': 'T/T航程', 'T/T': 'T/T航程', 'Transit': 'T/T航程',
    '有效期至': '有效期', '有效期(起-止)': '有效期',
    'ETD': '船期ETD', '船期': '船期ETD', '备注1': '备注',
}


def read_rows(path: Path):
    if path.suffix.lower() in ('.xlsx', '.xlsm'):
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)   # data_only：要单元格真实值，不要公式
        ws = wb[wb.sheetnames[0]]
        try:
            ws.reset_dimensions()      # 有些导出的 xlsx 维度元数据是坏的，openpyxl 会读不全
        except Exception:  # noqa: BLE001 - 新版 openpyxl 已不需要，失败就算了
            pass
        rows = [[('' if c is None else str(c).strip()) for c in r] for r in ws.iter_rows(values_only=True)]
        wb.close()
        return [r for r in rows if any(r)]
    with path.open(encoding='utf-8-sig', newline='') as f:
        d = '\t' if sum(l.count('\t') for l in f) > 3 else ','
        f.seek(0)
        return [[c.strip() for c in row] for row in csv.reader(f, delimiter=d) if any(c.strip() for c in row)]


def cell_of(row, mapping, col):
    i = mapping.get(col, -1)
    return row[i] if 0 <= i < len(row) else ''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True, help='.xlsx 或 .csv/.txt')
    ap.add_argument('--out', help='输出 CSV 路径，不给就打印')
    ap.add_argument('--post', action='store_true', help='结果同时放剪贴板')
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        raise SystemExit('找不到文件：%s' % path)
    rows = read_rows(path)
    if not rows:
        raise SystemExit('表里是空的')

    head = [h for h in rows[0]]
    named = {ALIAS.get(h, h): j for j, h in enumerate(head)}
    hit = {c: named[c] for c in COLS if c in named}
    if len(hit) >= 3:
        mapping, body = hit, rows[1:]
        print('按表头列名对位，认出 %d 列：%s' % (len(hit), '、'.join(sorted(hit, key=COLS.index))))
    else:
        mapping = {c: i for i, c in enumerate(COLS)}
        body = rows if not any(h in COLS for h in head) else rows[1:]
        print('表头认不出 3 列，按固定列序原样搬（列序：%s）' % '、'.join(COLS))

    out = io.StringIO()
    w = csv.writer(out, lineterminator='\n')
    w.writerow(COLS)
    kept = 0
    for r in body:
        vals = [cell_of(r, mapping, c) for c in COLS]
        if not any(vals):
            continue
        w.writerow(vals)
        kept += 1
    text = '﻿' + out.getvalue()
    print('写出 %d 数据行' % kept)

    if args.out:
        Path(args.out).write_text(text, encoding='utf-8')
        print('已存：', args.out)
    else:
        sys.stdout.write(text)
    if args.post:
        subprocess_run_clip(text)
    return 0


def subprocess_run_clip(text: str) -> None:
    """放剪贴板（Windows）。失败不影响主流程，屏幕/文件都还在。"""
    import subprocess
    try:
        p = subprocess.Popen(['clip'], stdin=subprocess.PIPE)
        p.communicate(text.lstrip('﻿').encode('utf-16-le', 'ignore'))
        print('已放剪贴板（PowerShell clip 走 UTF-16，若粘贴乱码请用 --out 存文件）')
    except Exception as e:  # noqa: BLE001 - 剪贴板只是便利贴，不致命
        print('剪贴板没放成：', e)


if __name__ == '__main__':
    sys.exit(main())
