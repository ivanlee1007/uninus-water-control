# P3｜單桶手動補水與警報

預設只監測。設定原始開關作為清單參照不會給予致動權。先在隔離的 Home Assistant 中使用測試開關，從「設定 → 編輯水箱」分別啟用手動補水或手動警報。一次只能啟用一個水箱；同一水箱的兩項控制可獨立啟用。手動補水需要已登錄、未共用且可回報有效水位的感測器；警報不依賴水位。

每個已啟用用途的 HA 運轉上限可設為 1–300 秒（預設 60 秒），自接受開始操作時計算。達上限時 HA 嘗試停止。這不是裝置計時器，不能涵蓋 HA 當機、網路中斷、錯誤裝置對應或實體卡死。實機使用前須獨立驗證裝置可安全關斷、實體對應正確且無其他控制器會操作這個輸出；表單勾選只表示操作者確認，不會驗證硬體。沒有這些證據請勿啟用；P3 沒有無人補水、門檻控制或遠端實機驗收。測試沒有連線至 Jingan。

## 操作

- 使用整合提供的「開始／停止／清除故障」原生按鈕，不要把卡片連到原始開關。有效的 HA 使用者還需具有對該按鈕的控制權，系統來源沒有手動按鈕權限。
- HA 回報、等待中的服務呼叫、要求的狀態相互獨立。收到服務回應不代表接點位置或實際出水。停止後若尚無新的 OFF 回報，不顯示已關閉。
- 停止不等待開始呼叫完成；開始呼叫遲到完成時，HA 會再次要求 OFF。無法確認停止時，整合只會對原來的已登錄擁有者嘗試 OFF，不會切換到接手舊實體 ID 的開關。
- 有故障時先安全關閉輸出並釐清原因。確認可辨識的輸出回報 OFF、沒有待處理指令且相關來源正常後，才能清除故障。若已遺失原開關登錄身分，不要靠重新使用舊 ID 繞過；先在裝置端安全隔離並修復原綁定。整合不能藉由刪除實體確認裝置 OFF。
- 編輯前停止兩項控制並等待回報。編輯已啟用的水箱時，重新選擇要保留的用途並確認保護；不勾選會撤銷該用途。重新載入或啟動不會回放 ON；運轉與故障清除不是持久化的運轉指令。啟動可針對已啟用且尚未觀察為 OFF 的原身分嘗試安全停止。
- 若重新載入時仍有未確認的指令，HA 可能無法卸載整合。此時會顯示「請重新啟動 Home Assistant 以恢復控制」；停止仍可用，開始與清除故障不會解除封鎖。先確認裝置已安全關閉，再重新啟動 HA。

## Native entity contract (v1)

Discover native entities by `attributes.uninus_water_manual`, not their localized or renameable entity ID. Use `tank_id` to match the existing `uninus_water_dashboard.tank.id`. There are no control entities for uncommissioned roles.

| Field | Values |
| --- | --- |
| `version` | `1` |
| `tank_id` | Immutable inventory tank ID |
| `role` | `refill` or `alarm` |
| `action` | `start`, `stop`, `reset` on `button`; `status` on `sensor` |

Status also carries `phase` (`idle`, `starting`, `running`, `stopping`, `fault`, `unknown`), `requested` (`on`, `off`), `actual` (`on`, `off`, `unknown`; HA feedback only), `pending_on`, `pending_off`, `fault` (nullable stable code), `max_runtime_seconds`, and `can_start`. Buttons accept only native `button.press` with an entity ID. No custom service accepts a switch or arbitrary operation. A consumer should not use a press timestamp as feedback, allow start without `can_start`, or disable stop merely because ON is pending.
