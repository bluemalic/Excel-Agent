# 11 · 演示视频脚本（2 分 25 秒）

> 成片：`recordings/excel_agent_demo.mp4`（1280×720，25fps，H.264，145.5 秒，无音轨）。
> 本文档给负责**配音与字幕**的同学使用：分镜与台词已按成片时间轴对齐，按章配音后拼回即可。

## 1. 素材与交付物

| 文件 | 用途 |
|---|---|
| `recordings/excel_agent_demo.mp4` | 画面底稿（无音轨） |
| `docs/assets/demo_narration.txt` | 纯台词稿（可直接分段喂 TTS） |
| `docs/assets/demo.srt` | 字幕草稿（时间轴已按章节预排，TTS 后微调） |

## 2. 分镜与台词

| # | 时间 | 画面（成片中实际发生的事） | 台词 |
|---|---|---|---|
| 1 | 0:00–0:03 | 标题卡 | 大家好，这是我们的课程项目——Excel 对话式数据分析智能体。 |
| 2 | 0:03–0:17 | 一键载入销售台账示例；输入「各大区销售额总和是多少？」；回答带表格与执行的 SQL | 在左侧一键载入内置的销售台账示例，然后直接用中文提问：各大区销售额总和是多少。系统解析表结构后生成 SQL，在 DuckDB 沙箱里执行，回答里附带可复现的查询语句。 |
| 3 | 0:18–0:29 | 追问「把上面的结果画成柱状图」；柱状图出现在右侧图表页 | 接着追问：把上面的结果画成柱状图。工作记忆记住了上一轮的结果，直接对它二次加工，图表自动出现在右侧面板。 |
| 4 | 0:29–0:45 | 「分析订单明细有没有异常，并给出相关性结论」；右侧切到「反思」页 | 再让它分析订单明细有没有异常。系统做了空值、离群和环比扫描，并给出相关性结论；右侧反思页可以看到 R1 规则校验和 R2 复核的完整轨迹。 |
| 5 | 0:45–1:02 | 追问「那华南呢？也按月份看趋势」；华南月度明细表 | 用一句「那华南呢」继续追问。指代消解把它改写成完整问题，华南的月度明细表直接生成。 |
| 6 | 1:02–1:14 | 「把华南每个月的销售额画成折线图」；新折线图 | 要求把华南每个月的销售额画成折线图，新图表追加到同一个会话的图表列表里，历史图表不会被覆盖。 |
| 7 | 1:14–1:38 | 均值填充缺失值：diff 卡（旧红新绿）→ 确认 → 撤销；单元格编辑 → 确认 → 撤销；版本历史出现「已撤销」 | 接下来是改数据。均值填充缺失值和单元格编辑，都会先弹出红绿 diff 预览，确认后才落盘；每一步都有逆操作日志，点撤销可以回到任意版本，撤销回初始状态时与原始文件逐字节一致。 |
| 8 | 1:38–2:03 | 「帮我出一份月度经营小结」；右侧「报告」页生成分析报告；导出 xlsx 与 PDF | 让它出一份月度经营小结，回答基于刚才的执行结果，右侧报告页同步生成；一键导出带样式的 xlsx 和 PDF 报告。 |
| 9 | 2:03–2:25 | 新建会话；会话历史打开旧会话（聊天/图表恢复）；「记忆」面板；快照恢复；回到预览 | 最后新建会话，再从历史记录里打开刚才的会话：聊天、数据绑定、图表都会恢复；记忆面板展示 L1 会话摘要、L2 工作记忆和 L3 长期记忆，快照恢复则把数据回到保存的状态。以上就是本项目的完整演示，谢谢观看。 |

> 台词合计约 560 字；按 4.2–4.6 字/秒的语速，每章都能在画面时间内讲完。
> 语速偏快可整段加 5%（见 §4），不要压缩停顿。

## 3. 录制环境（如需重录某一段）

```powershell
# PowerShell：使用隔离数据根 + 指定端口，避免污染主数据
$env:EXCEL_AGENT_DATA_ROOT = "data_demo"
$env:EXCEL_AGENT_PORT = "7862"
.venv\Scripts\python.exe app.py
```

```bash
# Git Bash
EXCEL_AGENT_DATA_ROOT=data_demo EXCEL_AGENT_PORT=7862 .venv/Scripts/python.exe app.py
```

录制前检查：

- [ ] 数据根为空目录（首次会话标题干净）
- [ ] 视口 1280×720；浏览器缩放 100%
- [ ] 未登录任何含密钥的页面；画面中不出现真实 Key、本机绝对路径
- [ ] 每个提问等待上一轮完全结束（停止按钮消失）再发送下一条

## 4. 配音（TTS）

推荐 `edge-tts`（免费、中文自然）：

```powershell
pip install edge-tts
# 逐章生成（示例第 2 章），--rate 控制语速
edge-tts --voice zh-CN-YunxiNeural --rate=+5% ^
  --text "在左侧一键载入内置的销售台账示例，……" ^
  --write-media ch02.mp3 --write-subtitles ch02.srt
```

- 音色：`zh-CN-YunxiNeural`（男）或 `zh-CN-XiaoxiaoNeural`（女），任选其一并全片统一。
- 按章节分别生成 9 段音频与各自 SRT，再拼接；**不要**把整篇文稿一次生成后变速对齐。
- 章节间隙留 0.3–0.5s 静音（`ffmpeg -f lavfi -i anullsrc=r=24000:cl=mono -t 0.4 gap.mp3`）。
- `edge-tts --write-subtitles` 输出的 SRT 基于 word boundary，比手排时间轴更准；可与 §6 的 `demo.srt` 合并校正。

## 5. 音画合成

```powershell
# 1) 拼接 9 段配音（含静音间隙）→ narration.m4a
# 2) 与画面混流（软字幕，播放器可开关）
ffmpeg -i excel_agent_demo.mp4 -i narration.m4a -i demo.srt `
  -c:v copy -c:a aac -b:a 160k -c:s mov_text -shortest `
  excel_agent_demo_vo.mp4
```

## 6. 字幕

- 样式建议：微软雅黑 22px、白字黑边（Outline=1）、底部边距 24px、每行 14–18 字、最多 2 行。
- 软字幕（mov_text）交付方便但部分播放器不显示；交作业建议**烧录硬字幕**：

```powershell
ffmpeg -i excel_agent_demo.mp4 -i narration.m4a `
  -vf "subtitles=docs/assets/demo.srt:force_style='FontName=Microsoft YaHei,FontSize=22,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=1,Outline=1,Shadow=0,MarginV=24'" `
  -c:v libx264 -crf 20 -pix_fmt yuv420p -c:a aac -b:a 160k -shortest `
  excel_agent_demo_final.mp4
```

- 时间轴校正：以 `edge-tts` 逐章输出的 SRT 为准，整体按该章起始时间平移；`docs/assets/demo.srt` 的预排时间仅供参考。
- 背景音乐（可选）：纯氛围垫乐，音量 −20dB 以下，不要盖过人声。

## 7. 剪辑核对清单

- [ ] 每章画面与台词一一对应，无「说了但画面没演」的内容
- [ ] 全片无真实密钥、无本机绝对路径、无个人姓名/学号
- [ ] 台词未使用「100% 准确」「完全自动」等绝对化表述
- [ ] 片头 3 秒标题卡保留；片尾停在数据预览或标题卡
- [ ] 输出 H.264 / yuv420p / 25fps / 1280×720，与底稿一致
- [ ] 成片命名建议 `excel_agent_demo_vo.mp4`，保留无音轨底稿
