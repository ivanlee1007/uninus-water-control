# UNiNUS Water Control

Home Assistant 補水控制，採原生 UI 優先。區域與水桶可依現場配置增減，每桶個別指定液位感測器，補水與告警開關各自選配。

## HA Marketplace 安裝

[在 Home Assistant Marketplace 開啟 UNiNUS Water Control](https://my.home-assistant.io/redirect/marketplace_repository/?owner=ivanlee1007&repository=uninus-water-control&category=integration)

也可到 Home Assistant **設定 → Marketplace → 從連結新增**，貼上本 GitHub 儲存庫網址，類別選 **整合（Integration）**，再安裝 UNiNUS Water Control。安裝後，到 **設定 → 裝置與服務 → 新增整合**，搜尋 UNiNUS Water Control。儀表板模組隨整合提供，依[原生儀表板設定](docs/NATIVE-DASHBOARD.md)加入，不必另裝卡片套件。

這是自訂儲存庫安裝方式，並非 Marketplace 預設目錄上架。

## 功能與使用限制

已提供可增減的區域與水桶、原生液位實體、自適應總覽、選配的單桶手動補水與警報，以及不操作設備的門檻與延遲告警預覽。自動補水、正式告警通知與實體設備驗收未完成。請先在測試 HA 使用模擬感測器和輸出。

- HA 設定流程負責區域、水桶、名稱與實體綁定；Dashboard 不負責設備配對。
- Dashboard 優先使用原生 Sections、Tile、Button、Number、History 等元件。只有原生做不到的動態配置或尺寸規則才考慮最薄的自訂層，不重做整套 HA UI。
- 開發與測試不修改 Jingan 的設定或設備，亦不以顯示配置直接控制原始實體。

完整範圍、交付順序與驗收見 [開發路線圖](docs/ROADMAP.md)。
