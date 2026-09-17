# Seed 验证报告（37 条候选）

## 结论分布
- **强支持（窗口印证）**：2 条
- **强支持**：1 条
- **支持**：1 条
- **支持（众数非唯一）**：3 条
- **⚠️ 反例（众数≠target）**：18 条
- **无样本**：12 条

## ⚠️ 反例：入库众数 ≠ seed target（需要人工裁定或 seed 有误）（18）

- `[CMA+EMC+PIL交流群] 巴拿马科隆 → Cristóbal` (pod, true_alias) 本群样本 **24** 窗口印证 0/24=0%，Top3: `Colón×2; Caucedo×2; Rio Haina×2` 跨群样本 4
- `[CMA+EMC+PIL交流群] 多米尼加 CAUCEDO → Caucedo` (pod, true_alias) 本群样本 **24** 窗口印证 24/24=100%，Top3: `Colón×2; Caucedo×2; Rio Haina×2` 跨群样本 4
- `[土耳其价格更新] ALIAGA → Aliağa` (pod, true_alias) 本群样本 **139** 窗口印证 139/139=100%，Top3: `Mersin×18; Aliağa×18; Ambarli×16` 跨群样本 0
- `[宁波舱位滚动更新群] 南美西墨西哥 → WCSA` (pod, true_alias) 本群样本 **15** 窗口印证 0/15=0%，Top3: `(未注明，南美西墨西哥线)×3; Buenaventura×2; (未注明，WSA2)×2` 跨群样本 32
- `[宁波舱位滚动更新群] 南美西墨西哥 → MEX` (pod, true_alias) 本群样本 **15** 窗口印证 15/15=100%，Top3: `(未注明，南美西墨西哥线)×3; Buenaventura×2; (未注明，WSA2)×2` 跨群样本 32
- `[宁波舱位滚动更新群] 加勒比 → CAR` (pod, true_alias) 本群样本 **23** 窗口印证 23/23=100%，Top3: `Manzanillo×3; Buenaventura×3; LAZARO×2` 跨群样本 512
- `[宁波舱位滚动更新群] 南美东 → ECSA` (pod, true_alias) 本群样本 **39** 窗口印证 4/39=10%，Top3: `Santos×6; Rio de Janeiro×5; Navegantes×5` 跨群样本 209
- `[宁波舱位滚动更新群] 墨西哥 → MEX` (pod, true_alias) 本群样本 **15** 窗口印证 15/15=100%，Top3: `(未注明，南美西墨西哥线)×3; Buenaventura×2; (未注明，WSA2)×2` 跨群样本 169
- `[欧洲价格更新] 全国口岸 → 全国基本港` (pol, true_alias) 本群样本 **18** 窗口印证 0/18=0%，Top3: `Yantian×4; Shanghai×4; Ningbo×4` 跨群样本 0
- `[欧洲价格更新] Antwerp → Antwerpen` (pod, true_alias) 本群样本 **27** 窗口印证 27/27=100%，Top3: `Felixstowe×7; Antwerpen×6; Hamburg×4` 跨群样本 0
- `[美加线销售天团] PEL → Philadelphia` (pod, true_alias) 本群样本 **12** 窗口印证 12/12=100%，Top3: `Norfolk×2; Philadelphia×2; Baltimore×2` 跨群样本 0
- `[美加线销售天团] BAL → Baltimore` (pod, true_alias) 本群样本 **12** 窗口印证 12/12=100%，Top3: `Norfolk×2; Philadelphia×2; Baltimore×2` 跨群样本 0
- `[美加线销售天团] HOU → Houston` (pod, true_alias) 本群样本 **29** 窗口印证 0/29=0%，Top3: `NYC×4; SAV×3; HOU×3` 跨群样本 0
- `[美加线销售天团] MIA → Miami` (pod, true_alias) 本群样本 **29** 窗口印证 0/29=0%，Top3: `NYC×4; SAV×3; HOU×3` 跨群样本 0
- `[美加线销售天团] TPA → Tampa` (pod, true_alias) 本群样本 **29** 窗口印证 0/29=0%，Top3: `NYC×4; SAV×3; HOU×3` 跨群样本 0
- `[宁波舱位滚动更新群] 空班 → 空班` (space_type, enum_norm) 本群样本 **362** 窗口印证 362/362=100%，Top3: `跳港×256; 空班×84; 加班船×17` 跨群样本 0
- `[宁波舱位滚动更新群] 首航 → 首航` (space_type, enum_norm) 本群样本 **5** 窗口印证 5/5=100%，Top3: `现舱×5` 跨群样本 0
- `[美加线销售天团] 40H → 40HQ` (ctype, enum_norm) 本群样本 **16** 窗口印证 0/16=0%，Top3: `40HQ/HC×14; 20GP×2` 跨群样本 0

