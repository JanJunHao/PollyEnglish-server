# 30+ 高质量 YouTube CC / 公有领域频道清单

**适用于**：构建合法、可商用的英语学习内容库

**重要前提**：

1. **公有领域（Public Domain）** 比 CC 更自由 —— 美国联邦政府制作的内容自动进入公有领域，无任何使用限制，连署名都不强制要求。
2. **CC BY** 允许商业使用 + 修改 + 重新分发，唯一要求是署名。
3. **CC BY-SA**（相同方式共享）允许商业使用，但你的衍生作品也必须以 CC BY-SA 发布。
4. **CC BY-NC**（非商业）**不能用于付费 App**。
5. **频道整体协议 ≠ 每个视频的协议**。再大牌的频道，也要逐个视频验证 `license` 字段。

**验证方法**：

```bash
yt-dlp --dump-json "https://youtube.com/watch?v=XXX" | jq '.license'
# 应返回: "Creative Commons Attribution license (reuse allowed)"
```

或在 YouTube 视频页 "Show more" 里查看是否标注 "Creative Commons Attribution license (reuse allowed)"。

---

## 🥇 第一梯队：公有领域（美国联邦政府）

> 这一类是**金矿** —— 完全免费、无任何使用限制、不强制署名（但建议署名以示尊重）。先把这几个频道吃透，足够撑起一个英语学习 App 的 80% 内容。

### 1. VOA Learning English

- **频道地址**: youtube.com/@VOALearningEnglish
- **内容**: 专为英语学习者制作的新闻、文化、科普
- **协议**: 美国联邦政府作品 → **公有领域**
- **价值**: 已按 Level 1/2/3 分级，语速精确控制（90~150 WPM），字幕规整
- **推荐栏目**: News Words、Let's Learn English、America's National Parks

### 2. VOA News（主频道）

- **频道地址**: youtube.com/@VOANews
- **内容**: 标准英语新闻广播
- **协议**: 公有领域
- **难度**: B2-C1（语速正常，适合进阶学习者）

### 3. NASA

- **频道地址**: youtube.com/@NASA
- **内容**: 太空任务直播、纪录片、科普
- **协议**: 公有领域（美国政府作品）
- **难度**: B1-C1，看具体节目
- **附加价值**: 视觉震撼，二次创作潜力大

### 4. NASA Goddard / NASA JPL（喷气推进实验室）

- **频道地址**: @NASAGoddard / @NASAJPL
- **内容**: 更专业的航天科技纪录片
- **协议**: 公有领域

### 5. The White House

- **频道地址**: youtube.com/@WhiteHouse
- **内容**: 总统讲话、政策发布、新闻发布会
- **协议**: 公有领域
- **注意**: 内容政治性强，对中国市场要谨慎选取（避免敏感话题）

### 6. U.S. Department of State

- **频道地址**: youtube.com/@statedept
- **内容**: 外交、国际关系
- **协议**: 公有领域

### 7. National Archives (US)

- **频道地址**: youtube.com/@usnationalarchives
- **内容**: 历史影像、纪录片
- **协议**: 公有领域
- **价值**: 大量 20 世纪历史素材

### 8. Library of Congress

- **频道地址**: youtube.com/@LibraryOfCongress
- **内容**: 文学、作家访谈、文化讲座
- **协议**: 公有领域 / 部分 CC
- **价值**: 文学课内容首选

### 9. C-SPAN

- **频道地址**: youtube.com/@cspan
- **内容**: 美国国会听证会、政治演讲、政治访谈
- **协议**: 国会录像为公有领域；C-SPAN 自制内容有特殊使用许可（教育用途友好）
- **注意**: 自制部分要逐个验证

### 10. NOAA（美国国家海洋和大气管理局）

- **频道地址**: youtube.com/@NOAA
- **内容**: 气候、海洋、天气科普
- **协议**: 公有领域

### 11. CDC / NIH

- **频道地址**: @CDC / @NIH
- **内容**: 医学健康科普
- **协议**: 公有领域
- **难度**: 适合 B1-B2 健康主题内容

### 12. U.S. Geological Survey (USGS)

- **频道地址**: youtube.com/@usgs
- **内容**: 地质、自然灾害、地震科普
- **协议**: 公有领域

---

## 🥈 第二梯队：CC BY（明确可商用 + 署名即可）

### 13. CrashCourse

- **频道地址**: youtube.com/@crashcourse
- **内容**: 系统科普课程（历史、文学、生物、心理学等）
- **协议**: **大部分 CC BY-SA**（注意：SA 要求衍生作品也用相同协议）
- **难度**: B1-B2
- **价值**: 内容结构化、节奏紧凑，最适合做精讲教材
- **注意**: 部分新视频可能改为 Standard YouTube License，要逐个验证

### 14. Khan Academy

- **频道地址**: youtube.com/@khanacademy
- **内容**: 数学、科学、经济学课程
- **协议**: **CC BY-NC-SA**（⚠️ NC = 非商业）
- **能用吗**: 付费 App **不能直接用**；免费版 App 可以
- **替代方案**: 联系 Khan Academy 申请商用授权（他们对教育项目相对友好）

### 15. MIT OpenCourseWare

- **频道地址**: youtube.com/@mitocw
- **内容**: MIT 真实课程录像
- **协议**: **CC BY-NC-SA**（⚠️ 非商业）
- **付费 App 不能用**

### 16. Stanford Online / Stanford

- **频道地址**: @stanfordonline
- **内容**: 公开课、讲座
- **协议**: **混合** —— 部分 CC BY，部分保留权利
- **必须逐个视频验证**

