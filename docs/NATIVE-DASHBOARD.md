# 在 Home Assistant 加入補水總覽

「補水控制」卡片採用提案的繁體中文介面，讀取 UNiNUS Water Control 發布的水位、門檻預覽及輸出回報。新增卡片不會呼叫設備服務，也不會覆寫既有儀表板。

1. 在隔離 HA 測試機載入整合，到 **設定 → 裝置與服務 → UNiNUS Water Control** 新增整合，選第一桶的液位實體。後續用整合的設定選項管理區域和水桶。
2. 用管理員帳號開啟 **設定 → 儀表板 → 資源**（若未顯示資源管理，先在個人設定啟用進階模式）。新增 URL `/uninus_water_control/dashboard.js`，類型選 **JavaScript 模組**。該 URL 由有啟用 HTTP 的整合提供，只需登錄一次；YAML 管理資源的 HA 可依其原有方式加入同一個 URL，勿同時登錄兩次。
3. 在要使用的儀表板開啟原生編輯。新增卡片時可選「補水控制」，或在手動卡片貼上：

   ```yaml
   type: custom:uninus-water-dashboard
   ```

   如需完整可用寬度，可使用面板（Panel）檢視，只放這張卡。卡片依實際可用寬度調整區域與水桶欄數，不固定桶數。
4. 在「操作總覽」點水位數字可開啟 HA 原生 **更多資訊**；點各桶的狀態徽章可展開「詳細狀態」，也可開啟已設定的門檻預覽實體。水位量程需為各桶另行設定，未設定時不畫填水比例。
5. 「補水參數」和「區域與水桶」分別顯示唯讀的參數和設備清單。管理員可由各頁進入 HA 的整合頁，再選相應的「設定」選項；卡片不另做寫入表單。

**控制邊界：** 只有已啟用的手動控制可經原生受控按鈕操作；啟動須確認，未提供控制的按鈕維持停用。低液位與延遲告警為預覽，不代表輸出已開啟。卡片不提供自動補水、告警上限或直接繼電器控制。

## 本機獨立驗證

在隔離的測試環境安裝 `requirements-test.txt` 和 `requirements-browser.txt`，用 `python -m playwright install chromium` 安裝 Chromium；已有受支援的本機 Chromium 可透過 `WATER_CHROMIUM_EXECUTABLE` 指定。執行 `python tools/verify_native_dashboard.py`。工具會啟動臨時、只綁定 `127.0.0.1` 的測試 HA，不需要任何正式站帳號或憑證。可加上 `--screenshots` 及輸出目錄另存檢查圖；再加上 `--reference` 與凍結的提案 HTML 路徑，比對同寬度的桌面、手機畫面與版面尺寸。
