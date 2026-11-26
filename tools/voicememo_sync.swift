// 語音備忘錄同步小幫手：把「語音備忘錄」App 新增的錄音複製到 Lantern 的收件匣，讓首頁問你要不要整理。
//
// 為什麼是獨立的小程式：語音備忘錄的資料夾受 macOS 保護，讀取要「完整磁碟取用權限」。
// 把權限給這支只做一件事的程式，比給 Python（所有 Python 腳本都會跟著拿到權限）安全得多。
//
// 它只做這些事：
//   - 只讀語音備忘錄的資料夾，不修改、不刪除任何原始錄音
//   - 新錄音（設定的起始時間之後、檔案已經同步完成）複製到 <dest>/inbox/，旁邊放一個 .json（標題、錄音時間、長度）
//   - 每次執行把結果寫到 <dest>/heartbeat.json，首頁用它顯示狀態（例如還沒有權限）
//   - 不連網路
//
// 用法：voicememo-sync --dest <Lantern/data/voicememos> [--src <錄音資料夾>]
// 由 LaunchAgent 每分鐘執行一次（見 voicememos.sh）。
import Foundation
import SQLite3

let fm = FileManager.default
var src = NSHomeDirectory() + "/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
var dest = ""
var args = CommandLine.arguments.dropFirst().makeIterator()
while let a = args.next() {
    switch a {
    case "--src": src = args.next() ?? src
    case "--dest": dest = args.next() ?? ""
    default: break
    }
}
guard !dest.isEmpty else {
    FileHandle.standardError.write("用法：voicememo-sync --dest <資料夾> [--src <錄音資料夾>]\n".data(using: .utf8)!)
    exit(2)
}
let inbox = dest + "/inbox"
let statePath = dest + "/state.json"
let EXTS: Set<String> = ["m4a", "qta"]
let SETTLE: TimeInterval = 20          // 檔案最後修改超過幾秒才算同步完成
let FIRST_RUN_BACK: TimeInterval = 86400  // 第一次執行時，往回收最近一天的錄音

func writeJSON(_ obj: Any, _ path: String) {
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]) else { return }
    let tmp = path + ".tmp"
    if fm.createFile(atPath: tmp, contents: data) {
        _ = try? fm.removeItem(atPath: path)
        try? fm.moveItem(atPath: tmp, toPath: path)
    }
}

func heartbeat(ok: Bool, error: String? = nil, copied: [String] = [], total: Int = 0) {
    var h: [String: Any] = ["at": Date().timeIntervalSince1970, "ok": ok, "copied": copied, "total": total]
    if let e = error { h["error"] = e }
    writeJSON(h, dest + "/heartbeat.json")
}

try? fm.createDirectory(atPath: inbox, withIntermediateDirectories: true)

// ---- 狀態：從什麼時間之後的錄音算「新的」、已經處理過哪些檔案 ----
var since = Date().timeIntervalSince1970 - FIRST_RUN_BACK
var seen = Set<String>()
if let d = fm.contents(atPath: statePath),
   let s = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any] {
    since = (s["since"] as? Double) ?? since
    seen = Set((s["seen"] as? [String]) ?? [])
}

// ---- 讀錄音清單 ----
let files: [String]
do {
    files = try fm.contentsOfDirectory(atPath: src)
} catch {
    let ns = error as NSError
    let denied = ns.code == NSFileReadNoPermissionError
        || (ns.userInfo[NSUnderlyingErrorKey] as? NSError)?.code == Int(EPERM)
    heartbeat(ok: false, error: denied ? "no_permission" : "讀不到語音備忘錄資料夾：\(ns.localizedDescription)")
    exit(denied ? 3 : 1)
}