### 17. TEDx Talks

- **频道地址**: youtube.com/@TEDx
- **内容**: 地方 TEDx 演讲
- **协议**: **混合** —— 有部分 CC BY 视频，但**不是全部**
- **⚠️ 注意**: TED 主频道（@TED）整体是 CC BY-NC-ND（不能商用 + 不能改），**不要混淆**
- **使用方式**: 必须用 yt-dlp 逐个验证

### 18. Easy English / Easy Languages

- **频道地址**: youtube.com/@EasyEnglishVideos / @EasyLanguages
- **内容**: 街头采访、生活英语
- **协议**: 大量 CC BY 内容
- **价值**: 真实口语、口音多样性、生活场景
- **必须逐个验证**

### 19. Wikimedia / Wikipedia 系列

- **频道地址**: youtube.com/@Wikimedia
- **协议**: 大部分 CC BY-SA
- **内容**: 知识科普

### 20. NPR（部分内容）

- **频道地址**: youtube.com/@NPR
- **协议**: **大部分非 CC**，但有少量 CC 标记的内容
- **⚠️ 默认不能用**，需视频级别验证

---

## 🥉 第三梯队：国际 / 跨语种 CC 内容

### 21. European Space Agency (ESA)

- **频道地址**: youtube.com/@EuropeanSpaceAgency
- **协议**: 大部分 CC BY-SA 3.0 IGO
- **价值**: 欧洲口音、与 NASA 互补

### 22. CERN

- **频道地址**: youtube.com/@CERN
- **协议**: 大部分 CC BY-SA
- **内容**: 物理科普

### 23. World Bank

- **频道地址**: youtube.com/@WorldBank
- **协议**: 大部分 CC BY
- **内容**: 经济、发展、国际事务

### 24. International Monetary Fund (IMF)

- **频道地址**: youtube.com/@IMF
- **协议**: 部分 CC

### 25. United Nations / UN Geneva

- **频道地址**: @unitednations / @ungeneva
- **协议**: 部分 CC BY-NC-ND（⚠️ 商用要单独谈）
- **价值**: 国际政治、人权、可持续发展话题

### 26. ABC News (Australia)

- **频道地址**: youtube.com/@abcnewsaustralia
- **协议**: 标准 YouTube 协议为主，少量 CC
- **⚠️ 默认不能用**

---

## ⚠️ 经常被误以为可用、但实际**不能**用于付费 App

> 这些都是 Pollykann 这类盗版 App 大量搬运的内容，**不要踩坑**：

| 频道 | 实际协议 | 备注 |
|---|---|---|
| **BBC Learning English** | **All Rights Reserved** | 完全 BBC 版权，免费看 ≠ 能再分发 |
| **BBC News** | All Rights Reserved | 商业再分发即侵权 |
| **TED**（主频道 @TED） | CC BY-NC-ND | 非商业 + 不允许衍生作品 |
| **CNN / Fox News / NBC** | All Rights Reserved | 商业新闻 |
| **Netflix / Disney+ / 任何院线电影** | All Rights Reserved | 想都别想 |
| **YouTube 网红频道**（绝大多数） | Standard YouTube License | 默认不允许下载分发 |
| **Joe Rogan / Lex Fridman 等播客** | 通常 All Rights Reserved | 即使免费看，也不能搬 |

---

## 🛠 实操工作流建议

### 阶段 1：种子库（第 1 周）

只用 **公有领域内容**，零法律风险：

1. 把 **VOA Learning English** 整个频道抓下来（约 2000+ 视频）
2. 加 **NASA + NASA Goddard + NASA JPL**（再抓 1000+ 视频）
3. 加 **C-SPAN 国会听证会**（深度听力素材）

光这三个频道，你就有了：

- 已分级的英语教材（VOA 自带 Level 1/2/3）
- 高质量画面（NASA）
- 真实演讲与辩论（C-SPAN）

### 阶段 2：扩充（第 2~4 周）

3. 用脚本扫描 **CrashCourse**、**TEDx**、**Easy English**，逐个验证 CC BY 视频
4. 加入 **ESA / CERN / World Bank** 等国际机构

### 阶段 3：自制（持续）

5. 你的独有内容来自 **精讲解说 + 学习路径 + 跟读测评** —— 这才是护城河
6. 等用户和收入起来了，再考虑：花钱采购授权（如老友记的中国区流媒体授权） + 自己拍

---

## 📋 频道级抓取的速查命令

把下面这些 channel ID 喂给 `yt_cc_scraper.py --channel <id>`：

```
# VOA Learning English
UCKyTokYo0nK2OA-az-sDijA

# NASA
UCLA_DiR1FfKNvjuUpBHmylQ

# NASA JPL
UCcomT2ynxR8DDygTOMjI-OQ

# CrashCourse
UCX6b17PVsYBQ0ip5gyeme-Q

# The White House
UCYxRlFDqcWM4y7FfpiAN3KQ

# C-SPAN
UCb--64Gl51jIEVE-GLDAVTg

# TEDx Talks
UCsT0YIqwnpJCM-mx7-gSA4Q

# Easy English
UCnsekZyXnEsbtdsmCdwM3eg
```

**注意**：channel ID 可能随平台更新而变化，建议用频道首页 URL 实时获取。

---

## 一句话总结

> **VOA Learning English + NASA + CrashCourse** 三个频道，再加你自己的产品设计，就能合法地启动一个英语学习 App。剩下的 20% 内容慢慢扩，但不要碰 BBC / Netflix / 商业新闻这些"明知故犯"的内容 —— 那条路 Pollykann 已经走过了，结局你看得到。