## 跨群歧义清单（3 条）
- `[宁波舱位滚动更新群] TJ → 天津` (pol) 跨群歧义：其他群 26 样本众数=NB（26/26）
- `[宁波舱位滚动更新群] XM → 厦门` (pol) 跨群歧义：其他群 26 样本众数=NB（26/26）
- `[COSCO+HMM+MSK+OOCL交流群] omit → 跳港` (space_type) 跨群歧义：其他群 3 样本众数=现舱（3/3）

## 强支持（窗口印证）：>=5 样本 + 众数>=80% + 窗口内 target 变体覆盖 >=50%（2）

- `[欧洲价格更新] POD欧基 → 欧基港` (route, true_alias) 本群样本 **13** 窗口印证 13/13=100%，Top3: `欧基港×13` 跨群样本 0
- `[欧洲价格更新] 欧基 → 欧基港` (route, true_alias) 本群样本 **13** 窗口印证 13/13=100%，Top3: `欧基港×13` 跨群样本 0

## 强支持：>=5 样本 + 众数>=80%（无窗口印证）（1）

- `[欧洲价格更新] 全口岸 → 全国基本港` (pol, true_alias) 本群样本 **13** 窗口印证 0/13=0%，Top3: `全国基本港×12; Ningbo×1` 跨群样本 58

## 支持：>=1 样本 + 众数=target（1）

- `[土耳其价格更新] 伊斯坦布尔 → Ambarli` (pod, true_alias) 本群样本 **2** 窗口印证 2/2=100%，Top3: `Ambarli×2` 跨群样本 0

## 支持（众数非唯一）（3）

- `[土耳其价格更新] ISTANBUL → Ambarli` (pod, true_alias) 本群样本 **197** 窗口印证 197/197=100%，Top3: `Ambarli×41; Mersin×34; Aliağa×16` 跨群样本 0
- `[美加线销售天团] NYC → New York` (pod, true_alias) 本群样本 **29** 窗口印证 29/29=100%，Top3: `NYC×4; SAV×3; HOU×3` 跨群样本 0
- `[美加线销售天团] ORF → Norfolk` (pod, true_alias) 本群样本 **12** 窗口印证 12/12=100%，Top3: `Norfolk×2; Philadelphia×2; Baltimore×2` 跨群样本 0

## 无样本：本群消息里从没写过该 alias（12）

- `[大连-拉美+美加] DL → Dalian` (pol, true_alias) 本群样本 **0**，Top3: `—` 跨群样本 242
- `[宁波舱位滚动更新群] NB → 宁波` (pol, true_alias) 本群样本 **0**，Top3: `—` 跨群样本 50
- `[宁波舱位滚动更新群] SH → 上海` (pol, true_alias) 本群样本 **0**，Top3: `—` 跨群样本 62
- `[宁波舱位滚动更新群] TJ → 天津` (pol, true_alias) 本群样本 **0**，Top3: `—` 跨群样本 26 | 跨群歧义：其他群 26 样本众数=NB（26/26）
- `[宁波舱位滚动更新群] DL → 大连` (pol, true_alias) 本群样本 **0**，Top3: `—` 跨群样本 242
- `[宁波舱位滚动更新群] XM → 厦门` (pol, true_alias) 本群样本 **0**，Top3: `—` 跨群样本 26 | 跨群歧义：其他群 26 样本众数=NB（26/26）
- `[COSCO+HMM+MSK+OOCL交流群] BLANK SAILING → 空班` (space_type, enum_norm) 本群样本 **0**，Top3: `—` 跨群样本 0
- `[COSCO+HMM+MSK+OOCL交流群] 停航 → 空班` (space_type, enum_norm) 本群样本 **0**，Top3: `—` 跨群样本 0
- `[COSCO+HMM+MSK+OOCL交流群] 跳挂 → 跳港` (space_type, enum_norm) 本群样本 **0**，Top3: `—` 跨群样本 0
- `[COSCO+HMM+MSK+OOCL交流群] omit → 跳港` (space_type, enum_norm) 本群样本 **0**，Top3: `—` 跨群样本 3 | 跨群歧义：其他群 3 样本众数=现舱（3/3）
- `[土耳其价格更新] 现仓 → 现舱` (space_type, enum_norm) 本群样本 **0**，Top3: `—` 跨群样本 1
- `[宁波舱位滚动更新群] 跳港 → 跳港` (space_type, enum_norm) 本群样本 **0**，Top3: `—` 跨群样本 0

---
生成：verify_seed_v1.py（窗口=40 字符；变体源=主数据 name + name_cn + aliases + port_groups.label + 大小写；反向索引 fold→name）