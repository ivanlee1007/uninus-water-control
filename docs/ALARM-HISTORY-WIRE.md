# Alarm output history wire v1

`uninus_water_control/alarm_history_get` is read-only. The normal Lovelace card offers it under **補水參數 → 告警輸出紀錄**; the native card editor remains the only card configuration surface. The card sends one request on explicit refresh. It does not poll, fetch other tanks, call a device service, edit settings, or configure Recorder.

## Request

```json
{
  "id": 42,
  "type": "uninus_water_control/alarm_history_get",
  "version": 1,
  "entry_id": "01G00000000000000000000000",
  "zone_id": "00000000000000000000000000000001",
  "tank_id": "00000000000000000000000000000002"
}
```

The envelope `id` is assigned by native HA WS. `version` is the integer `1`. `entry_id` is the 26-character uppercase HA entry identifier; `zone_id` and `tank_id` are 32 lowercase hexadecimal characters. Extra fields (including entity IDs, date ranges, and pagination) are rejected. Core requires a current active non-system administrator and an authenticated, current refresh-token principal. The server requires `read` on the selected entry's exact live native tank-level projection and on the current configured alarm registry row, when resolved. The tank must be in the requested zone of that loaded entry. For a commissioned alarm, the exact current owner, protected role and registry row must also agree; a passive bound switch does not require commissioning. A missing row must not resolve by name.

## Success result

Native HA wraps this object in the normal `result` envelope:

```json
{
  "version": 1,
  "scope": "5b5d10c047a3465e996381b94d216ad9",
  "status": "ok",
  "window": {"start_utc": "2026-01-01T00:00:00.000000Z", "end_utc": "2026-01-02T00:00:00.000000Z"},
  "coverage": {"start_utc": "2026-01-01T13:00:00.000000Z", "end_utc": "2026-01-01T15:00:00.000000Z"},
  "events": [{"at_utc": "2026-01-01T15:00:00.000000Z", "state": "on"}],
  "limited": false
}
```

- `window`: server-selected 24 hours, not a promise of 24 hours of retention. Times are fixed-width ISO 8601 in UTC (`Z`, six fractional digits).
- `scope`: opaque 32-character lowercase hexadecimal value, stable across reads of the same observed binding epoch. It may change after a continuity cut, runtime turnover, or Core restart and is never a request parameter or proof of authorization. An unresolved selection uses a separate opaque per-process token.
- `status`: `no_binding` for no configured alarm reference; `unavailable` for no usable proof (including unresolved/name-only bindings, excluded/disabled Recorder, or no confirmed baseline); `no_events` for a verified coverage interval without an `off`/`on` change; `ok` for one or more verified changes.
- `coverage`: `null` for `no_binding` or `unavailable`; otherwise the first and last corroborated sample times in the usable interval. Those can be equal for a single confirmed baseline. It does not extend to request time or imply recording beyond the endpoints.
- `events`: at most 50 rows, newest first, strictly descending by UTC timestamp. Each row contains only `at_utc` and `state` (`on`/`off`), the state after the transition. Initial/baseline states, attributes-only changes, unavailable states, actor/context IDs, command/preview outcomes and inferred causes are never events. An empty array never indicates that an output was off throughout the requested range.
- `limited`: `true` when the in-memory sample limit or the event count cap curtailed the verified history; `false` when there is no usable coverage. The 24-hour window and continuity boundaries still apply regardless.

## Errors and bounds

`invalid_format` (native WS schema), `unauthorized`, `entry_not_loaded`, `unknown_tank`, `history_busy`, `stale_history` (scope/registry/Recorder epoch changed while awaiting). Errors are sanitized and do not return history. Failed, incomplete, or timed-out Recorder reads provide no usable data; a timeout does not free the connection's slot before its DB work settles.

The private witness captures only local native StateMachine `on`/`off` points after the current owner and exact row are established. A witness is necessary, not sufficient: one native Recorder state-change query for one current entity must return the exact bounded witness sequence, including both endpoints of every reported change. No Recorder-start value, history from before this observer, old entity name, or name-only binding can fill a gap. Unavailable/removal/registry changes, Recorder enable/disable/purge, recording loss, or owner retirement break continuity; fresh corroborated samples are required. No new database, Store, action attribution or automation is involved. A Core-wide ceiling limits observation to 128 configured targets and 256 points each. There is at most one request/query in flight per connection and four Core-wide; the native one-entity call asks for no attributes, no synthetic start state, and at most 257 rows. The extra row detects excess input. Time spent awaiting a query is capped at five seconds; an underlying database job can take longer but retains its slot. The principal, projection, loaded owner, registry row and Recorder identity are rechecked after the await before sending anything.
