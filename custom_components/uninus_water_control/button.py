"""Core-authorized buttons for the commissioned role owners."""

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError, Unauthorized

from .manual import ACTIONS
from .settings import SettingsError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(ManualButton(role, action) for role in entry.runtime_data.roles.values() for action in ACTIONS)


class ManualButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, role, action):
        self._role = role
        self._action = action
        self._attr_unique_id = f"{role.owner.tank_id}_{role.name}_{action}"
        self._attr_translation_key = f"{role.name}_{action}"
        self._attr_translation_placeholders = role.owner.labels
        self._attr_extra_state_attributes = {"uninus_water_manual": role.identity(action)}

    async def _async_authorize(self, context):
        if context is None or not context.user_id:
            raise Unauthorized(context=context)
        user = await self.hass.auth.async_get_user(context.user_id)
        if (
            user is None
            or not user.is_active
            or user.system_generated
            or (not user.is_admin and not user.permissions.check_entity(self.entity_id, POLICY_CONTROL))
        ):
            raise Unauthorized(context=context)

    async def async_press(self):
        """Manual controls require an authenticated principal, not a system call."""
        context = self._context
        await self._async_authorize(context)
        if self._action == "start":
            settings = self._role.owner.settings
            if settings.topology is None:
                self._role.start(context)
                return
            try:
                await settings.async_start_manual(self._role, context, self._async_authorize)
            except SettingsError:
                raise HomeAssistantError("Owned water topology unavailable; use the independent cutoff and recovery instructions",
                                         translation_domain="uninus_water_control",
                                         translation_key="topology_recovery_required") from None
        elif self._action == "stop":
            self._role.stop(context=context)
        else:
            self._role.reset()
