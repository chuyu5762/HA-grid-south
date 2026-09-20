# -*- coding: utf-8 -*-
"""The China Southern Power Grid Statistics integration."""
from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import entity_registry
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_AUTH_TOKEN,
    CONF_ELE_ACCOUNTS,
    CONF_LOGIN_TYPE,
    CONF_UPDATED_AT,
    DOMAIN,
)
from .csg_client import (
    CSGAPIError,
    CSGClient,
    CSGElectricityAccount,
    InvalidCredentials,
    NotLoggedIn,
)
from .sensor import CSGCostSensor, CSGEnergySensor

PLATFORMS: list[Platform] = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)

# bounds so install/uninstall never hang on slow/unreachable CSG API
# (HA box often has flaky access to 95598.csg.cn, same as to github.com)
SETUP_VERIFY_TIMEOUT = 8  # seconds
LOGOUT_TIMEOUT = 8  # seconds


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up China Southern Power Grid Statistics from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Validate session, but never let a slow/unreachable CSG API block setup.
    # Only an explicit "login expired" (NotLoggedIn) should trigger reauth;
    # a network timeout/error is allowed to proceed, the coordinator will
    # surface session state in the background on first refresh.
    client = CSGClient.load(
        {
            CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
        }
    )
    try:
        logged_in = await asyncio.wait_for(
            hass.async_add_executor_job(client.verify_login),
            timeout=SETUP_VERIFY_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _LOGGER.warning(
            "Account %s: session verification timed out, continuing setup "
            "(data refresh will retry in background)",
            entry.data[CONF_USERNAME],
        )
        logged_in = None
    except Exception as err:  # noqa: BLE001 - network errors must not block setup
        _LOGGER.warning(
            "Account %s: session verification failed (%s), continuing setup",
            entry.data[CONF_USERNAME],
            err,
        )
        logged_in = None
    if logged_in is False:
        raise ConfigEntryAuthFailed("Login expired")

    hass.data[DOMAIN][entry.entry_id] = {}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.debug(f"Unloading entry: {entry.title}")
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    _LOGGER.debug(f"Unload platforms for entry: {entry.title}, success: {unload_ok}")
    hass.data[DOMAIN].pop(entry.entry_id)
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: DeviceEntry
) -> bool:
    """Remove device"""
    _LOGGER.info(f"removing device {device_entry.name}")
    account_num = list(device_entry.identifiers)[0][1]

    # remove entities
    entity_reg = entity_registry.async_get(hass)
    entities = {
        ent.unique_id: ent.entity_id
        for ent in entity_registry.async_entries_for_config_entry(
            entity_reg, config_entry.entry_id
        )
        if account_num in ent.unique_id
    }
    for entity_id in entities.values():
        entity_reg.async_remove(entity_id)

    # update config entry
    new_data = config_entry.data.copy()
    new_data[CONF_ELE_ACCOUNTS].pop(account_num)
    new_data[CONF_UPDATED_AT] = str(int(time.time() * 1000))
    hass.config_entries.async_update_entry(
        config_entry,
        data=new_data,
    )
    _LOGGER.info(
        "Removed ele account from %s: %s",
        config_entry.data[CONF_USERNAME],
        account_num,
    )
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry.

    Logs out from CSG in the background with a hard timeout so removal never
    blocks on slow/unreachable 95598.csg.cn. The entry is removed regardless
    of whether the remote logout succeeds.
    """
    _LOGGER.info("Removing entry: account %s", entry.data[CONF_USERNAME])

    def client_logout():
        try:
            client = CSGClient.load(
                {
                    CONF_AUTH_TOKEN: entry.data[CONF_AUTH_TOKEN],
                }
            )
            if client.verify_login():
                client.logout(entry.data[CONF_LOGIN_TYPE])
                _LOGGER.info(
                    "CSG account %s logged out", entry.data[CONF_USERNAME]
                )
        except Exception as err:  # noqa: BLE001 - logout is best-effort
            _LOGGER.debug(
                "Logout for account %s failed (ignored): %s",
                entry.data[CONF_USERNAME],
                err,
            )

    async def _best_effort_logout():
        try:
            await asyncio.wait_for(
                hass.async_add_executor_job(client_logout),
                timeout=LOGOUT_TIMEOUT,
            )
        except (asyncio.TimeoutError, Exception) as err:  # noqa: BLE001
            _LOGGER.debug(
                "Best-effort logout finished or timed out (ignored): %s", err
            )

    # run in background so removal returns instantly; not awaited
    hass.async_create_task(_best_effort_logout())
