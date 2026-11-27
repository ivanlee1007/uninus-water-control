# UNiNUS Water Control

Home Assistant 補水控制，採原生 UI 優先。區域與水桶可依現場配置增減，每桶個別指定液位感測器，補水與告警開關各自選配。

## 開發狀態

本儲存庫是私有開發專案，尚未發布。第一個增量是 **HA 原生整合設定 → 可放上原生 Tile 的唯讀液位實體**。可自適應的原生 Dashboard、受保護的補水／告警控制與實機驗收分階段交付，不屬於第一個增量的完成聲明。

- HA 設定流程負責區域、水桶、名稱與實體綁定；Dashboard 不負責設備配對。
- Dashboard 優先使用原生 Sections、Tile、Button、Number、History 等元件。只有原生做不到的動態配置或尺寸規則才考慮最薄的自訂層，不重做整套 HA UI。
- 開發與測試不修改 Jingan 的設定或設備，亦不以顯示配置直接控制原始實體。

完整範圍、交付順序與驗收見 [開發路線圖](docs/ROADMAP.md)。
