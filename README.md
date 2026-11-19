# 首頁（本機 AI 控制台）

開機後的首頁：天氣、今天與接下來 7 天的行程、待辦事項、YouTube Music 常播清單；
下方是這台 Mac 的 AI 工作（常用工作、錄音整理、Ollama、Claude 對話）。

## 從 GitHub 下載後的安裝

repo 裡不含個人設定、執行資料和語音模型（見 `.gitignore`），第一次要自己建立：

```bash
python3 -m venv .venv && .venv/bin/python -m pip install fastapi "uvicorn[standard]" httpx icalendar recurring-ical-events opencc-python-reimplemented numpy pypdf
ollama pull qwen3-embedding:0.6b                  # 知識庫的向量模型
cp config/home.example.json config/home.json      # 地點、日曆
cp config/tasks.example.json config/tasks.json    # 常用工作
cp config/kb.example.json config/kb.json          # 知識庫
mkdir -p models/breeze
curl -L -o models/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
curl -L -o models/ggml-silero-v5.1.2.bin https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin
curl -L -o models/breeze/ggml-breeze-asr-25-q8_0.bin https://huggingface.co/shdennlin/breeze-asr-25-ggml/resolve/main/ggml-breeze-asr-25-q8_0.bin
./autostart.sh install
```

另外需要 `brew install ffmpeg whisper-cpp`、Ollama，以及已登入的 Claude Code（`claude`）。
Google 連結要另外建立自己的用戶端（首頁按「連結 Google」照步驟做），`config/google_client.json` 與 `data/` 都不會進 git。

## 啟動

已設定成登入後自動啟動，直接開 <http://localhost:8765> 即可。服務只聽本機，其他裝置連不到。

> 首頁要用 `localhost` 開：YouTube 不允許在 `127.0.0.1` 這種 IP 網址上內嵌播放（會出現錯誤 150），
> 所以開 `127.0.0.1:8765` 會自動轉到 `localhost:8765`。

```bash
./autostart.sh status      # 看是否在執行
./autostart.sh restart     # 改了 app/ 裡的 Python 程式後要重啟
./autostart.sh uninstall   # 取消自動啟動
./autostart.sh install     # 重新安裝
```

- 服務當掉會自動重啟；記錄在 `data/server.log`。
- 重啟或停止控制台，不會影響它在背景啟動的長時間工作。
- 改 `static/` 或 `config/tasks.json` 不用重啟；開著的首頁會在一分鐘內自動重新整理（正在播音樂或打字時，會改成跳出「重新整理」的提示）。
- 沒裝自動啟動時，可以用 `./start.sh` 手動啟動（兩者不能同時用同一個連接埠）。
- 開機後 10 分鐘內登入會自動打開首頁（`open_home.sh`）。如果你把首頁釘選在 Arc 裡，
  可以把 `config/home.json` 的 `open_on_login` 改成 `false`，避免多開一個分頁。

## 首頁

設定在 `config/home.json`，改完不用重啟：

| 欄位 | 說明 |
|---|---|
| `location` | 天氣的地點（名稱與經緯度），預設台北市 |
| `calendars` | 要顯示的日曆。`role: "main"` 是主要行程；`"optional"` 顯示成灰色並標「提醒」，代表不一定參加 |
| `calendars[].google_id` | Google 日曆 ID（主要日曆寫 `primary`），透過 Google 連結讀取 |
| `calendars[].ics_url` | 也可以改填 iCal 的「私人網址」，不需要連結 Google（公開網址只有日曆設為公開時才能用） |
| `open_on_login` | 開機後是否自動打開首頁 |
| `todo_notifications` | 有時間的待辦到期時跳 macOS 通知（預設開；改成 `false` 關閉） |

