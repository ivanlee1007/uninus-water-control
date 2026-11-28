"""Native configuration for a single water controller."""

from __future__ import annotations

import asyncio
from copy import deepcopy

import voluptuous as vol
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.core import HomeAssistant, callback, valid_entity_id
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import current_request
from homeassistant.helpers.selector import BooleanSelector, EntitySelector, NumberSelector, SelectSelector, TextSelector

from . import async_prune_disabled_levels
from .inventory import DOMAIN, TITLE, Zone, inventory, level_source, new_tank, new_zone
from .manual import DEFAULT_SECONDS, ROLES, output_conflicts, output_row, source_conflicts
from .settings import SettingsError
from .topology_lifecycle import MARK, VERSION as TOPOLOGY_VERSION, enrollment_fence, request_enrollment, valid_witness, witness

LEVEL_SENSOR = "level_sensor"
ENTITY_FIELDS = {LEVEL_SENSOR: "sensor", "refill_switch": "switch", "alarm_switch": "switch"}


def _text_field(field: str, values: dict) -> dict:
    return {vol.Required(field, description={"suggested_value": values.get(field)}): TextSelector()}


def _tank_fields(values: dict) -> dict:
    fields = _text_field("tank_name", values)
    for field, domain in ENTITY_FIELDS.items():
        marker = vol.Required if field == LEVEL_SENSOR else vol.Optional
        fields[marker(field, description={"suggested_value": values.get(field)})] = EntitySelector(
            {"filter": [{"domain": domain}]}
        )
    return fields


def _field_errors(hass: HomeAssistant, values: dict) -> dict[str, str]:
    errors = {
        field: "blank_name" for field in ("zone_name", "tank_name") if field in values and not values[field].strip()
    }
    # The EntitySelector's filter is UI metadata and its validator accepts UUIDs.
    for field, domain in ENTITY_FIELDS.items():
        if field not in values:
            continue
        value = values[field]
        if not valid_entity_id(value) or value.partition(".")[0] != domain:
            errors[field] = f"invalid_{domain}"
        elif field == LEVEL_SENSOR:
            entity = er.async_get(hass).async_get(value)
            if entity is not None and entity.platform == DOMAIN:
                errors[field] = "own_level_sensor"
    return errors


class _LevelSuggestions:
    """Keep the registry identity behind every level selector default in a flow."""

    def __init__(self) -> None:
        self._shown: dict[str, str | None] = {}

    @staticmethod
    def _row_id(registry: er.EntityRegistry, source: str) -> str | None:
        row = registry.async_get(source)
        return row.id if row is not None else None

    def form_input(
        self, hass: HomeAssistant, user_input: dict | None, defaults: dict | None = None
    ) -> tuple[dict, dict[str, str]]:
        registry = er.async_get(hass)
        values = dict(user_input if user_input is not None else defaults or {})
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _field_errors(hass, user_input)
            selected = user_input[LEVEL_SENSOR]
            if selected in self._shown and self._shown[selected] != self._row_id(registry, selected):
                # An unchanged native default and a deliberate choice of the same
                # string are indistinguishable. Retain the first identity on replay.
                errors[LEVEL_SENSOR] = "level_source_changed"
                values[LEVEL_SENSOR] = (defaults or {}).get(LEVEL_SENSOR)
        return values, errors

    def capture(self, registry: er.EntityRegistry, values: dict) -> None:
        """Record what the form will show, after all submission checks."""
        if suggested := values.get(LEVEL_SENSOR):
            row_id = self._row_id(registry, suggested)
            if self._shown.setdefault(suggested, row_id) != row_id:
                values[LEVEL_SENSOR] = None


class WaterControlConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the controller with its first zone and tank."""

    VERSION = TOPOLOGY_VERSION

    def __init__(self) -> None:
        self._level_suggestions = _LevelSuggestions()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Set up the initial inventory in one native form."""
        await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
        self._abort_if_unique_id_configured()
        values, errors = self._level_suggestions.form_input(self.hass, user_input)
        if user_input is not None and not errors:
            return self.async_create_entry(title=TITLE, data={"zones": [new_zone(er.async_get(self.hass), user_input)]})
        self._level_suggestions.capture(er.async_get(self.hass), values)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({**_text_field("zone_name", values), **_tank_fields(values)}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> WaterControlOptionsFlow:
        """Use Core's native options manager for the inventory."""
        return WaterControlOptionsFlow()


class WaterControlOptionsFlow(OptionsFlowWithReload):
    """Apply one complete inventory change per native options transaction."""

    def __init__(self) -> None:
        self._entry: ConfigEntry | None = None
        self._zones: list[Zone]
        self._operation: str
        self._zone_index: int
        self._tank_index: int | None
        self._target_label: str
        self._level_suggestions = _LevelSuggestions()
        self._manual_delta: dict = {}
        self._shown_outputs: dict[str, str | None] = {}

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Take a draft snapshot and show available operations."""
        if self._entry is None:
            self._entry = entry = self.config_entry
            if MARK in entry.data:
                self._actor = self._native_actor()
                if (reason := self._enrollment_block(entry)) is not None:
                    return self.async_abort(reason=reason)
            self._require_writer(entry)
            self._base_options = entry.options
            self._base_data = entry.data
            self._base_owner = getattr(entry, "runtime_data", None)
            settings = getattr(self._base_owner, "settings", None)
            self._owned = MARK in entry.data
            self._base_manual = settings.manual if self._owned else self._base_options.get("manual") or {}
            self._base_settings_generation = settings.generation if settings else None
            self._zones = deepcopy(inventory(entry))
        self._require_fresh()
        operations = ["edit_tank", "card_settings"] if self._owned else [
            "add_zone", "edit_zone", "remove_zone", "add_tank", "edit_tank", "remove_tank", "card_settings",
        ]
        if not self._owned and self._entry.state is ConfigEntryState.LOADED:
            operations.append("enroll_topology")
        operations.append("topology_status")
        return self.async_show_menu(
            step_id="init",
            menu_options=operations,
        )

    def _enrollment_block(self, entry: ConfigEntry) -> str | None:
        mark = witness(entry)
        if not valid_witness(mark) or entry.error_reason_translation_key == "topology_recovery_required":
            return "topology_recovery_required"
        if reason := enrollment_fence(self.hass, entry):
            return reason
        if mark["phase"] == "requested":
            return "topology_restart_required"
        if entry.state is not ConfigEntryState.LOADED or getattr(entry, "runtime_data", None) is None:
            return "entry_not_loaded"
        if entry.runtime_data.settings.topology_busy:
            return "topology_writer_unavailable"
        return None

    async def async_step_topology_status(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Show only lifecycle status, not settings or actuator state."""
        entry = self._require_fresh()
        step = "topology_not_requested"
        if self._owned:
            try:
                verified = await entry.runtime_data.settings.async_confirm_owned()
            except SettingsError as err:
                return self.async_abort(reason="entry_not_loaded" if str(err) == "entry_not_loaded"
                                        else "topology_writer_unavailable")
            # A failed disk confirmation has already withdrawn writer access.
            # Recheck the flow's owner and actor before reporting that verdict.
            self._require_fresh(require_writer=verified)
            if not verified:
                return self.async_abort(reason="topology_recovery_required")
            step = "topology_owned_active"
        return self.async_show_form(step_id=step, data_schema=vol.Schema({}), last_step=False)

    async def async_step_topology_not_requested(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self.async_step_init()

    async def async_step_topology_owned_active(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self.async_step_init()

    async def async_step_enroll_topology(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Arm an explicit, cutoff-confirmed handoff without writing topology."""
        entry = self._require_fresh()
        if self._owned:
            return self.async_abort(reason="topology_writer_unavailable")
        runtime = getattr(entry, "runtime_data", None)
        if entry.state is not ConfigEntryState.LOADED or runtime is None:
            return self.async_abort(reason="entry_not_loaded")
        if user_input and user_input.get("confirm_device_cutoff") is True:
            if runtime.settings.lock.locked():
                return self.async_abort(reason="configuration_changed")
            self._require_settled(entry)
            request_enrollment(self.hass, entry)
            # The already-loaded exact owner can still Stop, but never Start.
            for role in runtime.roles.values():
                role._publish()
            return self.async_create_entry(data=None)
        return self.async_show_form(step_id="enroll_topology", data_schema=vol.Schema({
            vol.Required("confirm_device_cutoff", default=False): BooleanSelector(),
        }), errors={"confirm_device_cutoff": "cutoff_required"} if user_input is not None else {})

    async def async_step_add_zone(self, user_input: dict | None = None) -> ConfigFlowResult:
        self._operation = "add_zone"
        return self._zone_form("add_zone", user_input)

    async def async_step_edit_zone(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._choose("edit_zone")

    async def async_step_remove_zone(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._choose("remove_zone")

    async def async_step_add_tank(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._choose("add_tank")

    async def async_step_edit_tank(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._choose("edit_tank")

    async def async_step_remove_tank(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._choose("remove_tank")

    async def async_step_card_settings(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Direct users to the single owner without presenting a second form."""
        return self.async_abort(reason="card_settings_in_dashboard_editor")

    def _require_fresh(self, *, require_writer: bool = True) -> ConfigEntry:
        entry = self.hass.config_entries.async_get_entry(self.handler)
        if require_writer and entry is not None:
            self._require_writer(entry)
        if (
            entry is not self._entry
            or entry is None
            or (entry.options is not self._base_options or entry.data is not self._base_data)
            or (self._owned and getattr(entry, "runtime_data", None) is not self._base_owner)
        ):
            raise AbortFlow("configuration_changed")
        if self._owned and self._native_actor() is not self._actor:
            raise AbortFlow("topology_admin_required")
        settings = getattr(getattr(entry, "runtime_data", None), "settings", None)
        if (settings.generation if settings else None) != self._base_settings_generation:
            raise AbortFlow("configuration_changed")
        return entry

    @staticmethod
    def _require_writer(entry: ConfigEntry) -> None:
        owner = getattr(entry, "runtime_data", None)
        if MARK in entry.data:
            if owner is None or owner.settings.topology is None:
                raise AbortFlow("topology_writer_unavailable")
            try:
                owner.settings.require_live()
            except SettingsError as err:
                raise AbortFlow("topology_writer_unavailable") from err
            return
        # Preserve intentional edits to a user-disabled, never-enrolled entry;
        # a transient unload or pending/owned handoff is not that workflow.
        if entry.state is ConfigEntryState.NOT_LOADED and entry.disabled_by:
            return
        if entry.state is not ConfigEntryState.LOADED or owner is None or owner.retiring:
            raise AbortFlow("entry_not_loaded")

    @staticmethod
    def _native_actor():
        request = current_request.get()
        if request is None:
            return None  # Trusted in-process native manager, not an HTTP request.
        actor = request.get(KEY_HASS_USER)
        if actor is None or not actor.is_active or not actor.is_admin or actor.system_generated:
            raise AbortFlow("topology_admin_required")
        return actor

    @staticmethod
    def _require_settled(entry: ConfigEntry) -> None:
        runtime = getattr(entry, "runtime_data", None)
        if runtime and (runtime.retiring or any(not role.closed for role in runtime.roles.values())):
            raise AbortFlow("manual_outputs_unsettled")

    def _commit(self) -> ConfigFlowResult:
        # Core's OptionsFlowManager updates the options synchronously on this
        # return path; do not put an await between this check and that update.
        entry = self._require_fresh()
        if self._owned:
            raise AbortFlow("topology_writer_unavailable")
        settings = getattr(getattr(entry, "runtime_data", None), "settings", None)
        if settings and settings.lock.locked():
            raise AbortFlow("configuration_changed")
        self._require_settled(entry)
        if (manual := self._base_options.get("manual")) and not any(
            tank["id"] == manual["tank_id"] for zone in self._zones for tank in zone["tanks"]
        ):
            self._manual_delta = {"manual": {}}
        if entry.disabled_by and self._operation in ("remove_zone", "remove_tank"):
            # Disabled entries do not set up on reload. Read only the committed
            # native options on the next loop turn, never this private draft.
            self.hass.loop.call_soon(async_prune_disabled_levels, self.hass, entry)
        return self.async_create_entry(data={**self._base_options, "zones": self._zones} | self._manual_delta)

    async def _commit_owned_tank(self) -> ConfigFlowResult:
        entry = self._require_fresh()
        if self._operation != "edit_tank":
            raise AbortFlow("topology_writer_unavailable")
        self._require_settled(entry)
        settings = entry.runtime_data.settings
        source = settings.source_snapshot(self._zones)
        manual = self._manual_delta.get("manual", self._base_manual)

        async def authorize():
            actor = self._native_actor()
            if actor is not None:
                current = await self.hass.auth.async_get_user(actor.id)
                if current is not actor or self._native_actor() is not self._actor:
                    raise AbortFlow("topology_admin_required")

        def admitted():
            self._require_fresh()
            self._require_settled(entry)
            if settings.source_snapshot(self._zones) != source:
                raise AbortFlow("configuration_changed")

        try:
            await asyncio.shield(self.hass.async_create_task(settings.async_edit_topology(
                self._zones, manual, self._base_settings_generation, authorize, admitted), "water protected tank edit"))
        except SettingsError as err:
            reason = str(err)
            raise AbortFlow(reason if reason in ("configuration_changed", "entry_not_loaded", "readback_required")
                            else "topology_writer_unavailable") from err
        return self.async_create_entry(data=None)

    def _choose(self, operation: str) -> ConfigFlowResult:
        self._operation = operation
        return self._target_form(None)

    async def async_step_select_target(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._target_form(user_input)

    def _target_form(self, user_input: dict | None) -> ConfigFlowResult:
        self._require_fresh()
        tank_target = self._operation in ("edit_tank", "remove_tank")
        field = "tank_id" if tank_target else "zone_id"
        targets = {}
        registry = er.async_get(self.hass)
        for zi, zone in enumerate(self._zones):
            if tank_target:
                for ti, tank in enumerate(zone["tanks"]):
                    label = f"{zi + 1}.{ti + 1} · {zone['name']} / {tank['name']}"
                    if source := level_source(registry, tank):
                        label += f" · {source}"
                    targets[tank["id"]] = (zi, ti, label)
            else:
                targets[zone["id"]] = (zi, None, f"{zi + 1} · {zone['name']}")
        if user_input is None:
            options = [{"value": ident, "label": target[2]} for ident, target in targets.items()]
            return self.async_show_form(
                step_id="select_target",
                data_schema=vol.Schema({vol.Required(field): SelectSelector({"options": options, "mode": "dropdown"})}),
            )

        target = targets.get(user_input[field])
        if target is None:
            return self.async_abort(reason="invalid_target")
        self._zone_index, self._tank_index, self._target_label = target
        if self._operation == "edit_zone":
            return self._zone_form("rename_zone", None)
        if self._operation in ("add_tank", "edit_tank"):
            result = self._tank_form(None)
            assert result is not None
            return result
        if self._operation == "remove_zone" and len(self._zones) == 1:
            return self.async_abort(reason="minimum_zones")
        if self._operation == "remove_tank" and len(self._zone["tanks"]) == 1:
            return self.async_abort(reason="minimum_tanks")
        return self._confirm_remove(None)

    @property
    def _zone(self) -> Zone:
        return self._zones[self._zone_index]

    async def async_step_rename_zone(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._zone_form("rename_zone", user_input)

    def _zone_form(self, step_id: str, user_input: dict | None) -> ConfigFlowResult:
        self._require_fresh()
        add = step_id == "add_zone"
        if add:
            values, errors = self._level_suggestions.form_input(self.hass, user_input)
        elif user_input is not None:
            values, errors = user_input, _field_errors(self.hass, user_input)
        else:
            values, errors = {"zone_name": self._zone["name"]}, {}
        if user_input is not None and not errors:
            if add:
                self._zones.append(new_zone(er.async_get(self.hass), user_input))
            else:
                self._zone["name"] = user_input["zone_name"].strip()
            return self._commit()
        fields = _text_field("zone_name", values)
        if add:
            self._level_suggestions.capture(er.async_get(self.hass), values)
            fields.update(_tank_fields(values))
        return self.async_show_form(step_id=step_id, data_schema=vol.Schema(fields), errors=errors)

    async def async_step_save_tank(self, user_input: dict | None = None) -> ConfigFlowResult:
        result = self._tank_form(user_input)
        if result is not None:
            return result
        return await self._commit_owned_tank() if self._owned else self._commit()

    def _tank_form(self, user_input: dict | None) -> ConfigFlowResult | None:
        self._require_fresh()
        index = self._tank_index
        tank = self._zone["tanks"][index] if index is not None else None
        registry = er.async_get(self.hass)
        defaults = (
            dict(tank, tank_name=tank["name"], level_sensor=level_source(registry, tank)) if tank is not None else None
        )
        if defaults is not None:
            for role in ROLES:
                field = f"{role}_switch"
                if row_id := tank.get(f"{field}_registry_id"):
                    row = registry.async_get(row_id)
                    defaults[field] = row.entity_id if row else None
        values, errors = self._level_suggestions.form_input(self.hass, user_input, defaults)
        manual = self._base_manual
        if tank is not None and user_input is not None and not errors:
            self._manual_delta, errors = self._commission(registry, tank, user_input, manual)
        if user_input is not None and not errors:
            if index is None:
                self._zone["tanks"].append(new_tank(registry, user_input))
            else:
                edited = new_tank(registry, user_input, tank["id"])
                if "thresholds" in tank:
                    edited["thresholds"] = tank["thresholds"]
                self._zone["tanks"][index] = edited
            return None
        # Any rejected submission, including cutoff confirmation, can echo a
        # newly selected entity. Capture identities only once rendering is known.
        self._level_suggestions.capture(registry, values)
        if tank is not None:
            for role in ROLES:
                field = f"{role}_switch"
                if ident := values.get(field):
                    row = registry.async_get(ident)
                    actual = row.id if row else None
                    if self._shown_outputs.setdefault(ident, actual) != actual:
                        values[field] = None
        fields = _tank_fields(values)
        if tank is not None:
            for role in ROLES:
                field = f"commission_{role}"
                fields[vol.Optional(field, description={"suggested_value": values.get(field, False)})] = (
                    BooleanSelector()
                )
                seconds = f"{role}_max_runtime_seconds"
                saved = (manual.get(role) or {}).get("max_runtime_seconds", DEFAULT_SECONDS)
                fields[vol.Optional(seconds, description={"suggested_value": values.get(seconds, saved)})] = (
                    NumberSelector({"min": 1, "max": 300, "step": 1, "mode": "box", "unit_of_measurement": "s"})
                )
            fields[vol.Optional("confirm_device_cutoff")] = BooleanSelector()
        return self.async_show_form(
            step_id="save_tank",
            description_placeholders={"target": self._target_label},
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    def _commission(self, registry, tank, values, manual):
        tank_id = tank["id"]
        requested = [role for role in ROLES if values.get(f"commission_{role}") is True]
        if not requested:
            return ({"manual": {}} if manual.get("tank_id") == tank_id else {}), {}
        if manual and manual.get("tank_id") != tank_id:
            return {}, {f"commission_{role}": "one_manual_tank" for role in requested}
        if values.get("confirm_device_cutoff") is not True:
            return {}, {"confirm_device_cutoff": "cutoff_required"}
        proposed = new_tank(registry, values, tank_id)
        zones = [
            dict(zone, tanks=[proposed if t["id"] == tank_id else t for t in zone["tanks"]]) for zone in self._zones
        ]
        settings = {"tank_id": tank_id}
        for role in requested:
            field = f"{role}_switch"
            entity_id = values.get(field, "")
            output = output_row(registry, entity_id)
            if output is None:
                return {}, {field: "registered_output_required"}
            if self._shown_outputs.get(entity_id, output.id) != output.id:
                return {}, {field: "output_source_changed"}
            if output_conflicts(registry, zones, tank_id, role, output):
                return {}, {field: "shared_output"}
            source_id = None
            if role == "refill":
                source = registry.async_get(values[LEVEL_SENSOR])
                if source is None or source.disabled_by:
                    return {}, {LEVEL_SENSOR: "registered_source_required"}
                if source_conflicts(zones, tank_id, source):
                    return {}, {LEVEL_SENSOR: "shared_source"}
                source_id = source.id
            seconds_field = f"{role}_max_runtime_seconds"
            seconds = values.get(seconds_field, DEFAULT_SECONDS)
            if not float(seconds).is_integer():
                return {}, {seconds_field: "whole_seconds_required"}
            settings[role] = {
                "output": output.id,
                "source": source_id,
                "cutoff_confirmed": True,
                "max_runtime_seconds": int(seconds),
            }
        return {"manual": settings}, {}

    async def async_step_confirm_remove(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self._confirm_remove(user_input)

    def _confirm_remove(self, user_input: dict | None) -> ConfigFlowResult:
        self._require_fresh()
        if user_input is not None and user_input["confirm"] is True:
            if self._tank_index is None:
                del self._zones[self._zone_index]
            else:
                del self._zone["tanks"][self._tank_index]
            return self._commit()
        return self.async_show_form(
            step_id="confirm_remove",
            description_placeholders={"target": self._target_label},
            data_schema=vol.Schema({vol.Required("confirm", default=False): BooleanSelector()}),
            errors={"confirm": "confirm_required"} if user_input is not None else {},
        )
