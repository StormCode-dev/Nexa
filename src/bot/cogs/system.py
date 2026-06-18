# cogs/system.py
# Under the MIT License.
#
# System-level tasks and commands: rolling update checks, playit.gg watchdog.
# Restricted to operators as defined in the config.

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import TYPE_CHECKING

import discord
import psutil
import requests
from discord import Interaction, app_commands
from discord.ext import commands, tasks

from services import nexaLoggerFactory

if TYPE_CHECKING:
    from ..discordBot import NexaBot

logger = nexaLoggerFactory.get_logger("SystemCog")

# ---------------------------------------------------------------------------
# Update checker constants
# ---------------------------------------------------------------------------

CURRENT_NEXA_VERSION = "0.2.2"
UPDATE_INDEX_URL = "https://raw.githubusercontent.com/StormCode-dev/Nexa/refs/heads/main/updateIndex.json"


# ---------------------------------------------------------------------------
# Update checker function
# ---------------------------------------------------------------------------

def check_for_updates() -> int:
    """
    Checks the update index for the latest Nexa version.
    Returns:
         1 — update available
         0 — up to date
        -1 — check failed
    """
    try:
        response = requests.get(UPDATE_INDEX_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        latest = data["latestNexaVersion"]

        if CURRENT_NEXA_VERSION != latest:
            logger.warning(f"Update available: {CURRENT_NEXA_VERSION} → {latest}")
            return 1
        else:
            logger.info(f"Nexa is up to date ({CURRENT_NEXA_VERSION}).")
            return 0

    except requests.exceptions.RequestException as e:
        logger.warning(f"Update check failed: {e}")
        return -1
    except (KeyError, ValueError) as e:
        logger.warning(f"Malformed update index: {e}")
        return -1


# ---------------------------------------------------------------------------
# playit.gg helpers
# ---------------------------------------------------------------------------

def _is_playit_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            if "playit" in proc.info["name"].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def _start_playit():
    subprocess.Popen(["playit"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# SystemCog
# ---------------------------------------------------------------------------

class SystemCog(commands.Cog):
    """System-level tasks and operator commands."""

    def __init__(self, bot: NexaBot):
        self.bot = bot
        self._last_update_status: int | None = None
        self._update_checker.start()

        if self.bot.config.get("serverHealthManagement.keepPlayItAlive", False):
            self._playit_watchdog.start()

    def cog_unload(self):
        self._update_checker.cancel()
        self._playit_watchdog.cancel()

    # ---------------------------------------------------------------------------
    # Rolling update checker
    # ---------------------------------------------------------------------------

    @tasks.loop(minutes=1)  # Interval overridden in before_loop from config
    async def _update_checker(self):
        status = await asyncio.to_thread(check_for_updates)

        # Only act on state transitions to avoid log noise
        if status == self._last_update_status:
            return
        self._last_update_status = status

        self.bot.nexaUpdateStatus = status

        presenceName = f"Nexa v{CURRENT_NEXA_VERSION} (Update Available!)" if status == 1 else f"Nexa v{CURRENT_NEXA_VERSION}"
        try:
            await self.bot.change_presence(
                status=discord.Status.online,
                activity=discord.Activity(type=discord.ActivityType.playing, name=presenceName)
            )
        except Exception as e:
            logger.warning(f"Failed to update presence after update check: {e}")

        if status == 1:
            operator_mentions = ""
            if self.bot.config.get("security.enableServerOperators", False):
                operator_ids = self.bot.config.get("security.serverOperators", []) or []
                operator_mentions = " ".join(f"<@{uid}>" for uid in operator_ids) + " " if operator_ids else ""
            await self._notify_health_channel(f"{operator_mentions} A new version of Nexa is available! Check the GitHub repository for the latest release.")

    @_update_checker.before_loop
    async def _before_update_checker(self):
        await self.bot.wait_until_ready()
        interval = int(self.bot.config.get("serverHealthManagement.updateCheckIntervalInMins", 360))
        self._update_checker.change_interval(minutes=interval)

    # ---------------------------------------------------------------------------
    # playit.gg watchdog
    # ---------------------------------------------------------------------------

    @tasks.loop(seconds=30)  # Interval overridden in before_loop from config
    async def _playit_watchdog(self):
        if _is_playit_running():
            return

        logger.warning("playit.gg process not found. Attempting restart...")
        try:
            await asyncio.to_thread(_start_playit)
            logger.info("playit.gg restarted successfully.")
            await self._notify_health_channel("⚠️ playit.gg was not running and has been automatically restarted.")
        except Exception as e:
            logger.error(f"Failed to restart playit.gg: {e}")
            await self._notify_health_channel(f"❌ playit.gg is not running and could not be restarted: {e}")

    @_playit_watchdog.before_loop
    async def _before_playit_watchdog(self):
        await self.bot.wait_until_ready()

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    async def _notify_health_channel(self, message: str):
        channel_id = self.bot.config.get("discord.healthIssuesChannelID", None)
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if not channel:
            logger.warning(f"healthIssuesChannelID {channel_id} not found or bot cannot access it.")
            return
        try:
            await channel.send(message)
        except Exception as e:
            logger.warning(f"Failed to send health notification: {e}")

    # ---------------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------------

    @app_commands.command(name="check_updates", description="Manually check for Nexa updates.")
    async def check_updates(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_operator(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        result = await asyncio.to_thread(check_for_updates)

        if result == 1:
            await interaction.followup.send("An update is available! Check the GitHub repository for the latest release.", ephemeral=True)
        elif result == 0:
            await interaction.followup.send(f"Nexa is up to date ({CURRENT_NEXA_VERSION}).", ephemeral=True)
        else:
            await interaction.followup.send("Failed to check for updates. Please try again later.", ephemeral=True)