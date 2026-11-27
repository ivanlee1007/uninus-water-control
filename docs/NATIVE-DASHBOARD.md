# 在 Home Assistant 加入補水總覽

這份指南適用於 P2 開發版驗證。它只讀取由 UNiNUS Water Control 整合發布的液位實體，不會啟動或停止補水，也不會覆寫既有 Dashboard。

1. 在隔離 HA 測試機載入整合，到 **設定 → 裝置與服務 → UNiNUS Water Control** 新增整合，選第一桶的液位實體。後續用整合的設定選項管理區域和水桶。
2. 用管理員帳號開啟 **設定 → 儀表板 → 資源**（若未顯示資源管理，先在個人設定啟用進階模式）。新增 URL `/uninus_water_control/dashboard.js`，類型選 **JavaScript 模組**。該 URL 由有啟用 HTTP 的整合提供，只需登錄一次；YAML 管理資源的 HA 可依其原有方式加入同一個 URL，勿同時登錄兩次。
3. 在要使用的 Dashboard 開啟原生編輯。新增卡片時可選 UNiNUS 補水總覽，或在手動卡片貼上：

   ```yaml
   type: custom:uninus-water-dashboard
   ```

   如果希望單頁取得完整可用寬度，可為該頁選面板（Panel）並只放這張卡。原生 Sections 每張卡可取得的寬度仍受 Dashboard 配置限制。外部配置變窄時，區域與原生 Tile 會改為更少欄。
4. 點一桶的原生 Tile 可開啟 HA 的 **更多資訊**。水位及其狀態由 HA 原生 Tile 顯示；沒有存放在 Dashboard 的固定實體清單。

**開發邊界：** 本版本尚未提供自動補水、告警、原始繼電器開關或實機測試。在 Dashboard 上新增這張卡不會呼叫任何實體服務。

## 本機獨立驗證

在隔離的測試環境安裝 `requirements-test.txt` 和 `requirements-browser.txt`，用 `python -m playwright install chromium` 安裝 Chromium；已有受支援的本機 Chromium 可透過 `WATER_CHROMIUM_EXECUTABLE` 指定。執行 `python tools/verify_native_dashboard.py`。工具會啟動臨時、只綁定 `127.0.0.1` 的測試 HA，不需要任何正式站帳號或憑證。選擇性加上 `--screenshots` 加絕對目錄以另存檢查圖。
