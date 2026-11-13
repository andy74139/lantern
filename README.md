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

- **版面**：最上面是一週行程（今天起 7 天，每天附天氣），下面是全寬的待辦事項，再往下是 AI 工作；音樂收在右上角的「♪ 音樂」。
- **目錄**：寬螢幕固定在左邊，窄螢幕在左下角的「☰ 目錄」，點了直接跳到行程、待辦、常用工作、錄音整理、Ollama、Claude 對話、最近執行。
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

- 快速輸入會自動抓日期時間與重複，例如「明天下午3點 打給王經理」「下週一 9:30 開會」「每月5號 繳房租」「平日 早上9點 晨會」。
  沒寫上午下午的 1–6 點當成下午。
- **離線也能用**：所有修改先存在這台電腦（`data/controller.db`），連得上 Google 時自動雙向同步
  （修改後幾秒、或每 5 分鐘）。兩邊都改過同一項時，以修改時間較晚的為準。
- Google Tasks 只存「日期」，所以**時間、重複、釘選只存在這台電腦**；在手機上看到的是同一天、沒有時間。
- 重複的待辦完成後，會自動建立下一次（也會同步到 Google）。
- 錄音摘要裡的待辦，可以在錄音的詳細視窗按「加入待辦」，同一項不會重複加入。
- 分頁：「今天」（含已過期）、「接下來」（依日期）、「全部」、「已完成」（最近 7 天）。
- 每項右邊的 ⋯：今天／明天／下週一／選日期／清除日期、釘選、移到別的清單、編輯、刪除。
- 完成或刪除後，提示框裡可以按「復原」。
- 有多個 Google Tasks 清單時，可以篩選清單、新增時選清單、把項目移到別的清單。
- **看板**：「看板」分頁把每個 Google Tasks 清單當成一欄。卡片可以拖到別欄（＝移到那個清單，會同步到 Google）或上下拖拉排序（排序只存在這台電腦）。
  每欄下方可以直接新增（一樣能寫「明天下午3點」）；最右邊「＋ 新增清單」、欄位的 ⋯ 可以改名或刪除清單（會同步到 Google；還有未完成待辦的清單不能刪）。
- **清單檢視也能拖**：拖一項待辦時，最上面會出現「今天／明天／沒有期限」，拖進去就改日期。
- 快捷鍵：`N` 新增、`/` 搜尋、`M` 音樂、`Esc` 關閉。
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

## 知識庫

在首頁的「知識庫」問問題，會從你的**錄音**（逐字稿與摘要，自動收錄）和**加入的資料夾**（PDF、Word、PowerPoint、Markdown、文字檔、網頁）找出相關段落，再請模型回答，每個說法都附出處 `[1]`、`[2]`。

- 點出處：錄音會打開並跳到那一句（逐字稿標亮、播放器移到那個時間）；文件會用預設的 App 開啟，並告訴你在第幾頁／第幾張投影片。
- 搜尋同時用語意（本機的向量模型 `qwen3-embedding:0.6b`，中英文可以互相找到）與關鍵字（人名、術語），再合併排序。
- 回答可以選 **Claude**（品質較好，找到的相關段落會傳給 Anthropic）或 **本機模型**（完全不離開這台電腦）。
- 索引存在 `data/kb.db`，每 10 分鐘自動更新；加入或移除資料夾、按「重新掃描」會立刻更新。移除資料夾不會刪除你的檔案。
- 設定在 `config/kb.json`（資料夾清單、模型）；不會進 git，範本是 `config/kb.example.json`。
- 第一次使用前要下載向量模型：`ollama pull qwen3-embedding:0.6b`。

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
| `status.cmd` | 查詢狀態的指令，輸出會顯示在卡片上（`every` 秒更新一次） |
| `progress` | 從記錄檔最後一筆符合 `regex` 的文字取進度；`done`／`total`／`note` 是擷取群組的編號 |
| `log` | 卡片展開時顯示的記錄檔 |

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

### 設定

設定在 `config/recordings.json`，改完不用重啟：

| 欄位 | 說明 |
|---|---|
| `summary_model` | 預設用來整理的模型：Ollama 模型名稱，或 `claude`（頁面上可以臨時改） |
| `claude_model` | 用 Claude 整理時的模型，預設 `sonnet` |
| `num_ctx` | 本機模型整理時的上下文長度；模型已經載入時會沿用它目前的長度，避免重新載入 |
| `glossary` | 專有名詞清單，例如 `["實發", "自提", "待確認清單"]`；會提示給轉錄和整理兩個步驟，減少同音錯字 |
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
- `data/recordings/<編號>/`：每筆錄音的音檔、`transcript.txt`、`summary.md`、`job.log`
- `models/`：語音模型：`models/breeze/`（Breeze-ASR-25，1.66 GB）、large-v3-turbo（1.6 GB）與語音偵測模型
- `data/claude_cache.json`：Claude 對話摘要的快取，可以直接刪掉
- Claude 的對話紀錄讀自 `~/.claude/projects`，只讀不寫
