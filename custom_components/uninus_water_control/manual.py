"""Own the one commissioned tank's fixed native switch operations."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, SERVICE_TURN_OFF, SERVICE_TURN_ON, STATE_OFF, STATE_ON
from homeassistant.core import Context, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_state_change_event, async_track_state_report_event

from .inventory import DOMAIN, inventory
from .sensor import _level_metres

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State

ROLES = ("refill", "alarm")
ACTIONS = ("start", "stop", "reset")
DEFAULT_SECONDS = 60
FEEDBACK_SECONDS = 2
RETRY_SECONDS = 2
CLOSE_SECONDS = 3
RESTART_REQUIRED = "restart_required"
VIRTUAL_OUTPUTS = {DOMAIN, "group", "template", "switch_as_x"}


@dataclass(frozen=True)
class Binding:
    """Exact registry identities authorized by commissioning."""

    output: str
    source: str | None
    seconds: int


def output_row(registry: er.EntityRegistry, ident: str):
    row = registry.async_get(ident)
    if row is None or row.domain != "switch" or row.disabled_by or row.platform in VIRTUAL_OUTPUTS:
        return None
    return row


def output_conflicts(registry: er.EntityRegistry, zones: list, tank_id: str, role: str, row) -> bool:
    """Detect visible shared switches and same-device outputs conservatively."""
    for zone in zones:
        for tank in zone["tanks"]:
            for other_role in ROLES:
                if tank["id"] == tank_id and other_role == role:
                    continue
                if not (ident := tank.get(f"{other_role}_switch")):
                    continue
                other = registry.async_get(tank.get(f"{other_role}_switch_registry_id", ident))
                if ident == row.entity_id or (
                    other is not None
                    and (other.id == row.id or (row.device_id is not None and row.device_id == other.device_id))
                ):
                    return True
    return False


def source_conflicts(zones: list, tank_id: str, row) -> bool:
    return any(
        tank["id"] != tank_id
        and (
            tank.get("level_sensor_registry_id") == row.id
            or ("level_sensor_registry_id" not in tank and tank["level_sensor"] == row.entity_id)
        )
        for zone in zones
        for tank in zone["tanks"]
    )


class ManualController:
    """Own role lifetimes without exposing a raw service or caller-selected target."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.options = entry.options
        self.registry = er.async_get(hass)
        self.zones = inventory(entry)
        self.retiring = False
        self.restart_required = False
        self.shutdown_deadline: float | None = None
        self.tank = None
        self.roles: dict[str, ManualRole] = {}
        manual = entry.options.get("manual") or {}
        self.tank_id = manual.get("tank_id")
        self.labels = {}
        for zone in self.zones:
            for tank in zone["tanks"]:
                if tank["id"] == self.tank_id:
                    self.tank = tank
                    self.labels = {"zone": zone["name"], "tank": tank["name"]}
        if self.tank is not None:
            for role in ROLES:
                if (settings := manual.get(role)) and self._valid_settings(settings, role):
                    binding = Binding(settings["output"], settings.get("source"), settings["max_runtime_seconds"])
                    self.roles[role] = ManualRole(self, role, binding)
        self._unsubs = []
        if self.roles:
            self._unsubs = [
                hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry_changed),
                hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, self._on_shutdown),
            ]
            for role in self.roles.values():
                role.observe()

    def _valid_settings(self, settings, role):
        return (
            isinstance(settings, Mapping)
            and isinstance(settings.get("output"), str)
            and settings.get("output") == self.tank.get(f"{role}_switch_registry_id")
            and type(settings.get("max_runtime_seconds")) is int
            and 1 <= settings["max_runtime_seconds"] <= 300
            and settings.get("cutoff_confirmed") is True
            and (
                role != "refill"
                or settings.get("source") == self.tank.get("level_sensor_registry_id")
                and isinstance(settings.get("source"), str)
            )
        )

    @property
    def off_attempt_allowed(self) -> bool:
        return self.shutdown_deadline is None or self.hass.loop.time() < self.shutdown_deadline

    @callback
    def _registry_changed(self, event):
        for role in self.roles.values():
            role.observe()

    async def _on_shutdown(self, event):
        # Core cancels pre-existing background tasks before this event. OFFs
        # created here have their own bounded, Core-tracked STOP lifetime.
        self.shutdown_deadline = self.hass.loop.time() + CLOSE_SECONDS
        try:
            if await self.async_close():
                self.detach()
        finally:
            for role in self.roles.values():
                role._clear_timer("_retry_timer")

    async def async_close(self) -> bool:
        """Arm every role before waiting; retain unresolved owners across unload refusal."""
        self.retiring = True
        for role in self.roles.values():
            if not role.closed:
                role.stop()
        if self.roles:
            try:
                deadline = self.shutdown_deadline
                if deadline is None:
                    deadline = self.hass.loop.time() + CLOSE_SECONDS
                async with asyncio.timeout_at(deadline):
                    await asyncio.gather(*(role.settled.wait() for role in self.roles.values()))
            except TimeoutError:
                return False
        return True

    @callback
    def require_restart(self):
        """Keep the retained owner visibly retired until a new Core instance."""
        self.restart_required = True
        for role in self.roles.values():
            role._publish()

    @callback
    def detach(self):
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for role in self.roles.values():
            role.detach()


