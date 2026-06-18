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
from services.modpackInstaller import ModpackInstaller, InstallStage, STAGE_LABELS

from ..ui import AuthRequestModal, ServerStatusEmbed

if TYPE_CHECKING:
    from ..discordBot import NexaBot

logger = nexaLoggerFactory.get_logger("SuperUserCog")


class SuperUserCog(commands.Cog):
    """Superuser-only commands."""

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

    # ---------------------------------------------------------------------------
    # Commands
    # ---------------------------------------------------------------------------

    @app_commands.command(name="config", description="SUPERUSER-ONLY. View a config file.")
    @app_commands.describe(config_key="The config file to view.")
    @app_commands.choices(config_key=[
        app_commands.Choice(name="NexaBotConfig.yaml",        value="NexaBotConfig"),
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

        cleanedCmd = command.lstrip("/")
        protected_cmds = tgt.get_protected_commands() or []
        if cleanedCmd.split()[0] in protected_cmds:
            await interaction.response.send_message(
                f"Command `{cleanedCmd.split()[0]}` is protected and cannot be executed through this interface.",
                ephemeral=True
            )
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
            await interaction.response.send_message(
                f"`{instance}` is currently {tgt.status.value} and cannot be locked. Ensure the instance is offline before locking.",
                ephemeral=True
            )
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
        if tgt.status in (ServerStatus.OFFLINE,):
            await interaction.response.send_message(f"`{instance}` is already {tgt.status.value}.", ephemeral=True)
            return

        await interaction.response.send_message(f"Force stopping `{tgt.name}`.", ephemeral=True)
        asyncio.create_task(self.bot.instance_manager.stop_instance(tgt.name, hard=True))

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
            await interaction.response.send_message(f"❌ Instance `{instance}` not found.", ephemeral=True)
            return
        if getattr(tgt, "locked", False):
            await interaction.response.send_message(f"❌ Instance `{instance}` is already locked.", ephemeral=True)
            return

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

        channel = self.bot.get_channel(self.bot.statusChannelID) if self.bot.statusChannelID else None
        install_msg: Optional[discord.Message] = None

        await interaction.response.send_message(
            f"🚀 Starting modpack installation for `{instance}`…", ephemeral=True
        )

        if channel:
            install_msg = await channel.send(
                embed=_build_embed(STAGE_LABELS[InstallStage.DOWNLOADING_MRPACK])
            )

        shutdown_cancelled = False

        async def handle_players():
            nonlocal shutdown_cancelled

            await tgt.refresh_players()
            if tgt.players > 0 and tgt.status == ServerStatus.ONLINE:
                await self.bot.instance_manager.schedule_shutdown(
                    instance,
                    delay_seconds=15 * 60,
                    reason="Modpack installation scheduled by operator.",
                    hard=True
                )

                if install_msg:
                    cancel_view = discord.ui.View(timeout=15 * 60)
                    cancel_btn = discord.ui.Button(
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
                            "✅ Shutdown cancelled. Install aborted.", ephemeral=True
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

                while tgt.status != ServerStatus.OFFLINE:
                    if shutdown_cancelled:
                        return False
                    await asyncio.sleep(2)

                if install_msg:
                    await install_msg.edit(view=None)

            elif tgt.status in (ServerStatus.ONLINE,):
                await self.bot.instance_manager.stop_instance(instance, hard=True)
                while tgt.status != ServerStatus.OFFLINE:
                    await asyncio.sleep(2)

            return True

        async def on_status(status):
            if install_msg:
                label = STAGE_LABELS.get(status.stage, status.stage.name)
                await install_msg.edit(
                    embed=_build_embed(label, status.detail, failed=status.failed),
                    view=None
                )

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

            if install_msg:
                await asyncio.sleep(180)
                try:
                    await install_msg.delete()
                except Exception:
                    pass

        asyncio.create_task(_run_install())

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