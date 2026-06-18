# discordBot.py
# Under the MIT License.

# This file has now been deprecated in favor of a cleaner, more modular structure.
# The new entry point is discordBot.py. This file is kept for reference and will be removed in a future release.

import os
import json
import asyncio
import inspect
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Callable, Optional, Awaitable, List

import hashlib

import discord
from discord.ext import commands
from discord import app_commands, Interaction, TextChannel

from backend.instanceManager import InstanceManager, ServerStatus, ServerInstance
from services.nexaConfig import NexaConfig, NexaInstanceRegistry
from services.nexaDB import unprotectedDB, protectedDB
from services import nexaLoggerFactory

from services.modpackInstaller import ModpackInstaller, InstallStage, STAGE_LABELS

logger = nexaLoggerFactory.get_logger("DiscordBot")


# I really should move this to a better place.
VERSION = "Nexa v0.2.2-beta"

# ---------------------------------------------------------------------------
# UI Primitives
# ---------------------------------------------------------------------------

@dataclass
class MenuButton:
    label: str
    callback: Callable[[Interaction, "SimpleMenu"], Awaitable | None]
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    emoji: str | None = None
    disabled: bool = False
    row: int | None = None


@dataclass
class MenuPage:
    title: str
    description: str
    on_enter: Optional[Callable[[Interaction, "SimpleMenu"], Awaitable | None]] = None
    buttons: Optional[list[MenuButton]] = None


class SimpleMenu(discord.ui.View):
    def __init__(self, owner: discord.User, *, timeout: int = 300, ephemeral: bool = True):
        super().__init__(timeout=timeout)
        self.owner = owner
        self.pages: List[MenuPage] = []
        self.index = 0
        self.ephemeral = ephemeral
        self.message: Optional[discord.Message] = None
        self._dynamic_buttons: List[discord.ui.Button] = []

    def add_page(self, *, title: str, description: str,
                 on_enter=None,
                 buttons: Optional[List[MenuButton]] = None):
        self.pages.append(MenuPage(title, description, on_enter, buttons))
        return self

    async def send(self, interaction: Interaction):
        self._build_page()
        await interaction.response.send_message(
            embed=self._embed(), view=self, ephemeral=self.ephemeral
        )
        self.message = await interaction.original_response()
        await self._run_on_enter(interaction)

    async def refresh(self, interaction: Interaction):
        self._build_page()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    def _embed(self) -> discord.Embed:
        page = self.pages[self.index]
        embed = discord.Embed(title=page.title, description=page.description, color=0x5865F2)
        embed.set_footer(text=f"Page {self.index + 1}/{len(self.pages)} • Nexabot")
        return embed

    async def _run_on_enter(self, interaction: Interaction):
        page = self.pages[self.index]
        if page.on_enter:
            result = page.on_enter(interaction, self)
            if inspect.isawaitable(result):
                await result

    async def interaction_check(self, interaction: Interaction) -> bool:
        if interaction.user != self.owner:
            await interaction.response.send_message("This menu isn't yours.", ephemeral=True)
            return False
        return True

    def _clear_dynamic_buttons(self):
        for button in list(self._dynamic_buttons):
            try:
                self.remove_item(button)
            except Exception:
                pass
        self._dynamic_buttons.clear()

    def _build_page(self):
        self._clear_dynamic_buttons()
        if not self.pages:
            return
        page = self.pages[self.index]
        if not page.buttons:
            return

        for btn_model in page.buttons:
            button = discord.ui.Button(
                label=btn_model.label,
                style=btn_model.style,
                emoji=btn_model.emoji,
                disabled=btn_model.disabled,
                row=btn_model.row
            )

            async def callback(interaction: Interaction, model=btn_model):
                try:
                    result = model.callback(interaction, self)
                    if inspect.isawaitable(result):
                        await result
                except Exception as e:
                    try:
                        await interaction.response.send_message(f"Error: {e}", ephemeral=True)
                    except Exception:
                        pass

            button.callback = callback
            self.add_item(button)
            self._dynamic_buttons.append(button)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: Interaction, _):
        self.index = (self.index - 1) % len(self.pages)
        self._build_page()
        await interaction.response.edit_message(embed=self._embed(), view=self)
        await self._run_on_enter(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=4)
    async def next(self, interaction: Interaction, _):
        self.index = (self.index + 1) % len(self.pages)
        self._build_page()
        await interaction.response.edit_message(embed=self._embed(), view=self)
        await self._run_on_enter(interaction)


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------