- **兩個頁面**：上方切換「首頁」與「AI 工作」（網址 `#ai` 直接開 AI 工作；有工作或錄音正在跑時，分頁上會顯示「N 執行中」）。
  - 首頁：知識庫搜尋列、一週行程（今天起 7 天，每天附天氣）、待辦看板、ASA 準備進度。視窗寬度 1380 以上時，待辦和 ASA 左右並排。
  - AI 工作：錄音整理、常用工作、Ollama（已安裝的模型預設收合）與 Claude 對話（先顯示 8 筆）、最近執行（先顯示 5 筆）。
- **知識庫搜尋列**：最上面輸入問題按 Enter；回答在搜尋列下方展開，✕ 收起，⋯ 看資料來源與最近的問題。
- **目錄**：寬螢幕固定在左邊，窄螢幕在左下角的「☰ 目錄」；點另一頁的區塊會自動切換頁面再捲過去。
- 音樂收在右上角的「♪ 音樂」。
- **天氣**：Open-Meteo（免金鑰），每 30 分鐘更新，7 天預報；降雨機率取白天 8–20 點的最大值，今天和明天會提醒帶傘。
- **行事曆**：每 10 分鐘更新。沒網路時顯示上次的資料，並標示「離線」。

### 連結 Google（待辦事項與行事曆共用）

第一次在首頁按「連結 Google」，照對話框的步驟做一次（約 5 分鐘）：建立 Google Cloud 專案、
啟用 Google Tasks API 與 Google Calendar API、設定 OAuth 同意畫面（維持「測試中」並把自己加成測試使用者）、
建立「電腦版應用程式」用戶端並把 JSON 檔選進來，最後到 Google 授權。

- 個人 Gmail 要「發布成正式版」必須先填網域與隱私權政策（Branding），所以維持測試中；代價是**授權每 7 天失效**，
  到時首頁會提示「重新連結」，按一下重新授權即可，期間待辦事項照常能用，連結後自動補同步。
- 行事曆建議改用 iCal「私人網址」（填在 `config/home.json` 的 `ics_url`），就不受 7 天限制。

- 用戶端設定存在 `config/google_client.json`，授權存在 `data/google_token.json`，都只有你自己讀得到。
- 權限：Google Tasks（讀寫）、日曆（唯讀）。

### 待辦事項

- **只用看板**：每個 Google Tasks 清單一欄，在欄位下方新增。
- 新增時會自動抓日期時間與重複，例如「明天下午3點 打給王經理」「下週一 9:30 開會」「每月5號 繳房租」「平日 早上9點 晨會」。
  沒寫上午下午的 1–6 點當成下午。
- **離線也能用**：所有修改先存在這台電腦（`data/controller.db`），連得上 Google 時自動雙向同步
  （修改後幾秒、或每 5 分鐘）。兩邊都改過同一項時，以修改時間較晚的為準。
- Google Tasks 只存「日期」，所以**時間、重複、釘選只存在這台電腦**；在手機上看到的是同一天、沒有時間。
- 重複的待辦完成後，會自動建立下一次（也會同步到 Google）。
- 錄音摘要裡的待辦，可以在錄音的詳細視窗按「加入待辦」，同一項不會重複加入。
- 搜尋（按 `/`）時改成列出符合的項目（未完成，以及最近 7 天完成的）。
- 每項右邊的 ⋯：今天／明天／下週一／選日期／清除日期、釘選、移到別的清單、編輯、刪除。
- 完成或刪除後，提示框裡可以按「復原」。
- **看板**：卡片可以拖到別欄（＝移到那個清單，會同步到 Google）或上下拖拉排序（排序只存在這台電腦）。
  每欄下方可以直接新增（一樣能寫「明天下午3點」）；最右邊「＋ 新增清單」、欄位的 ⋯ 可以改名或刪除清單（會同步到 Google；還有未完成待辦的清單不能刪）。
- 快捷鍵：`N` 新增（跳到第一欄的新增框）、`/` 搜尋、`M` 音樂、`Esc` 關閉。
- 有時間的待辦到期時會跳 macOS 通知（超過 15 分鐘的不補發）；分頁標題會顯示今天加過期的數量。

### 音樂