class ManualRole:
    """Separate requested action, pending service I/O and current HA feedback."""

    def __init__(self, owner: ManualController, name: str, binding: Binding):
        self.owner = owner
        self.hass = owner.hass
        self.name = name
        self.binding = binding
        self.requested = False
        self.closing = False
        self.running_seen = False
        self.fault: str | None = None
        self.on_task = None
        self.off_task = None
        self.settled = asyncio.Event()
        self._runtime_timer = self._feedback_timer = self._retry_timer = None
        self._unsub_state = None
        self._unsub_report = None
        self._watched = frozenset()
        self._listeners = set()
        self._context_id = None
        self._off_again = False
        self._off_baseline = None

    def output(self):
        return output_row(self.owner.registry, self.binding.output)

    def source(self):
        if (
            self.binding.source
            and (row := self.owner.registry.async_get(self.binding.source))
            and row.domain == "sensor"
            and row.platform != DOMAIN
            and not row.disabled_by
        ):
            return row
        return None

    def source_healthy(self, frame: State | None = None, *, captured: bool = False) -> bool:
        if self.name != "refill":
            return True
        if not (row := self.source()) or source_conflicts(self.owner.zones, self.owner.tank_id, row):
            return False
        value = _level_metres(frame if captured else self.hass.states.get(row.entity_id))
        return value is not None and value >= 0

    def output_safe(self) -> bool:
        row = self.output()
        return row is not None and not output_conflicts(
            self.owner.registry, self.owner.zones, self.owner.tank_id, self.name, row
        )

    @property
    def actual(self) -> str:
        row = self.output()
        state = self.hass.states.get(row.entity_id) if row is not None else None
        if state is None or state.state not in (STATE_ON, STATE_OFF):
            return "unknown"
        if state.state == STATE_OFF and self.closing:
            if self._off_baseline is None:
                return "unknown"
            target, previous, last_reported = self._off_baseline
            # An unchanged OFF written by the provider is reported separately by
            # Core. Do not accept the same pre-dispatch State/report as new proof.
            if row.entity_id != target or (state is previous and state.last_reported is last_reported):
                return "unknown"
        return state.state

    @property
    def closed(self) -> bool:
        return not (self.requested or self.closing or self.on_task or self.off_task) and self.actual == STATE_OFF

    def snapshot(self) -> dict:
        actual = self.actual
        fault = RESTART_REQUIRED if self.owner.restart_required else self.fault
        if self.closing or self.off_task:
            phase = "stopping"
        elif self.requested:
            phase = "starting" if actual != STATE_ON or self.on_task else "running"
        elif fault:
            phase = "fault"
        else:
            phase = "idle" if actual == STATE_OFF else "unknown"
        return {
            "phase": phase,
            "requested": STATE_ON if self.requested else STATE_OFF,
            "actual": actual,
            "pending_on": self.on_task is not None,
            "pending_off": self.off_task is not None,
            "fault": fault,
            "max_runtime_seconds": self.binding.seconds,
            "can_start": self.can_start(),
        }

    def identity(self, action: str) -> dict:
        return {"version": 1, "tank_id": self.owner.tank_id, "role": self.name, "action": action}

    def can_start(self) -> bool:
        return (
            self.hass.is_running
            and not self.owner.retiring
            and self.owner.entry.options is self.owner.options
            and not self.fault
            and self.closed
            and self.source_healthy()
            and self.output_safe()
        )

    def subscribe(self, listener):
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _publish(self):
        if self.closed:
            self.settled.set()
        else:
            self.settled.clear()
        for listener in tuple(self._listeners):
            listener()

    @callback
    def observe(self):
        output, source = self.output(), self.source()
        watched = frozenset(row.entity_id for row in (output, source) if row is not None)
        if watched != self._watched:
            self._clear_timer("_unsub_state")
            self._clear_timer("_unsub_report")
            self._watched = watched
            self._unsub_state = async_track_state_change_event(self.hass, watched, self._changed) if watched else None
            if output:
                self._unsub_report = async_track_state_report_event(self.hass, output.entity_id, self._output_reported)
        self._evaluate()

    @callback
    def _output_reported(self, event):
        if self.closing and (output := self.output()) and event.data["entity_id"] == output.entity_id:
            self._evaluate()

    @callback
    def _changed(self, event):
        ident = event.data["entity_id"]
        state = event.data["new_state"]
        if self.requested:
            source, output = self.source(), self.output()
            if source and source.entity_id == ident and not self.source_healthy(state, captured=True):
                self.stop("source_invalid")
                return
            if output and output.entity_id == ident and (state is None or state.state not in (STATE_ON, STATE_OFF)):
                self.stop("output_unknown")
                return
        self._evaluate()

    @callback
    def _evaluate(self):
        actual = self.actual
        if self.requested:
            if not self.output_safe():
                self.stop("output_binding_changed")
                return
            if not self.source_healthy():
                self.stop("source_invalid")
                return
            if actual == "unknown" or (self.running_seen and actual != STATE_ON):
                self.stop("output_unknown" if actual == "unknown" else "unexpected_off")
                return
            if actual == STATE_ON:
                self.running_seen = True
                if not self.on_task:
                    self._clear_timer("_feedback_timer")
        elif self.closing:
            if actual == STATE_ON and not self.off_task:
                self._arm_off()
            self._settle_off()
        elif actual != STATE_OFF:
            self.stop("unexpected_on" if actual == STATE_ON else "output_unknown")
            return
        self._publish()

    @callback
    def start(self, context: Context):
        if self.owner.restart_required:
            raise HomeAssistantError("Restart Home Assistant after the failed unload")
        if not self.can_start():
            raise HomeAssistantError("Manual start is not safe or commissioned")
        self._context_id = context.id
        self.requested = True
        self.running_seen = False
        self._runtime_timer = async_call_later(self.hass, self.binding.seconds, self._runtime_expired)
        self._feedback_timer = async_call_later(self.hass, FEEDBACK_SECONDS, self._feedback_expired)
        self._send(STATE_ON)
        self._publish()

    @callback
    def reset(self):
        if self.owner.restart_required:
            raise HomeAssistantError("Restart Home Assistant after the failed unload")
        if self.owner.retiring or not self.closed or not self.output_safe() or not self.source_healthy():
            raise HomeAssistantError("Off feedback, settled commands and healthy bindings are required")
        self.fault = None
        self._publish()

    @callback
    def _runtime_expired(self, _now):
        self._runtime_timer = None
        self.stop("runtime_limit")

    @callback
    def _feedback_expired(self, _now):
        self._feedback_timer = None
        self.stop("on_unconfirmed")

    @callback
    def stop(self, fault: str | None = None, *, context: Context | None = None):
        was_open = not self.closed
        if context is not None:
            self._context_id = context.id
        self.fault = self.fault or fault
        self.requested = False
        self._clear_timer("_runtime_timer")
        self._clear_timer("_feedback_timer")
        if was_open:
            if not self.closing:
                self.closing = True
                self._arm_off()
            elif self.actual != STATE_OFF:
                self._arm_off()
            self._settle_off()
        self._publish()

    @callback
    def _arm_off(self):
        if self.off_task or not self.owner.off_attempt_allowed:
            return
        if self.output() is None:
            self.fault = self.fault or "output_binding_changed"
            return
        self._off_baseline = None
        self._send(STATE_OFF)

    @callback
    def _capture_off_baseline(self, entity_id: str):
        state = self.hass.states.get(entity_id)
        self._off_baseline = (entity_id, state, state.last_reported if state else None)

    @callback
    def _settle_off(self):
        if not (self.on_task or self.off_task or self._off_again) and self.actual == STATE_OFF:
            self.closing = False
            self._off_baseline = None
            self._clear_timer("_retry_timer")
        elif not self._retry_timer and self.owner.off_attempt_allowed:
            self._retry_timer = async_call_later(self.hass, RETRY_SECONDS, self._retry_off)

    @callback
    def _retry_off(self, _now):
        self._retry_timer = None
        if self.closing:
            if self.actual != STATE_OFF or self.off_task:
                self.fault = self.fault or "off_unconfirmed"
            if self.actual != STATE_OFF and not self.off_task:
                self._arm_off()
            self._settle_off()
        self._publish()

    @callback
    def _send(self, desired: str):
        stopping = desired == STATE_OFF and self.owner.shutdown_deadline is not None
        create_task = self.owner.entry.async_create_task if stopping else self.owner.entry.async_create_background_task
        task = create_task(self.hass, self._dispatch(desired), f"water {self.name} {desired}", eager_start=False)
        if desired == STATE_ON:
            self.on_task = task
        else:
            self.off_task = task
        task.add_done_callback(lambda task: self._dispatch_done(desired, task))

    @callback
    def _dispatch_done(self, desired: str, task: asyncio.Task):
        # A coroutine cancelled before its first instruction never runs its
        # finally. Reap only that still-owned slot, not a replacement dispatch.
        owned = self.on_task if desired == STATE_ON else self.off_task
        if owned is task:
            self._completed(desired, False, "command_interrupted")

    async def _dispatch(self, desired: str):
        failure = None
        sent = False
        try:
            if desired == STATE_OFF and not self.owner.off_attempt_allowed:
                return
            row = self.output()
            if row is None:
                failure = "output_binding_changed"
            elif desired == STATE_OFF or (
                self.requested
                and self.hass.is_running
                and not self.owner.retiring
                and self.owner.entry.options is self.owner.options
                and self.output_safe()
                and self.source_healthy()
                and self.actual == STATE_OFF
            ):
                # This is the sole raw service boundary. No caller data is forwarded.
                if desired == STATE_OFF:
                    self._capture_off_baseline(row.entity_id)
                sent = True
                deadline = self.owner.shutdown_deadline if desired == STATE_OFF else None
                async with asyncio.timeout_at(deadline):
                    await self.hass.services.async_call(
                        "switch",
                        SERVICE_TURN_ON if desired == STATE_ON else SERVICE_TURN_OFF,
                        {"entity_id": row.entity_id},
                        blocking=True,
                        context=Context(parent_id=self._context_id),
                    )
            elif self.requested:
                failure = "start_guard_changed"
        except asyncio.CancelledError:
            failure = "command_interrupted"
            raise
        except Exception:  # noqa: BLE001 - Provider errors must not bypass safety OFF.
            failure = "command_failed"
        finally:
            self._completed(desired, sent, failure)

    @callback
    def _completed(self, desired, sent, failure):
        if desired == STATE_ON:
            self.on_task = None
            if not self.requested and self.closing and sent:
                # A provider may apply ON after our first OFF.
                if self.off_task:
                    self._off_again = True
                else:
                    self._arm_off()
            if failure:
                # Even an errored ON may have reached the provider.
                self.closing = True
                self._arm_off()
                self.stop(failure)
            else:
                self._evaluate()
        else:
            self.off_task = None
            self.fault = self.fault or failure
            if self._off_again or (
                failure == "command_interrupted" and self.owner.shutdown_deadline is not None
            ):
                self._off_again = False
                self._arm_off()
            self._settle_off()
            self._publish()

    @callback
    def _clear_timer(self, key):
        if cancel := getattr(self, key):
            cancel()
            setattr(self, key, None)

    @callback
    def detach(self):
        for key in ("_runtime_timer", "_feedback_timer", "_retry_timer", "_unsub_state", "_unsub_report"):
            self._clear_timer(key)
