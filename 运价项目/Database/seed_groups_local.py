# -*- coding: utf-8 -*-
"""把港群定义直接写进本地独立库（局域网版自己的主数据，不读不写任何其它看板）。

    python seed_groups_local.py          写进 freight_manual.db
    python seed_groups_local.py --check  只看解析结果

成员名先按规范名精确匹配、再退别名索引；归一不上的列出来不猜。
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

import manualdb  # noqa: E402

# 群名, 中文叫法, 成员（用户口径：库里存群名，查询成员港时自动带出群价）
GROUPS = [
    ("WCSA", "南美西岸", "Buenaventura/Guayaquil/Posorja/Manta/Callao/Chancay/Paita/Pisco/"
                        "San Antonio/Valparaiso/Iquique/Arica/Antofagasta/Lirquen/Coronel/"
                        "Talcahuano/San Vicente/Mejillones"),
    ("ECSA", "南美东岸", "Santos/Rio de Janeiro/Paranagua/Itajai/Navegantes/Rio Grande/"
                        "Montevideo/Buenos Aires"),
    ("NEB", "北巴（巴西东北）", "Itaqui/Suape/Recife/Fortaleza/Pecém/Salvador/Belem/Manaus"),
    ("WCCA", "中美西岸", "Balboa/Puerto Quetzal/Acajutla/Corinto/Caldera"),
    ("CAR", "加勒比", "Colon/Cartagena/Barranquilla/Santa Marta/Kingston/Caucedo/"
                     "Manzanillo(PA)/Rio Haina"),
    ("MEX", "墨西哥", "Manzanillo(MX)/Lazaro Cardenas"),
]
# 口语写法 → 主数据里的规范名（Caldera/Cartagena/Manzanillo 都是跨国同名，必须指明是哪个）
FIX = {"Colon": "Cristobal", "Paranagua": "Paranaguá", "Lazaro Cardenas": "Lázaro Cárdenas",
       "Lirquen": "Lirquén", "Cartagena": "Cartagena(CO)", "Caldera": "Caldera(CR)"}


def resolve(m: manualdb.Master, raw: str) -> str:
    want = FIX.get(raw, raw)
    for cand in (want, raw):
        if cand in m.name_idx.values() or manualdb.fold(cand) in m.name_idx:
            return m.name_idx.get(manualdb.fold(cand), cand)
    got, ok = m.lookup_port(want)
    return got if ok else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只解析不写库")
    args = ap.parse_args()
    c = manualdb.connect()
    m = manualdb.Master(c)
    rows, miss = [], []
    for name, label, members in GROUPS:
        norm = []
        for raw in members.split("/"):
            got = resolve(m, raw.strip())
            (norm if got else miss).append(got or raw.strip())
        rows.append({"name": name, "label": label, "members": ",".join(norm),
                     "specials": "", "kind": "pod", "active": 1,
                     "note": "库里存群名；查询成员港时自动带出群价"})
        print("%-5s %-14s %2d 港  %s" % (name, label, len(norm), "、".join(norm)))
    if miss:
        print("\n主数据里找不到、没写进群成员（要么补港，要么改写法）：")
        for x in miss:
            print("   ", x)
    if args.check:
        return 1 if miss else 0
    n = manualdb.sync_rows(c, "port_groups", rows)
    manualdb.invalidate_master()
    print("\n已写入本地库 port_groups：%d 条 → %s" % (n, manualdb.DEFAULT_DB.name))
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
