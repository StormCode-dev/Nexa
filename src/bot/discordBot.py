# discordBot.py
# Under the MIT License.

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands
from discord import Interaction, TextChannel

from backend.instanceManager import InstanceManager, ServerStatus, ServerInstance
from bot.cogs.system import SystemCog
from services.nexaConfig import NexaConfig, NexaInstanceRegistry
from services.nexaDB import protectedDB
from services import nexaLoggerFactory

from .ui import SimpleMenu, MenuButton, ServerStatusEmbed
from .cogs.general import GeneralCog
from .cogs.instances import InstancesCog
from .cogs.superuser import SuperUserCog

logger = nexaLoggerFactory.get_logger("DiscordBot")

VERSION = "Nexa v0.2.2-beta"


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
        isResurrected: bool = False
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
        self.healthChannelID = self.config.get("discord.healthIssuesChannelID", None)
        self.updateInterval  = int(self.config.get("general.updateInterval", 10))

        self.isResurrected = isResurrected

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
            return False
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
    
    def _is_server_operator(self, user_id: int) -> bool:
        return (
            self.config.get("security.enableServerOperators", False)
            and user_id in (self.config.get("security.serverOperators") or [])
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
                MenuButton(label="I Agree",        style=discord.ButtonStyle.success, callback=_agree),
                MenuButton(label="I Do Not Agree", style=discord.ButtonStyle.danger,  callback=_decline),
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
            return False
        if self._is_superuser(interaction.user.id):
            return True
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
        return False

    async def check_operator(self, interaction: Interaction) -> bool:
        """
        Returns True if the user is a server operator.
        If not, sends an error and returns False.
        """
        if not await self.check_guild(interaction):
            await interaction.response.send_message(
                "An unknown error occurred.", ephemeral=True
            )
            logger.warning(f"check_operator called for user {interaction.user} ({interaction.user.id}) in unauthorized guild {interaction.guild_id}.")
            return False
        if self._is_server_operator(interaction.user.id):
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
        await self.add_cog(SystemCog(self))

    async def on_ready(self):
        logger.info(f"Logged in as {self.user}")
        try:
            synced = await self.tree.sync()
            logger.info(f"Synced {len(synced)} command(s).")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")

        await self.instance_manager.start()

        presenceName = f"{VERSION} (Update Available!)" if self.nexaUpdateStatus == 1 else VERSION
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(type=discord.ActivityType.playing, name=presenceName)
        )

        if self.isResurrected:
            channel = self.get_channel(self.config.get("discord.healthIssuesChannelID", None))
            if channel:
                await channel.send("⚠️ Nexa was automatically restarted after an unexpected shutdown.")
            else:
                logger.warning("healthIssuesChannelID not configured or channel not found. Could not send resurrection notice.")
        else:
            channel = self.get_channel(self.config.get("discord.healthIssuesChannelID", None))
            if channel:
                await channel.send("Nexa has started successfully and is now online.")
            else:
                logger.warning("healthIssuesChannelID not configured or channel not found. Could not send resurrection notice.")

        asyncio.create_task(self._live_status_loop())

    # ---------------------------------------------------------------------------
    # Live status loop
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
                    continue

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