- **讓它出現**：點右上角「♪ 音樂」（或按 `M`）→ 右邊滑出音樂面板，在清單裡點一首就開始播。
- **收起但繼續聽**：再點一次「♪ 音樂」、按 `Esc`、按面板右上角的 ⇥，或點頁面其他地方 → 面板收起，歌曲還載入著的話縮成**右下角的小播放器**（可以暫停、⤢ 展開）。
- **讓它完全消失**：在右下角小播放器按 **✕** → 停止播放並關掉。
- **聽音樂但不要小播放器**：在小播放器按 **⌄**（或在面板裡取消勾選「收起面板時，在右下角顯示小播放器」）→ 收起後完全看不到，音樂照樣播；要回來就點右上角的「♪ 播放中」。
- 右上角的按鈕會顯示目前狀態：「♪ 音樂」（沒有載入）、「♪ 播放中」、「♪ 已暫停」。
- 重新整理頁面後不會自動載入上次的歌，所以小播放器只會在你按了播放之後出現。
- 在 YouTube Music 按「分享 → 複製連結」，貼到面板裡的輸入框就會加入；歌曲或播放清單都可以。
- 從首頁播放的次數會排出「最近常播」（兩週沒播，分數減半）；可以用 ☆ 釘選在最上面。「單曲循環」預設開啟。
- 瀏覽器不允許開機後自動出聲，第一次要按一下播放。少數歌曲不允許在其他網頁播放，會顯示「在 YouTube Music 開啟」的連結。

### Claude 用量上限

- 狀態列最後一格：`Claude 工作中/開啟 │ 5 小時 xx% │ 本週 xx%`；70% 以上橘色、90% 以上紅色，游標停上去看重置時間。
- **首頁開著時每 5 分鐘自動更新**：用一個極小的請求（Haiku、一個字、不開工具、不留紀錄）向 Claude 取得最新的上限百分比；
  點一下用量那格會立刻更新。首頁 10 分鐘沒有人看就停止查詢。
- 終端機的 Claude Code 每次回覆時，也會透過狀態列指令（`claude_statusline.py`，設定在 `~/.claude/settings.json`）順便更新。
- 不讀任何登入資訊；需要終端機的 `claude` 已登入。

## ASA 準備進度

首頁的「ASA 準備進度」（目錄裡也有）：

- **環形圖**：完整進度、FAM、ALTAM、PA，旁邊是考試倒數和學習頁連結（「設定」可以改考試日、連結網址和連結文字，只接受 https:// 網址）。
  完整進度＝考試（P、FM、FAM、SRM、PA、ALTAM 六科）佔 70%＋其他項目（ATPA、VEE、FAP、APC）佔 30%；已通過算滿，準備中的考科依讀書進度完成的比例計入。
- **全部項目**：環形圖下方，考試一列、其他項目一列；點一下標籤改狀態（未開始／準備中／已通過／已抵免）、日期和備註，也可以刪除。
- **讀書進度**：Topic 1、2…依 SOA 官方 syllabus（FAM 2026 年 2 月版、ALTAM 2026 年 10 月版、PA 2026 年 4 月版），標出官方權重範圍；
  自己加的項目（刷題、模擬考）不編號，一樣計入進度。日期全部由你自己安排（可以不填），系統不會自動排或改日期。拖拉 ⋮⋮ 調整順序。
  預設收合，只顯示各科的下一項；點「讀書計畫」展開（會記住）。點項目名稱可以設定這一項的學習頁連結。
- SOA 改版時更新 `app/asa.py` 的 `SYLLABUS`。資料存在 `data/controller.db`（不會進 git）。

## 知識庫

在首頁最上面的知識庫搜尋列問問題，會從你的**錄音**（逐字稿與摘要，自動收錄）和**加入的資料夾**（PDF、Word、PowerPoint、Markdown、文字檔、網頁）找出相關段落，再請模型回答，每個說法都附出處 `[1]`、`[2]`。

