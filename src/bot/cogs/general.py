# cogs/general.py
# Under the MIT License.
#
# General-purpose commands available to all users.

import discord
from discord.ext import commands
from discord import app_commands, Interaction

from services.nexaDB import protectedDB
from services import nexaLoggerFactory

from ..ui import SimpleMenu, MenuButton

logger = nexaLoggerFactory.get_logger("GeneralCog")


class GeneralCog(commands.Cog):
    """General-purpose commands available to all users."""

    def __init__(self, bot: "NexaBot"): # type: ignore
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
                    MenuButton(label="Confirm Delete", style=discord.ButtonStyle.danger,     callback=confirm_delete),
                    MenuButton(label="Cancel",         style=discord.ButtonStyle.secondary,  callback=cancel_delete),
                ]
            )
            await confirm_menu.send(interaction)

        menu.add_page(
            title="Delete All Data",
            description="If you wish to delete all of your stored data, click the button below.\n\n**WARNING:** This action cannot be undone.",
            buttons=[MenuButton(label="Delete All Data", style=discord.ButtonStyle.danger, callback=delete_all_data)]
        )

        await menu.send(interaction)