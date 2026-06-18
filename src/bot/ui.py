# ui.py
# Under the MIT License.
#
# Shared UI primitives for NexaBot.
# Includes menus, embeds, and authorization components used across cogs.

import inspect
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Awaitable, List

import discord
from discord import Interaction

from backend.instanceManager import ServerInstance, ServerStatus


# ---------------------------------------------------------------------------
# Menu Primitives
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
        embed.add_field(name="Status",    value=self.instance.status.value.capitalize(), inline=True)
        embed.add_field(name="Players",   value=f"{self.instance.players}/{self.instance.max_players}", inline=True)
        embed.add_field(name="Version",   value=self.instance.version,  inline=True)
        embed.add_field(name="Modloader", value=self.instance.loader,   inline=True)
        if self.instance.icon_url:
            embed.set_thumbnail(url=self.instance.icon_url)
        embed.set_footer(text="Nexa V2")
        return embed


# ---------------------------------------------------------------------------
# Authorization Components
# ---------------------------------------------------------------------------

class AuthRequestModal(discord.ui.Modal, title="Authorization Required"):
    def __init__(self, requestor: str, purpose: str, authorizations: list[str]):
        super().__init__(custom_id="auth_modal")

        self.requestor = requestor
        self.purpose = purpose
        self.authorizations = authorizations

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