class ServerStatusEmbed:
    def __init__(self, instance: ServerInstance):
        self.instance = instance

    def build(self) -> discord.Embed:
        color_map = {
            ServerStatus.ONLINE:   0x57F287,
            ServerStatus.STARTING: 0xFEE75C,
            ServerStatus.OFFLINE:  0xED4245,
            ServerStatus.SLEEPING: 0x5865F2,
        }
        color = color_map.get(self.instance.status, 0x5865F2)
        embed = discord.Embed(
            title=f"Server Status: {self.instance.name}",
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Status",     value=self.instance.status.value.capitalize(), inline=True)
        embed.add_field(name="Players",    value=f"{self.instance.players}/{self.instance.max_players}", inline=True)
        embed.add_field(name="Version",    value=self.instance.version,  inline=True)
        embed.add_field(name="Modloader",  value=self.instance.loader,   inline=True)
        if self.instance.icon_url:
            embed.set_thumbnail(url=self.instance.icon_url)
        embed.set_footer(text="Nexa V2")
        return embed


# ---------------------------------------------------------------------------
# Cogs
# ---------------------------------------------------------------------------

class GeneralCog(commands.Cog):
    """General-purpose commands available to all users."""

    def __init__(self, bot: "NexaBot"):
        self.bot = bot

    @app_commands.command(name="ping", description="Tests the bot.")
    async def ping(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return
        await interaction.response.send_message(f"Latency: {self.bot.latency:.2f}s", ephemeral=True)

    @app_commands.command(name="refresh_cmds", description="Tricks Discord into refreshing commands for a client.")
    async def refresh_cmds(self, interaction: Interaction):
        await interaction.response.send_message("Commands up to date.", ephemeral=True)

    @app_commands.command(name="status", description="Shows server stats.")
    async def status(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return
        instance = self.bot.instance_manager.get_primary_instance()
        if not instance:
            await interaction.response.send_message("No server instance is configured.", ephemeral=True)
            return

        menu = SimpleMenu(interaction.user)
        menu.add_page(
            title=f"Server Status: {instance.name}",
            description=(
                f"**Status:** {instance.status.value.capitalize()}\n"
                f"**Players:** {instance.players}/{instance.max_players}\n"
                f"**Version:** {instance.version}\n"
                f"**Modloader:** {instance.loader}"
            )
        )
        menu.add_page(
            title="Usage",
            description="• `/start`: Start the server\n• `/stop`: Stop the server"
        )
        menu.add_page(
            title="Diagnostics",
            description=(
                f"**Instance Name:** {instance.name}\n"
                f"**Status Channel:** {f'<#{self.bot.statusChannelID}>' if self.bot.statusChannelID else 'not configured'}\n"
                f"**Update Interval:** {self.bot.updateInterval}s"
            )
        )
        await menu.send(interaction)

    @app_commands.command(name="userdata", description="Show and manage your user data stored by NexaBot.")
    async def userdata(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return

        self.bot.userData.load()
        user_data = self.bot.userData.fetchEntry(str(interaction.user.id)) or {}
        self.bot.userData.unload()

        menu = SimpleMenu(interaction.user)

        menu.add_page(
            title="Introduction",
            description=(
                "This section allows you to view and manage your user data stored by NexaBot.\n\n"
                "The following pages will allow you to see what data is stored, and perform data management actions."
            )
        )
        menu.add_page(
            title="Basic Data",
            description=(
                f"**Minecraft Username:** {user_data.get('minecraftUser', 'Not linked')}\n"
                f"**User ID:** {interaction.user.id}"
            )
        )

        authed_apps = user_data.get("authorizedNexusApps", [])
        menu.add_page(
            title="Authorized Nexus Applications",
            description=(
                "\n".join(f"- {app}" for app in authed_apps)
                if authed_apps else "You have not authorized any applications."
            )
        )

        privacy = user_data.get("privacySettings", {})

        async def remove_privacy_setting(interaction: Interaction, _menu: SimpleMenu):
            class RemoveSettingModal(discord.ui.Modal, title="Remove Privacy Setting"):
                setting_name = discord.ui.TextInput(
                    label="Setting Name", placeholder="e.g. abstractIdentifiers", required=True
                )

                async def on_submit(modal_self, interaction: Interaction):
                    key = modal_self.setting_name.value.strip()
                    if key in privacy:
                        del privacy[key]
                        self.bot.userData.load()
                        entry = self.bot.userData.fetchEntry(str(interaction.user.id)) or {}
                        entry["privacySettings"] = privacy
                        self.bot.userData.setEntry(str(interaction.user.id), entry)
                        self.bot.userData.unload()
                        await interaction.response.send_message(f"Removed `{key}`.", ephemeral=True)
                    else:
                        await interaction.response.send_message(f"`{key}` not found.", ephemeral=True)

            await interaction.response.send_modal(RemoveSettingModal())

        menu.add_page(
            title="Privacy Settings",
            description=(
                "\n".join(f"- {k}: {v}" for k, v in privacy.items())
                if privacy else "You have not set any privacy settings."
            ),
            buttons=[MenuButton(label="Remove Setting", style=discord.ButtonStyle.danger, callback=remove_privacy_setting)]
        )

        async def delete_all_data(interaction: Interaction, _menu: SimpleMenu):
            """Callback to show delete confirmation menu."""
            confirm_menu = SimpleMenu(interaction.user)
            
            async def confirm_delete(interaction: Interaction, _menu: SimpleMenu):
                self.bot.userData.load()
                self.bot.userData.deleteEntry(str(interaction.user.id))
                self.bot.userData.unload()
                await interaction.response.edit_message(
                    content="✓ All your data has been successfully deleted. You will need to re-agree to terms to use the bot again.",
                    embed=None, view=None
                )
            
            async def cancel_delete(interaction: Interaction, _menu: SimpleMenu):
                await interaction.response.edit_message(
                    content="Deletion cancelled.",
                    embed=None, view=None
                )
            
            confirm_menu.add_page(
                title="Delete All Data",
                description="Are you sure you want to delete all your stored data? This action cannot be undone.",
                buttons=[
                    MenuButton(label="Confirm Delete", style=discord.ButtonStyle.danger, callback=confirm_delete),
                    MenuButton(label="Cancel", style=discord.ButtonStyle.secondary, callback=cancel_delete),
                ]
            )
            await confirm_menu.send(interaction)

        menu.add_page(
            title="Delete All Data",
            description="If you wish to delete all of your stored data, click the button below.\n\n**WARNING:** This action cannot be undone.",
            buttons=[MenuButton(label="Delete All Data", style=discord.ButtonStyle.danger, callback=delete_all_data)]
        )

        await menu.send(interaction)


class InstancesCog(commands.Cog):
    """Instance management commands, like start, stop, lock, unlock."""

    def __init__(self, bot: "NexaBot"):
        self.bot = bot

    # --- Helpers ---

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

    # --- Commands ---

    @app_commands.command(name="start", description="Starts the primary server instance.")
    async def start(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return
        instance = self.bot.instance_manager.get_primary_instance()
        if not instance:
            await interaction.response.send_message("No primary instance configured.", ephemeral=True)
            return
        if instance.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            await interaction.response.send_message(f"`{instance.name}` is already {instance.status.value}.", ephemeral=True)
            return
        if getattr(instance, "locked", False):
            await interaction.response.send_message(f"`{instance.name}` is locked and cannot be started.", ephemeral=True)
            return
        await interaction.response.send_message(f"Starting `{instance.name}`…", ephemeral=True)
        asyncio.create_task(self.bot.instance_manager.start_instance(instance.name))

    @app_commands.command(name="start_specific", description="Starts a specific instance.")
    @app_commands.describe(instance="The instance to start.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def start_specific(self, interaction: Interaction, instance: str):
        if not await self.bot.check_terms(interaction):
            return
        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if tgt.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            await interaction.response.send_message(f"`{tgt.name}` is already {tgt.status.value}.", ephemeral=True)
            return
        if getattr(tgt, "locked", False):
            await interaction.response.send_message(f"`{tgt.name}` is locked and cannot be started.", ephemeral=True)
            return
        await interaction.response.send_message(f"Starting `{tgt.name}`…", ephemeral=True)
        asyncio.create_task(self.bot.instance_manager.start_instance(tgt.name))

    @app_commands.command(name="stop", description="Stops the primary server instance.")
    async def stop(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return
        
        if self.bot.config.get("discord.preventRandomPeopleFromStoppingInstances", True):
            # Make a special exception for super users
            if not await self.bot.check_superuser(interaction):
                await interaction.respond.send_message("You do not have permission to stop instances.", ephemeral=True)
                return

        instance = self.bot.instance_manager.get_primary_instance()
        if not instance:
            await interaction.response.send_message("No primary instance configured.", ephemeral=True)
            return
        await interaction.response.send_message(f"Stopping `{instance.name}`…", ephemeral=True)
        status_msg = await self._resolve_status_message()

        async def update_embed(inst: ServerInstance):
            if status_msg:
                await status_msg.edit(embed=ServerStatusEmbed(inst).build())

        asyncio.create_task(self.bot.instance_manager.stop_instance(instance.name, update_embed_callback=update_embed))

    @app_commands.command(name="stop_specific", description="Stops a specific instance.")
    @app_commands.describe(instance="The instance to stop.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def stop_specific(self, interaction: Interaction, instance: str):
        if not await self.bot.check_terms(interaction):
            return
        
        if self.bot.config.get("discord.preventRandomPeopleFromStoppingInstances", True):
            # Make a special exception for super users
            if not await self.bot.check_superuser(interaction):
                await interaction.respond.send_message("You do not have permission to stop instances.", ephemeral=True)
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


class SuperUserCog(commands.Cog):
    """Superuser-only commands."""

    def __init__(self, bot: "NexaBot"):
        self.bot = bot

    # --- Helpers ---

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

    # --- Commands ---

    @app_commands.command(name="config", description="SUPERUSER-ONLY. View a config file.")
    @app_commands.describe(config_key="The config file to view.")
    @app_commands.choices(config_key=[
        app_commands.Choice(name="NexaBotConfig.yaml",       value="NexaBotConfig"),
        app_commands.Choice(name="NexaInstanceRegistry.yaml", value="NexaInstanceRegistry"),
    ])
    async def config(self, interaction: Interaction, config_key: app_commands.Choice[str]):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        cfg = self.bot.config if config_key.value == "NexaBotConfig" else self.bot.registry
        embed = discord.Embed(
            title=f"{config_key.name} Contents",
            description=f"```yaml\n{json.dumps(cfg.dumpData(), indent=2)}\n```",
            color=0x5865F2
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="execute", description="SUPERUSER-ONLY. Execute a raw RCON command on an instance.")
    @app_commands.describe(instance="The instance to run the command on.", command="The RCON command to execute.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def execute(self, interaction: Interaction, instance: str, command: str):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(f"Instance `{instance}` not found.", ephemeral=True)
            return
        
        # Check if command is protected
        
        cleanedCmd = command.lstrip("/")
        protected_cmds = tgt.get_protected_commands() or []
        if cleanedCmd.split()[0] in protected_cmds:
            await interaction.response.send_message(f"Command `{cleanedCmd.split()[0]}` is protected and cannot be executed through this interface.", ephemeral=True)
            return

        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(None, tgt.executeCommand, command)
            embed = discord.Embed(
                title=f"RCON Response: {tgt.name}",
                description=f"**Command:** `{command}`\n**Response:**\n```{response}```",
                color=0x5865F2
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except RuntimeError as e:
            await interaction.response.send_message(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="lock_instance", description="SUPERUSER-ONLY. Prevent an instance from being started.")
    @app_commands.describe(instance="The instance to lock.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def lock_instance(self, interaction: Interaction, instance: str):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(f"Instance `{instance}` not found.", ephemeral=True)
            return

        if tgt.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            await interaction.response.send_message(f"`{instance}` is currently {tgt.status.value} and cannot be locked. Ensure the instance is offline before locking.", ephemeral=True)
            return
        if getattr(tgt, "locked", False):
            await interaction.response.send_message(f"`{instance}` is already locked.", ephemeral=True)
            return

        tgt.locked = True
        logger.info(f"Instance '{instance}' locked by {interaction.user} ({interaction.user.id}).")
        await interaction.response.send_message(f"`{instance}` is now locked. It cannot be started until unlocked.", ephemeral=True)

    @app_commands.command(name="unlock_instance", description="SUPERUSER-ONLY. Allow a locked instance to be started again.")
    @app_commands.describe(instance="The instance to unlock.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def unlock_instance(self, interaction: Interaction, instance: str):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if not getattr(tgt, "locked", False):
            await interaction.response.send_message(f"`{instance}` is not locked.", ephemeral=True)
            return

        tgt.locked = False
        logger.info(f"Instance '{instance}' unlocked by {interaction.user} ({interaction.user.id}).")
        await interaction.response.send_message(f"`{instance}` is now unlocked.", ephemeral=True)

    @app_commands.command(name="force_stop", description="SUPERUSER-ONLY. Force stop an instance immediately.")
    @app_commands.describe(instance="The instance to force stop.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def force_stop(self, interaction: Interaction, instance: str):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if tgt.status in (ServerStatus.OFFLINE):
            await interaction.response.send_message(f"`{instance}` is already {tgt.status.value}.", ephemeral=True)
            return

        await interaction.response.send_message(f"Force stopping `{tgt.name}`.", ephemeral=True)
        asyncio.create_task(self.bot.instance_manager.stop_instance(tgt.name, hard=True))

    # I apologize for the code vomit. You have been warned.
    # Add this to SuperUserCog in discordBotV2.py

    @app_commands.command(
        name="install_mpck",
        description="SUPERUSER-ONLY. Install a .mrpack modpack to an instance."
    )
    @app_commands.describe(
        url="Direct URL to the .mrpack file.",
        instance="The instance to install the modpack to."
    )
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def install_mpck(self, interaction: Interaction, url: str, instance: str):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await interaction.response.send_message(
                f"❌ Instance `{instance}` not found.", ephemeral=True
            )
            return

        if getattr(tgt, "locked", False):
            await interaction.response.send_message(
                f"❌ Instance `{instance}` is already locked. ", ephemeral=True
            )
            return

        # --- Build the live install embed ---
        def _build_embed(stage_label: str, detail: str = "", failed: bool = False, cancellable: bool = False) -> discord.Embed:
            color = 0xED4245 if failed else (0xFEE75C if not stage_label.startswith("🎉") else 0x57F287)
            embed = discord.Embed(
                title=f"Installing Modpack to {instance}",
                description=f"**{stage_label}**\n{detail}".strip(),
                color=color,
                timestamp=datetime.now(timezone.utc)
            )
            if cancellable:
                embed.set_footer(text="React with the Cancel button to abort the scheduled shutdown.")
            else:
                embed.set_footer(text="Nexa • Modpack Installer")
            return embed

        # Post initial public embed to the status channel if available, else ephemeral
        channel = self.bot.get_channel(self.bot.statusChannelID) if self.bot.statusChannelID else None
        install_msg: Optional[discord.Message] = None

        await interaction.response.send_message(
            f"Starting modpack installation for `{instance}`…", ephemeral=True
        )

        if channel:
            install_msg = await channel.send(
                embed=_build_embed(STAGE_LABELS[InstallStage.DOWNLOADING_MRPACK])
            )

        # --- Cancellable shutdown state ---
        shutdown_cancelled = False

        async def handle_players():
            """If players are online, schedule shutdown and present cancel option."""
            nonlocal shutdown_cancelled

            await tgt.refresh_players()
            if tgt.players > 0 and tgt.status == ServerStatus.ONLINE:
                # Schedule shutdown
                await self.bot.instance_manager.schedule_shutdown(
                    instance,
                    delay_seconds=15 * 60,
                    reason="Modpack installation scheduled by operator.",
                    hard=True
                )

                # Update embed with cancel option
                if install_msg:
                    cancel_view = discord.ui.View(timeout=15 * 60)
                    cancel_btn  = discord.ui.Button(
                        label="Cancel Shutdown",
                        style=discord.ButtonStyle.danger,
                        emoji="🛑"
                    )

                    async def on_cancel(btn_interaction: Interaction):
                        nonlocal shutdown_cancelled
                        if not self.bot._is_superuser(btn_interaction.user.id):
                            await btn_interaction.response.send_message(
                                "Only superusers can cancel this.", ephemeral=True
                            )
                            return
                        shutdown_cancelled = True
                        self.bot.instance_manager.cancel_shutdown(instance)
                        await btn_interaction.response.send_message(
                            "Shutdown cancelled. Install aborted.", ephemeral=True
                        )
                        await install_msg.edit(
                            embed=_build_embed("🛑 Install cancelled by operator.", failed=True),
                            view=None
                        )

                    cancel_btn.callback = on_cancel
                    cancel_view.add_item(cancel_btn)

                    await install_msg.edit(
                        embed=_build_embed(
                            STAGE_LABELS[InstallStage.WAITING_FOR_SHUTDOWN],
                            f"{tgt.players} player(s) online. Server shutting down in 15 minutes.",
                            cancellable=True
                        ),
                        view=cancel_view
                    )

                # Wait for OFFLINE or cancellation
                while tgt.status != ServerStatus.OFFLINE:
                    if shutdown_cancelled:
                        return False
                    await asyncio.sleep(2)

                # Remove cancel button once server is down
                if install_msg:
                    await install_msg.edit(view=None)

            elif tgt.status in (ServerStatus.ONLINE,):
                # Online but empty, so stop immediately
                await self.bot.instance_manager.stop_instance(instance, hard=True)
                while tgt.status != ServerStatus.OFFLINE:
                    await asyncio.sleep(2)

            return True

        # --- Status callback for the installer ---
        async def on_status(status):
            if install_msg:
                label = STAGE_LABELS.get(status.stage, status.stage.name)
                await install_msg.edit(
                    embed=_build_embed(label, status.detail, failed=status.failed),
                    view=None
                )

        # --- Run shutdown handling before handing off to installer ---
        async def _run_install():
            nonlocal shutdown_cancelled

            proceed = await handle_players()
            if not proceed or shutdown_cancelled:
                return

            installer = ModpackInstaller(
                url=url,
                instance_name=instance,
                instance_manager=self.bot.instance_manager,
                registry=self.bot.registry,
                on_status=on_status,
            )

            result = await installer.run()

            # Ping the superuser on completion
            try:
                if result.success:
                    await interaction.user.send(
                        f"✅ Modpack installation for `{instance}` completed successfully."
                    )
                else:
                    await interaction.user.send(
                        f"❌ Modpack installation for `{instance}` failed:\n{result.message}"
                    )
            except discord.Forbidden:
                pass  # User has DMs closed

            # Auto-remove install embed after 3 minutes on success
            if result.success and install_msg:
                await asyncio.sleep(180)
                try:
                    await install_msg.delete()
                except Exception:
                    pass

            # Also do it for failure.
            if not result.success and install_msg:
                await asyncio.sleep(180)
                try:
                    await install_msg.delete()
                except Exception:
                    pass

        asyncio.create_task(_run_install())

    # Command to test AuthRequestModal and AuthPermissionView
    @app_commands.command(name="test_auth_modal", description="SUPERUSER-ONLY. Test the authorization request modal and permission view.")
    async def test_auth_modal(self, interaction: Interaction):
        if not await self.bot.check_terms(interaction):
            return
        if not await self.bot.check_superuser(interaction):
            return

        requestor = "ExampleApp"
        purpose = "Access your Minecraft username for personalized features."
        permissions = ["Read Minecraft username", "Send you notifications"]

        await interaction.response.send_modal(
            AuthRequestModal(requestor, purpose, permissions)
        )
# ---------------------------------------------------------------------------
# Custom-ish components
# ---------------------------------------------------------------------------

class AuthRequestModal(discord.ui.Modal, title="Authorization Required"):
    def __init__(self, requestor: str, purpose: str, authorizations: list[str]):
        super().__init__(custom_id="auth_modal")

        self.requestor = requestor
        self.purpose = purpose
        self.authorizations = authorizations

        # Context display
        self.info = discord.ui.TextInput(
            label="Request Details",
            style=discord.TextStyle.paragraph,
            default=(
                f"Requestor: {self.requestor}\n"
                f"Purpose: {self.purpose}\n\n"
                "Select which permissions to grant below."
            ),
            required=False
        )

        self.add_item(self.info)

        # V2 Checkbox Group (Components API)
        # TODO: Fix.
        self.permissions = discord.ui.CheckboxGroup(
            custom_id="permissions",
            min_values=0,
            max_values=len(authorizations),
            options=[
                discord.SelectOption(label=perm, value=perm)
                for perm in authorizations
            ],
            label="Permissions"
        )

        self.add_item(self.permissions)

    async def on_submit(self, interaction: discord.Interaction):
        selected = self.permissions.values

        await interaction.response.send_message(
            view=AuthConfirmView(selected),
            ephemeral=True
        )

class AuthConfirmView(discord.ui.View):
    def __init__(self, selected_permissions: list[str]):
        super().__init__(timeout=None)
        self.selected_permissions = selected_permissions

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: Interaction, _):
        await interaction.response.send_message(
            f"Permissions granted: {', '.join(self.selected_permissions)}",
            ephemeral=True
        )
# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class NexaBot(commands.Bot):
    """
    The main Discord bot class.
    Integrates with NexaConfig, NexaInstanceRegistry, and InstanceManager.
    """

    def __init__(
        self,
        token: str,
        instance_manager: InstanceManager,
        *,
        registry: NexaInstanceRegistry | str | None = None,
        config: NexaConfig | str | None = None,
        statusChannelID: int | None = None,
        nexaUpdateStatus: int,
    ):
        intents = discord.Intents.default()
        intents.message_content = False
        super().__init__(command_prefix="/", intents=intents)

        self.token_str = token
        self.instance_manager = instance_manager
        self.nexaUpdateStatus = nexaUpdateStatus

        # Config
        self.config = config if isinstance(config, NexaConfig) else NexaConfig(
            config if isinstance(config, str) else "NexaBotConfig.yaml"
        )

        # Registry
        self.registry = registry if isinstance(registry, NexaInstanceRegistry) else NexaInstanceRegistry(
            registry if isinstance(registry, str) else "NexaInstanceRegistry.yaml"
        )

        self.statusChannelID = statusChannelID or self.config.get("discord.statusChannel", None)
        self.updateInterval  = int(self.config.get("general.updateInterval", 10))

        # Protected DB
        db_key = os.environ.get("NEXABOT_PROTECTED_KEY")
        if not db_key:
            logger.error("NEXABOT_PROTECTED_KEY is not set. Cannot start.")
            raise ValueError("NEXABOT_PROTECTED_KEY environment variable is not set.")
        self.userData = protectedDB(
            dbPath=Path("databases") / "userData.nxdb",
            password=db_key,
            create_if_missing=True
        )

        self._hydrate_instances()

    # ---------------------------------------------------------------------------
    # Guards
    # ---------------------------------------------------------------------------

    def _is_authorized_guild(self, guild_id: int | None) -> bool:
        if not self.config.get("discord.lockToAuthorizedGuild", False):
            return True
        if guild_id is None:
            return False  # DM interactions are not authorized
        authorized = self.config.get("discord.authorizedGuilds") or []
        return guild_id in authorized

    async def check_guild(self, interaction: Interaction) -> bool:
        if self._is_authorized_guild(interaction.guild_id):
            return True
        await interaction.response.send_message(
            "This bot is not authorized for use in this server.", ephemeral=True
        )
        logger.warning(f"Unauthorized guild access attempt by {interaction.user} ({interaction.user.id}) in guild {(await self.fetch_guild(interaction.guild_id)).name} ({interaction.guild_id}).")
        return False

    def _is_superuser(self, user_id: int) -> bool:
        return (
            self.config.get("discord.enableSuperUsers", False)
            and user_id in (self.config.get("discord.superUsers") or [])
        )

    def _has_agreed_to_terms(self, user_id: int) -> bool:
        self.userData.load()
        exists = self.userData.fetchEntry(str(user_id)) is not None
        self.userData.unload()
        return exists

    async def check_terms(self, interaction: Interaction) -> bool:
        """
        Returns True if the user has agreed to terms.
        If not, sends the terms menu and returns False.
        """
        if not await self.check_guild(interaction):
            return False
        if self._has_agreed_to_terms(interaction.user.id):
            return True

        menu = SimpleMenu(interaction.user)

        async def _agree(interaction: Interaction, _menu: SimpleMenu):
            self.userData.load()
            self.userData.setEntry(str(interaction.user.id), {
                "minecraftUser": None,
                "authorizedNexusApps": [],
                "privacySettings": {}
            })
            self.userData.unload()
            await interaction.response.edit_message(
                content="Thank you for agreeing! Please re-run your previous command.",
                embed=None, view=None
            )

        async def _decline(interaction: Interaction, _menu: SimpleMenu):
            await interaction.response.edit_message(
                content="Understood. Re-run any command to see the agreement again.",
                embed=None, view=None
            )

        menu.add_page(
            title="Terms of Service",
            description=(
                "In order to use Nexa, you must agree to our data usage terms:\n\n"
                "- We store a mapping of your Discord ID to any Minecraft accounts you link.\n"
                "- We store a list of any Nexus apps you authorize.\n"
                "- We store privacy settings you configure.\n\n"
                "Your data is encrypted and never shared with third parties.\n\n"
                "Click **I Agree** to continue."
            ),
            buttons=[
                MenuButton(label="I Agree",         style=discord.ButtonStyle.success, callback=_agree),
                MenuButton(label="I Do Not Agree",  style=discord.ButtonStyle.danger,  callback=_decline),
            ]
        )
        await menu.send(interaction)
        return False

    async def check_superuser(self, interaction: Interaction) -> bool:
        """
        Returns True if the user is a superuser.
        If not, sends an error and returns False.
        """
        if not await self.check_guild(interaction):
            await interaction.response.send_message(
            "An unknown error occurred.", ephemeral=True
            )
            logger.warning(f"check_superuser called for user {interaction.user} ({interaction.user.id}) in unauthorized guild {interaction.guild_id}.")
            return
        if self._is_superuser(interaction.user.id):
            return True
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
        return False

    # ---------------------------------------------------------------------------
    # Instance hydration
    # ---------------------------------------------------------------------------

    def _hydrate_instances(self):
        if self.instance_manager.instances:
            return

        instances_root = Path.cwd() / Path(self.config.get("general.instancesFolder", "instances"))

        try:
            instance_names = self.registry.list_instances()
        except Exception:
            instance_names = []

        for name in instance_names:
            try:
                inst_cfg  = self.registry.get_instance(name) or {}
                folder    = inst_cfg.get("folder") or str(instances_root / name)
                version   = inst_cfg.get("version", "")
                loader    = inst_cfg.get("loaderType") or inst_cfg.get("loader") or ""
                icon_url  = inst_cfg.get("icon_url") or inst_cfg.get("icon") or None

                self.instance_manager.add_instance(ServerInstance(
                    name=name, folder=folder, version=version,
                    loader=loader, icon_url=icon_url
                ))
                logger.info(f"Registered instance '{name}' -> {folder}")
            except Exception as e:
                logger.error(f"Failed to register instance '{name}': {e}")

    # ---------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------

    async def setup_hook(self):
        await self.add_cog(GeneralCog(self))
        await self.add_cog(InstancesCog(self))
        await self.add_cog(SuperUserCog(self))

    async def on_ready(self):
        logger.info(f"Logged in as {self.user}")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s).")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        await self.instance_manager.start()

        presenceName = ""
        if self.nexaUpdateStatus == 1:
            presenceName = f"{VERSION} (Update Available!)"
        else:
            presenceName = f"{VERSION}"

        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.playing, name=presenceName)
        )

        asyncio.create_task(self._live_status_loop())

    # ---------------------------------------------------------------------------
    # Live status loop + Helper
    # ---------------------------------------------------------------------------
    def _embed_fingerprint(self, embed: discord.Embed) -> str:
        """Cheap hash of the embed's visible content for change detection."""
        parts = [
            embed.title or "",
            embed.description or "",
            str(embed.color),
            "|".join(f"{f.name}={f.value}" for f in embed.fields),
            embed.footer.text if embed.footer else "",
        ]
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    async def _live_status_loop(self):
        await self.wait_until_ready()
        channel: TextChannel | None = self.get_channel(self.statusChannelID) if self.statusChannelID else None
        if channel is None:
            logger.warning(f"Status channel '{self.statusChannelID}' not found or not configured.")
            return

        # Resume existing embed messages by matching embed titles to instance names
        existing: dict[str, discord.Message] = {}
        async for msg in channel.history(limit=50):
            if msg.author == self.user and msg.embeds:
                title = msg.embeds[0].title or ""
                for name in self.instance_manager.instances:
                    if name in title and name not in existing:
                        existing[name] = msg

        status_messages: dict[str, discord.Message] = {}
        for name, instance in self.instance_manager.instances.items():
            if name in existing:
                status_messages[name] = existing[name]
            else:
                msg = await channel.send(embed=ServerStatusEmbed(instance).build())
                status_messages[name] = msg

        status_fingerprints: dict[str, str] = {}

        while not self.is_closed():
            for name, instance in self.instance_manager.instances.items():
                msg = status_messages.get(name)
                if not msg:
                    continue
                
                embed = ServerStatusEmbed(instance).build()
                if instance.status == ServerStatus.SLEEPING:
                    embed.title += ": Sleeping"
                if getattr(instance, "locked", False):
                    embed.title += " 🔒"

                fp = self._embed_fingerprint(embed)
                if status_fingerprints.get(name) == fp:
                    continue  # Nothing changed, so skip the edit
                
                try:
                    await msg.edit(embed=embed)
                    status_fingerprints[name] = fp
                except discord.NotFound:
                    new_msg = await channel.send(embed=ServerStatusEmbed(instance).build())
                    status_messages[name] = new_msg
                    status_fingerprints[name] = fp
                except Exception as e:
                    logger.warning(f"Failed to update status embed for '{name}': {e}")

            await asyncio.sleep(self.updateInterval)

    # ---------------------------------------------------------------------------
    # Entry point
    # ---------------------------------------------------------------------------

    def start_bot(self):
        self.run(self.token_str)