- 點出處：錄音會打開並跳到那一句（逐字稿標亮、播放器移到那個時間）；文件會用預設的 App 開啟，並告訴你在第幾頁／第幾張投影片。
- 搜尋同時用語意（本機的向量模型 `qwen3-embedding:0.6b`，中英文可以互相找到）與關鍵字（人名、術語），再合併排序。
- 回答可以選 **Claude**（品質較好，找到的相關段落會傳給 Anthropic）或 **本機模型**（完全不離開這台電腦）。
- 索引存在 `data/kb.db`，每 10 分鐘自動更新；加入或移除資料夾、按「重新掃描」會立刻更新。移除資料夾不會刪除你的檔案。
- 設定在 `config/kb.json`（資料夾清單、模型）；不會進 git，範本是 `config/kb.example.json`。
- 第一次使用前要下載向量模型：`ollama pull qwen3-embedding:0.6b`。

## 使用紀錄（調整版面用）

首頁會記錄你平常怎麼用這個頁面，只存在本機的 `data/controller.db`（不會進 git、不會送出去）：

- **在看什麼**：每個區塊實際在畫面上的時間，依佔畫面的比例分配。只在分頁在前景、視窗有焦點、而且最近 60 秒內有滑鼠／鍵盤／捲動時才算，頁面只是開著沒人看的時間不算。
- **做了什麼**：按了哪些功能（動作名稱和所在區塊）的次數。不記內容，例如待辦文字、錄音標題、問的問題。
- 首頁／AI 工作各看了多久、視窗大小。
- 看結果：`curl -s localhost:8765/api/activity?days=30`。某個瀏覽器不想被記錄（例如測試用）：在那個瀏覽器的主控台執行 `localStorage.setItem('activity-off', '1')`。

## iPhone／iPad（iCloud 匯出）

把首頁的資料放一份唯讀快照到 iCloud Drive 的「Lantern」資料夾，給 iPhone／iPad 上的 Lantern App 讀（App 的專案在另一個資料夾 `lantern_ios`）。

- 開啟方式：`cp config/icloud.example.json config/icloud.json`，把 `enabled` 改成 `true`；一分鐘內生效，不用重啟。
- 匯出的內容：行程與天氣、待辦（未完成＋最近 7 天完成）、ASA 進度、錄音的摘要與逐字稿。`export` 裡可以逐項關掉，關掉後 iCloud 上那份會自動移除。
- **不會放進 iCloud**：錄音音檔、處理記錄、知識庫索引、Google 授權、Claude 對話。目前全部約 250 KB。
- 每分鐘檢查一次，內容有變才寫檔；沒變的話每 15 分鐘只更新一個很小的 `meta.json`，讓手機知道 Mac 還開著。
- 只有這台 Mac 會寫 `state/` 裡的檔案。目前 App 是唯讀的，不會改到這裡的任何資料。
- 狀態：`curl -s localhost:8765/api/icloud`。如果顯示沒有權限，到「系統設定 → 隱私權與安全性 → 檔案與檔案夾」打開 python 的「iCloud Drive」。

| 欄位 | 說明 |
|---|---|
| `enabled` | 是否匯出 |
| `folder` | 匯出的位置，預設是 iCloud Drive 裡的 `Lantern` |
| `interval` | 幾秒檢查一次，預設 60 |
| `budget_mb` | 佔用上限（顯示在 App 的設定頁），預設 200 |
| `export` | 要匯出的項目：`home`、`todos`、`asa`、`recordings` |

## 新增常用工作

編輯 `config/tasks.json`，存檔後重新整理頁面就會出現，不用重啟。網頁只能執行這個檔案裡列出的指令。

```json
{
  "id": "meeting-notes",
  "name": "錄音整理",
  "description": "把錄音轉成逐字稿並整理重點",
  "cwd": "/Users/你的帳號/某個資料夾",
  "actions": [
    {
      "id": "run",
      "label": "整理一個錄音檔",
      "cmd": ["python3", "transcribe.py", "{audio}"],
      "long": true,
      "inputs": [{ "id": "audio", "label": "錄音檔", "type": "file" }]
    }
  ]
}
```