// ---- 標題、錄音時間、長度：語音備忘錄自己的資料庫（複製一份再讀，避免影響 App） ----
struct Info { var title: String?; var date: Double?; var duration: Double? }
func readDB() -> [String: Info] {
    var out: [String: Info] = [:]
    let tmp = NSTemporaryDirectory() + "voicememo-sync-\(getpid())"
    try? fm.createDirectory(atPath: tmp, withIntermediateDirectories: true)
    defer { try? fm.removeItem(atPath: tmp) }
    for suffix in ["", "-wal", "-shm"] {
        let p = src + "/CloudRecordings.db" + suffix
        if fm.fileExists(atPath: p) { try? fm.copyItem(atPath: p, toPath: tmp + "/r.db" + suffix) }
    }
    var db: OpaquePointer?
    guard sqlite3_open_v2(tmp + "/r.db", &db, SQLITE_OPEN_READWRITE, nil) == SQLITE_OK else { return out }
    defer { sqlite3_close(db) }
    // 欄位名稱各版本不同：先看有哪些欄位
    var cols = Set<String>()
    var st: OpaquePointer?
    if sqlite3_prepare_v2(db, "PRAGMA table_info(ZCLOUDRECORDING)", -1, &st, nil) == SQLITE_OK {
        while sqlite3_step(st) == SQLITE_ROW { cols.insert(String(cString: sqlite3_column_text(st, 1))) }
    }
    sqlite3_finalize(st)
    guard cols.contains("ZPATH") else { return out }
    let titleCol = ["ZENCRYPTEDTITLE", "ZCUSTOMLABEL", "ZCUSTOMLABELFORSORTING"].first { cols.contains($0) }
    let sql = "SELECT ZPATH, \(titleCol ?? "NULL"), \(cols.contains("ZDATE") ? "ZDATE" : "NULL"), "
        + "\(cols.contains("ZDURATION") ? "ZDURATION" : "NULL") FROM ZCLOUDRECORDING"
    if sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK {
        while sqlite3_step(st) == SQLITE_ROW {
            guard let p = sqlite3_column_text(st, 0) else { continue }
            let name = (String(cString: p) as NSString).lastPathComponent
            var info = Info()
            if let t = sqlite3_column_text(st, 1) { info.title = String(cString: t) }
            if sqlite3_column_type(st, 2) != SQLITE_NULL { info.date = sqlite3_column_double(st, 2) + 978307200 }  // Core Data 從 2001 年算
            if sqlite3_column_type(st, 3) != SQLITE_NULL { info.duration = sqlite3_column_double(st, 3) }
            out[name] = info
        }
    }
    sqlite3_finalize(st)
    return out
}

var infos: [String: Info]? = nil
var copied: [String] = []
let now = Date().timeIntervalSince1970
for name in files.sorted() {
    let ext = (name as NSString).pathExtension.lowercased()
    guard EXTS.contains(ext), !seen.contains(name) else { continue }
    let path = src + "/" + name
    guard let attr = try? fm.attributesOfItem(atPath: path),
          let size = attr[.size] as? Int, size > 0,
          let mtime = (attr[.modificationDate] as? Date)?.timeIntervalSince1970 else { continue }
    if now - mtime < SETTLE { continue }                      // 還在寫入或同步中，下一輪再看
    if infos == nil { infos = readDB() }
    let info = infos?[name] ?? Info()
    let created = (attr[.creationDate] as? Date)?.timeIntervalSince1970 ?? mtime
    let recordedAt = info.date ?? created
    if recordedAt < since { seen.insert(name); continue }    // 舊錄音：不打擾
    let target = inbox + "/" + name
    do {
        _ = try? fm.removeItem(atPath: target + ".part")
        try fm.copyItem(atPath: path, toPath: target + ".part")
        _ = try? fm.removeItem(atPath: target)
        try fm.moveItem(atPath: target + ".part", toPath: target)
    } catch {
        continue                                              // 這次複製失敗（例如還沒下載完），下一輪再試
    }
    var meta: [String: Any] = ["file": name, "size": size, "recorded_at": recordedAt, "found_at": now]
    let title = (info.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    meta["title"] = title.isEmpty ? (name as NSString).deletingPathExtension : title
    if let d = info.duration, d > 0 { meta["duration"] = d }
    writeJSON(meta, target + ".json")
    seen.insert(name)
    copied.append(name)
}

writeJSON(["since": since, "seen": Array(seen).sorted()], statePath)
heartbeat(ok: true, copied: copied, total: files.filter { EXTS.contains(($0 as NSString).pathExtension.lowercased()) }.count)
