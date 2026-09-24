# cogs/superuser.py
# Under the MIT License.
#
# Superuser-only commands for instance and bot management.

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import commands
from discord import app_commands, Interaction

from backend.instanceManager import ServerStatus, ServerInstance
from services import nexaLoggerFactory

from ..ui import AuthRequestModal, ServerStatusEmbed

if TYPE_CHECKING:
    from ..discordBot import NexaBot

logger = nexaLoggerFactory.get_logger("SuperUserCog")


class SuperUserCog(commands.Cog):
    """Default superuser commands."""

    def __init__(self, bot: NexaBot):
        self.bot = bot

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    def _instance_choices(self) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=n, value=n)
            for n in self.bot.instance_manager.instances.keys()
        ]

    async def _instance_autocomplete(self, interaction: Interaction, current: str):
        return [
            app_commands.Choice(name=n, value=n)
            for n in self.bot.instance_manager.instances.keys()
            if current.lower() in n.lower()
        ]

    async def _resolve_status_message(self) -> Optional[discord.Message]:
        """Find the most recent bot embed in the status channel."""
        if not self.bot.statusChannelID:
            return None
        channel = self.bot.get_channel(self.bot.statusChannelID)
        if not channel:
            return None
        async for msg in channel.history(limit=10):
            if msg.author == self.bot.user:
                return msg
        return None

    # ---------------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------------

    @app_commands.command(name="stop", description="Stops the primary server instance.")
    async def stop(self, interaction: Interaction):
        if not await self.bot.guard.evaluate(interaction, "stop"):
            return
        
        instance = self.bot.instance_manager.get_primary_instance()
        if not instance:
            await interaction.response.send_message("No primary instance configured.", ephemeral=True)
            return
        await interaction.response.send_message(f"Stopping `{instance.name}`…", ephemeral=True)
        status_msg = await self._resolve_status_message()

        async def update_embed(inst: ServerInstance):
            if status_msg:
                status_embed = ServerStatusEmbed(inst)
                await status_msg.edit(embed=status_embed.build(), view=status_embed.build_view())

        asyncio.create_task(self.bot.instance_manager.stop_instance(instance.name, update_embed_callback=update_embed))

    @app_commands.command(name="stop_specific", description="Stops a specific instance.")
    @app_commands.describe(instance="The instance to stop.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def stop_specific(self, interaction: Interaction, instance: str):
        if not await self.bot.guard.evaluate(interaction, "stop_specific"):
            return
        
        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if tgt.status in (ServerStatus.OFFLINE, ServerStatus.SLEEPING):
            await interaction.response.send_message(f"`{tgt.name}` is already {tgt.status.value}.", ephemeral=True)
            return
        await interaction.response.send_message(f"Stopping `{tgt.name}`…", ephemeral=True)
        asyncio.create_task(self.bot.instance_manager.stop_instance(tgt.name))