| 欄位 | 說明 |
|---|---|
| `cwd` | 指令執行的資料夾 |
| `actions[].cmd` | 指令與參數的陣列（不經過 shell）；`{輸入的 id}` 會換成使用者填的值 |
| `actions[].long` | `true` 表示在背景執行，控制台關掉也會繼續跑；輸出存在 `data/runs/` |
| `actions[].when` | `running`＝只在執行中顯示；`stopped`＝只在未執行時顯示 |
| `actions[].confirm` | 執行前要先確認的文字 |
| `actions[].inputs` | 執行前要填的欄位；`type` 為 `file`／`folder` 時會有「選擇…」按鈕 |
| `actions[].timeout` | 非背景指令的秒數上限，預設 120 |
| `running.pidfile` | 工作自己寫的 PID 檔，用來判斷是否執行中 |
| `running.process` | 沒有 PID 檔時，用程序名稱判斷是否執行中（`pgrep -f`），例如 `mlx_vlm.train` |
| `status.cmd` | 查詢狀態的指令，輸出會顯示在卡片上（`every` 秒更新一次） |
| `progress` | 從記錄檔最後一筆符合 `regex` 的文字取進度；`done`／`total`／`note` 是擷取群組的編號。`note_format`（例如 `"剩 {3}・loss {4}"`）可以組合多個群組，`note_prefix` 是前面的字（預設「預估剩 」） |
| `progress.file`、`log` | 記錄檔；可以用萬用字元（例如 `logs/*.log`），會取最近修改的那個，`exclude`／`log_exclude` 排除符合的檔名。進度條用 `\r` 覆寫的行會只留最後狀態 |
| `progress.state` | 分段接續的工作用：一個 JSON 檔（例如 `{"offset": 200, "total": 418}`），顯示的進度是 offset 加上這一段的進度、總量用 total |

要交給 Claude 做的工作也可以寫成指令，例如 `["claude", "-p", "整理 {folder} 裡的文件"]`。

## 錄音整理

把錄音或影片拖進頁面上的「錄音整理」區塊，會依序：

