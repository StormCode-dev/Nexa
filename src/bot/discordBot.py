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
from services.nexaConfig import NexaConfig, NexaInstanceRegistry, NexaCmdConfig
from services.nexaDB import protectedDB
from services import nexaLoggerFactory
from bot.cmdServices.nxbotCmdInstanceEmbeds import InstanceEmbedTracker
from bot.cmdServices.nxbotGuard import NxbotGuard

from .ui import SimpleMenu, MenuButton, ServerStatusEmbed
from .cogs.general import GeneralCog
from .cogs.instances import InstancesCog
from .cogs.superuser import SuperUserCog
from .cogs.operator import OperatorCog

logger = nexaLoggerFactory.get_logger("DiscordBot")

VERSION = "Nexa v0.3.0-beta-hotfix1"


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
        cmdConfig: NexaCmdConfig | str | None = None,
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

        # Command Config
        self.cmdConfig = cmdConfig if isinstance(cmdConfig, NexaCmdConfig) else(
            config if isinstance(cmdConfig, str) else "NexaBotCmdCfg.yaml"
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

        # Instance embed tracker
        self.instanceEmbeds = InstanceEmbedTracker()

        # Guard
        self.guard = NxbotGuard(self, self.cmdConfig)

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
        #print(f"Server Ops Enable: {self.config.get("security.enableServerOperators", "Could not fetch.")}")
        #print(f"Result: {user_id in (self.config.get("security.serverOperators") or [])}")
        return (
            self.config.get("security.enableServerOperators", False)
            and user_id in (self.config.get("security.serverOperators") or [])
        )
    
    def _is_head_operator(self, user_id: int) -> bool:
        return (
            self.config.get("security.headOperator", 0) == user_id
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
                "- We store a list of any Nexa Routines you authorize.\n"
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
    
    async def check_head_operator(self, interaction: Interaction) -> bool:
        if not await self.check_guild(interaction):
            await interaction.response.send_message(
                "An unknown error occurred.", ephemeral=True
            )
            logger.warning(f"check_head_operator called for user {interaction.user} ({interaction.user.id}) in unauthorized guild {interaction.guild_id}.")
            return False
        if self._is_head_operator(interaction.user.id):
            return True
        await interaction.response.send_message(
            "This command is restricted to the head operator. You do not have permission to use this command.", ephemeral=True
        )
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
        await self.add_cog(OperatorCog(self))

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

    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
    
        custom_id = interaction.data.get("custom_id", "")
        if not custom_id.startswith("start_instance:"):
            return
    
        instance_name = custom_id.split(":", 1)[1]
    
        cog = self.get_cog("InstancesCog")
        if cog is None:
            await interaction.response.send_message("Instance commands are unavailable right now.", ephemeral=True)
            return
    
        await cog.start_specific.callback(cog, interaction, instance_name)


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

    async def _resolve_status_message(
        self, channel: TextChannel, name: str, instance: ServerInstance
    ) -> discord.Message:
        """
        Resolves the tracked status embed message for an instance, creating
        and registering a new one if none is tracked or the tracked message
        no longer exists.
        """
        link = self.instanceEmbeds.getEmbed(name)

        if link:
            try:
                channel_id_str, message_id_str = link.split(":", 1)
                msg_channel = self.get_channel(int(channel_id_str)) or channel
                return await msg_channel.fetch_message(int(message_id_str))
            except (discord.NotFound, discord.Forbidden, ValueError):
                logger.warning(f"Tracked embed for '{name}' is stale or invalid. Creating a new one.")
            except Exception as e:
                logger.warning(f"Failed to resolve tracked embed for '{name}': {e}. Creating a new one.")

        status_embed = ServerStatusEmbed(instance)
        view = status_embed.build_view()

        msg = await channel.send(embed=status_embed.build(), view=view)
        self.instanceEmbeds.newEmbed(name, f"{msg.channel.id}:{msg.id}")
        return msg

    async def _live_status_loop(self):
        await self.wait_until_ready()
        channel: TextChannel | None = self.get_channel(self.statusChannelID) if self.statusChannelID else None
        if channel is None:
            logger.warning(f"Status channel '{self.statusChannelID}' not found or not configured.")
            return

        status_messages: dict[str, discord.Message] = {}
        for name, instance in self.instance_manager.instances.items():
            status_messages[name] = await self._resolve_status_message(channel, name, instance)

        status_fingerprints: dict[str, str] = {}

        while not self.is_closed():
            for name, instance in self.instance_manager.instances.items():
                msg = status_messages.get(name)
                if not msg:
                    continue

                status_embed = ServerStatusEmbed(instance)
                embed = status_embed.build()
                if instance.status == ServerStatus.SLEEPING:
                    embed.title += ": Sleeping"
                if getattr(instance, "locked", False):
                    embed.title += " 🔒"
                view = status_embed.build_view()

                fp = self._embed_fingerprint(embed)
                if status_fingerprints.get(name) == fp:
                    continue
                
                try:
                    await msg.edit(embed=embed, view=view)
                    status_fingerprints[name] = fp
                except discord.NotFound:
                    status_embed = ServerStatusEmbed(instance)
                    new_msg = await channel.send(embed=status_embed.build(), view=status_embed.build_view())
                    status_messages[name] = new_msg
                    status_fingerprints[name] = fp
                    self.instanceEmbeds.newEmbed(name, f"{new_msg.channel.id}:{new_msg.id}")
                    logger.warning(f"Status embed for '{name}' was deleted externally. Recreated and re-tracked.")
                except Exception as e:
                    logger.warning(f"Failed to update status embed for '{name}': {e}")

            await asyncio.sleep(self.updateInterval)

    # ---------------------------------------------------------------------------
    # Entry point
    # ---------------------------------------------------------------------------

    def start_bot(self):
        self.run(self.token_str)