1. 用 ffmpeg 轉成音檔（留一份小的 `audio.m4a` 供頁面播放，上傳的原檔轉完就刪掉）
2. 用 whisper.cpp（`whisper-cli`）轉成逐字稿。轉錄模型可以選：
   - **準確（預設）**：[Breeze-ASR-25](https://huggingface.co/MediaTek-Research/Twister)（聯發科，以 Whisper large-v2 為台灣華語與中英夾雜調校）。
     中英夾雜的段落能直接轉對、中文專業用詞較準；純中文比 turbo 慢約一倍；原本沒有標點，會把中文字之間的空格換成逗號。
   - **快速**：whisper large-v3-turbo。速度快，但遇到英文段落容易整段出錯或重複。
3. 整理出摘要、重點、決議、待辦事項、待確認：可以選 **Claude**（品質較好，逐字稿會傳給 Anthropic），或本機的 Ollama 模型（不離開這台電腦）

一次只處理一筆，其餘排隊。有常用工作（例如掃描）在執行時，可以選「等其他工作結束後自動開始」。
點摘要或逐字稿裡的時間會跳到錄音的那個位置。

### 修改與重新整理

- **編輯**：錄音視窗的「摘要」「逐字稿」分頁都有「✎ 編輯」。逐字稿每句保留時間、只改文字（清空就刪掉那句）；摘要可以改標題和內容（Markdown）。
- **用 Claude 整理**：按「✦ 用 Claude 整理」，會把**逐字稿**（不含音檔）交給本機的 Claude Code（`claude -p`，關閉所有工具、不留對話紀錄）整理，幾秒到一分鐘。
  會用到你 Claude 方案的用量；預設用 Sonnet（`config/recordings.json` 的 `claude_model`）。
- **某一段語言錯了**（例如中文錄音裡的一段英文被吃掉、或轉成意思不通的中文）：在「逐字稿」按「重新辨識一段…」，填開始、結束時間和那段的語言，
  只會用那個語言重新轉錄那一段並換掉；之後摘要會標「待更新」，再按「用 Claude 整理」即可。
- 逐字稿改過（手動或重新辨識）後，摘要會標成「摘要待更新」。
- 摘要會要求模型用自己的話歸納、合併相關內容，不逐句照抄逐字稿（Claude 做得最好；本機的小模型偶爾還是會照抄）。

### 詞彙與修正（越用越準）

錄音區右上角的「詞彙與修正」：

- **專有名詞**：一行一個。轉錄時放進 whisper 的提示（約 150 字上限，前面的優先），整理摘要時也會告訴模型正確寫法。
- **自動修正**：手動修改逐字稿並儲存時，系統會比對每句修改前後，記下像「換了一個詞」的修改（例如 `理陪 → 理賠`、`lucket in → locked in`）。
  - 整句改寫、只改標點的不算；只改一個中文字的會帶上前後一個字，避免誤換。
  - 同一組改過 2 次就變成「自動套用」，之後轉錄完直接換掉，錄音資訊會顯示「自動修正 N 處」。
  - 你把自動修正改回去的話，那組會自動停用。也可以在這裡手動新增、改狀態或刪除。
- 每筆錄音第一次手動修改前的原稿會存成 `transcript.orig.json`；對話框會顯示已校正幾筆、幾分鐘。將來累積到幾個小時，可以評估要不要拿來微調語音模型。

詞彙表與修正字典存在 `data/asr_vocab.json`（不進 git）。

### 設定

設定在 `config/recordings.json`，改完不用重啟：

| 欄位 | 說明 |
|---|---|
| `summary_model` | 預設用來整理的模型：`claude`（預設，品質最好、約十幾秒）或 Ollama 模型名稱（頁面上可以臨時改） |
| `claude_model` | 用 Claude 整理時的模型，預設 `sonnet` |
| `num_ctx` | 本機模型整理時的上下文長度；模型已經載入時會沿用它目前的長度，避免重新載入 |
| `glossary` | 固定的專有名詞（會和頁面上「詞彙與修正」的詞合併），例如 `["實發", "自提"]`；提示給轉錄和整理兩個步驟 |
| `language` | 預設語言：`zh`、`en`、`ja`、`auto` |
| `traditional_chinese` | 把轉錄結果的簡體字轉成繁體（只轉字形，不改用詞） |
| `initial_prompt` | 給 whisper 的開頭提示，用來引導標點與用字 |
| `asr` | 預設的轉錄模型：`breeze` 或 `turbo` |
| `asr_models` | 轉錄模型清單（名稱、模型檔、是否補標點） |
| `whisper_model`、`vad_model` | 備用模型檔與語音偵測模型（在 `models/`） |

選本機模型時：標示「會思考，較慢」的模型會先產生一段思考，摘要容易被截斷（會標示「摘要不完整」）；有其他工作正在用同一個模型時，請求會排在它後面。

也可以不經過頁面直接跑：`jobs/transcribe.py <錄音資料夾>`（資料夾裡要有 `meta.json`）。

## 資料位置

- `data/controller.db`：待辦事項、音樂清單、執行紀錄
- `data/kb.db`：知識庫的索引與問答紀錄
- `data/cache/`：天氣與行事曆的快取（離線時顯示）
- `data/google_token.json`、`config/google_client.json`：Google 授權（只有你讀得到）
- `data/recordings/<編號>/`：每筆錄音的音檔、`transcript.txt`、`summary.md`、`job.log`；手動改過的還有 `transcript.orig.json`（修改前原稿）
- `data/asr_vocab.json`：專有名詞與自動修正字典
- `models/`：語音模型：`models/breeze/`（Breeze-ASR-25，1.66 GB）、large-v3-turbo（1.6 GB）與語音偵測模型
- `data/claude_cache.json`：Claude 對話摘要的快取，可以直接刪掉
- Claude 的對話紀錄讀自 `~/.claude/projects`，只讀不寫
