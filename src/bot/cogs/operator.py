# cogs/operator.py
# Under the MIT License.
#
# Bot commands exclusively reserved for server operators.

import os
import base64
import io
from pathlib import Path
from typing import List, Optional
import asyncio

import discord
from discord.ext import commands
from discord import app_commands, Interaction

from datetime import datetime, timezone

from services.nexaDB import protectedDB
from services.nexaConfig import NexaConfig
from services.nexaAuthenticationService import (
    NexaAuthenticationService,
    OperatorKeyAlreadyExistsError,
    OperatorKeyNotFoundError,
    InvalidCapabilityError,
)
from services.nexaVerifService import NexaVerifService
from services.nexaSftpService import NexaSftpService, SESSION_TIMEOUT_SECONDS
from services import nexaLoggerFactory
from services.modpackInstaller import ModpackInstaller, InstallStage, STAGE_LABELS

from backend.instanceManager import ServerStatus, ServerInstance

from bot.cmdServices.nxbotGuard import NxbotGuard

from ..cmdServices import nxbotCmdGeneral

from ..ui import SimpleMenu, MenuButton

logger = nexaLoggerFactory.get_logger("OperatorCog")


# Head Operator ticket info
TICKET_LIFETIME_SECONDS = 15 * 60
_headOperatorTicketExpiresAt: float = 0.0  # 0.0 / any past timestamp = no valid ticket

class InteractionResponder:
    """
    Wraps an Interaction so callers don't need to manually track whether an
    upstream step (_ensureHeadOperatorTicket, _ensureOpHasCorrectPerms)
    already deferred it. Both of those call interaction.response.defer()
    the moment they run, which permanently consumes interaction.response for
    the rest of that interaction's lifetime -- any response.send_message()
    after that point raises, and any followup.send() before it also raises.

    Create one per interaction, pass it to the auth helpers instead of the
    raw Interaction, and call responder.send(...) for every response in the
    command body instead of choosing between interaction.response.send_message
    and interaction.followup.send by hand.
    """
    def __init__(self, interaction: Interaction):
        self.interaction = interaction
        self.deferred = False

    async def defer(self, *args, **kwargs):
        await self.interaction.response.defer(*args, **kwargs)
        self.deferred = True

    async def send(self, *args, **kwargs):
        if self.deferred:
            return await self.interaction.followup.send(*args, **kwargs)
        return await self.interaction.response.send_message(*args, **kwargs)

def _hasValidTicket() -> bool:
    global _headOperatorTicketExpiresAt
    import time
    if time.time() >= _headOperatorTicketExpiresAt:
        _headOperatorTicketExpiresAt = 0.0  # explicitly zero out an expired ticket
        return False
    return True


def _issueTicket() -> None:
    global _headOperatorTicketExpiresAt
    import time
    _headOperatorTicketExpiresAt = time.time() + TICKET_LIFETIME_SECONDS

async def _ensureHeadOperatorTicket(responder: InteractionResponder, verifService: NexaVerifService) -> bool:
    """
    Called by every /keyman subcommand immediately after guard.evaluate(),
    and BEFORE the command does anything else with the interaction. Always
    defers via responder.defer(), so responder.send(...) is safe to call
    downstream for the rest of the command regardless of whether a ticket
    was already valid or a full verification flow had to run.

    verifService must be the cog's single, shared NexaVerifService instance.
    """
    await responder.defer(ephemeral=True)

    if _hasValidTicket():
        return True

    try:
        session = await verifService.beginAuthentication(
            discordUserID=responder.interaction.user.id,
            isHOVerif=True
        )
    except RuntimeError as e:
        await responder.send(str(e), ephemeral=True)
        return False

    embed = discord.Embed(
        title="Head Operator Verification Required",
        description=(
            f"Your /keyman session has expired or has not yet been verified this session. "
            f"Open this link or scan the QR code below, then submit your Head Operator "
            f"authentication code to continue.\n\n"
            f"**Link:** {session.tunnelUrl}"
        ),
        color=discord.Color.gold()
    )
    embed.set_image(url="attachment://qr.png")
    embed.set_footer(text="This verification will time out automatically if left idle.")

    qrBytes = base64.b64decode(session.qrCodeDataUri.split(",", 1)[1])
    qrFile = discord.File(io.BytesIO(qrBytes), filename="qr.png")

    message = await responder.send(embed=embed, file=qrFile, ephemeral=True, wait=True)

    logger.info(f"Head Operator ticket verification started by {responder.interaction.user.id}.")

    result = await verifService.endSession(session)

    if not result.success:
        logger.warning(f"Head Operator ticket verification failed for {responder.interaction.user.id}: {result.reason}")
        failedEmbed = discord.Embed(
            title="Verification Failed",
            description=f"Could not verify your Head Operator credential.\n\n**Reason:** `{result.reason}`\n\n"
                        f"Run the command again to retry.",
            color=discord.Color.red()
        )
        await message.edit(embed=failedEmbed, attachments=[])
        return False

    _issueTicket()
    logger.info(f"Head Operator ticket issued for {responder.interaction.user.id}, valid for "
                f"{TICKET_LIFETIME_SECONDS // 60} minutes.")

    confirmedEmbed = discord.Embed(
        title="Verified",
        description=f"You're verified for the next {TICKET_LIFETIME_SECONDS // 60} minutes. Continuing...",
        color=discord.Color.green()
    )
    await message.edit(embed=confirmedEmbed, attachments=[])
    return True


async def _ensureOpHasCorrectPerms(
    responder: InteractionResponder,
    verifService: NexaVerifService,
    permissions: List[str],
) -> bool:
    """
    Defers via responder.defer(), verifies the caller through the web-auth
    flow, and returns True only if the verified operator holds every
    requested permission.
    """
    await responder.defer(ephemeral=True)

    if not permissions:
        return True

    try:
        session = await verifService.beginAuthentication(
            discordUserID=responder.interaction.user.id
        )
    except RuntimeError as e:
        await responder.send(str(e), ephemeral=True)
        return False

    embed = discord.Embed(
        title="Operator Verification Required",
        description=(
            "Your operator permissions need to be verified before you can continue. "
            "Open this link or scan the QR code below, then submit a valid "
            "operator authentication code to proceed.\n\n"
            f"**Link:** {session.tunnelUrl}"
        ),
        color=discord.Color.blurple()
    )
    embed.set_image(url="attachment://qr.png")
    embed.set_footer(text="This verification will time out automatically if left idle.")

    qrBytes = base64.b64decode(session.qrCodeDataUri.split(",", 1)[1])
    qrFile = discord.File(io.BytesIO(qrBytes), filename="qr.png")

    message = await responder.send(embed=embed, file=qrFile, ephemeral=True, wait=True)

    logger.info(f"Operator permission verification started by {responder.interaction.user.id}.")

    result = await verifService.endSession(session)

    if not result.success:
        logger.warning(f"Operator permission verification failed for {responder.interaction.user.id}: {result.reason}")
        failedEmbed = discord.Embed(
            title="Verification Failed",
            description=(
                f"Could not verify your operator credentials.\n\n**Reason:** `{result.reason}`\n\n"
                f"Run the command again to retry."
            ),
            color=discord.Color.red()
        )
        await message.edit(embed=failedEmbed, attachments=[])
        return False

    missingPermissions = [p for p in permissions if p not in result.capabilities]
    if missingPermissions:
        deniedEmbed = discord.Embed(
            title="Insufficient Permissions",
            description=f"Your verified operator key is missing the required permissions for this command.",
            color=discord.Color.orange()
        )
        await message.edit(embed=deniedEmbed, attachments=[])
        return False

    confirmedEmbed = discord.Embed(
        title="Verified",
        description="Your operator permissions are sufficient for this action.",
        color=discord.Color.green()
    )
    await message.edit(embed=confirmedEmbed, attachments=[])
    return True


def _resolve_instance_folder(bot, instance_name: str | None) -> Path:
    if instance_name:
        instance = bot.instance_manager.get_instance(instance_name)
        if instance is None:
            raise ValueError(f"Instance '{instance_name}' was not found.")
        return Path(instance.folder).expanduser().resolve()

    configured_name = bot.config.get("general.primaryInstance")
    if configured_name:
        instance = bot.instance_manager.get_instance(configured_name)
        if instance is not None:
            return Path(instance.folder).expanduser().resolve()

    primary_instance = bot.instance_manager.get_primary_instance()
    if primary_instance is not None:
        return Path(primary_instance.folder).expanduser().resolve()

    raise ValueError("No instance was specified and no primary instance is configured.")


class _HeadOperatorApprovalView(discord.ui.View):
    def __init__(self, timeout_seconds: int = 60):
        super().__init__(timeout=timeout_seconds)
        self.approved = asyncio.Event()
        self.denied = asyncio.Event()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: Interaction, _button: discord.ui.Button):
        self.approved.set()
        self.stop()
        await interaction.response.send_message("Approved.", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: Interaction, _button: discord.ui.Button):
        self.denied.set()
        self.stop()
        await interaction.response.send_message("Denied.", ephemeral=True)

class NotificationHelper:
    def __init__(self, bot: "NexaBot"): #type: ignore
        self.bot = bot

    async def dmHeadOperator(self, message: str) -> None:
        head_operator_id = self.bot.config.get("security.headOperator", 0)
        if not head_operator_id:
            logger.warning("Head operator ID is not configured.")
            return

        user = await self._fetch_user(head_operator_id)
        if user is None:
            logger.warning(f"Head operator user {head_operator_id} could not be resolved.")
            return

        await self._send_dm(user, message)

    async def dmAllOperators(self, message: str) -> None:
        print("Sending DM to all Operators")
        operator_ids = set(self.bot.config.get("security.serverOperators", []) or [])
        head_operator_id = self.bot.config.get("security.headOperator", 0)
        print(f"operator_ids: {operator_ids}")
        print(f"head_operator_id: {head_operator_id}")
        if head_operator_id:
            operator_ids.add(head_operator_id)

        for user_id in operator_ids:
            user = await self._fetch_user(user_id)
            if user is not None:
                print(f"sending a DM to {user}")
                await self._send_dm(user, message)

    async def _fetch_user(self, user_id: int) -> discord.User | None:
        try:
            return await self.bot.fetch_user(user_id)
        except discord.NotFound:
            return None
        except discord.HTTPException as exc:
            logger.warning(f"Failed to fetch user {user_id}: {exc}")
            return None

    async def _send_dm(self, user: discord.User, message: str) -> None:
        try:
            await user.send(message)
        except discord.Forbidden:
            logger.warning(f"Cannot DM user {user.id}; DMs are closed.")
        except discord.HTTPException as exc:
            logger.warning(f"Failed to DM user {user.id}: {exc}")

def _buildAuthService(config: NexaConfig, notificationService: "NotificationHelper") -> NexaAuthenticationService:
    """
    Constructs a fresh protectedDB + NexaAuthenticationService pair pointed at keys.nxdb.
    Instantiation is normalized to CWD, so this works identically wherever it's called from.

    config: the bot's NexaConfig instance (self.bot.config), passed through to
            NexaAuthenticationService as configClass.
    """
    db_key = os.environ.get("NEXABOT_PROTECTED_KEY")
    keySystem = protectedDB(
        dbPath=Path("databases") / "keys.nxdb",
        password=db_key,
        create_if_missing=True
    )
    return NexaAuthenticationService(protectedDB=keySystem, configClass=config, notifier=notificationService)

class OperatorCog(commands.Cog):
    """Discord commands for operators."""

    def __init__(self, bot: "NexaBot"):  # type: ignore
        self.bot = bot
        self.notifService = NotificationHelper(bot=self.bot)

        # Constructed once for the cog's lifetime, not per-command-invocation. 
        self.authService = _buildAuthService(self.bot.config, notificationService=self.notifService)
        self.verifService = NexaVerifService(self.authService)
        self.sftpService = NexaSftpService(authService=self.authService)

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

    async def _awaitSftpSessionTeardown(self, session, tgt, instance) -> None:
        try:
            outcome = await self.sftpService.endSession(session)
            logger.info(f"SFTP session for Discord user {session.discordUserID} ended: {outcome.reason}")
        except Exception as e:
            logger.error(f"Error while tearing down SFTP session for Discord user "
                         f"{session.discordUserID}: {e}")
        finally:
            nxbotCmdGeneral.deregisterOperation(session.discordUserID)
            tgt.locked = False
            logger.info(f"Instance '{instance}' lock automatically removed at SFTP server teardown")

    # ------------------------------------------------------------------
    # /keyman command group - Head Operator only
    # ------------------------------------------------------------------

    keyman_group = app_commands.Group(
        name="keyman",
        description="Manage authentication keys for Server Operators."
    )

    @keyman_group.command(name="issue", description="Issue a new operator key with the given capability.")
    @app_commands.describe(
        discord_user="The Server Operator to issue a key to.",
        capability="The capability to grant on this key."
    )
    @app_commands.choices(capability=[
        app_commands.Choice(name="File System Access", value="fsaccess"),
        app_commands.Choice(name="Modpack Installs", value="modpackInstalls"),
        app_commands.Choice(name="Instance Lifecycle Management", value="lockAndUnlockInstances"),
        app_commands.Choice(name="Remote Console", value="executeRCON"),
    ])
    async def issue(self, interaction: Interaction, discord_user: discord.User, capability: app_commands.Choice[str]):
        if not await self.bot.guard.evaluate(interaction, "keyman"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.keyman.issue.requireAuthentication", True):
            if not await _ensureHeadOperatorTicket(responder, self.verifService):
                return

        if not self.bot._is_server_operator(discord_user.id):
            await responder.send(
                f"{discord_user.mention} is not a Server Operator. Operator keys can only be "
                f"issued to users listed under `security.serverOperators`. Add them there first "
                f"if this was intentional.",
                ephemeral=True
            )
            return

        try:
            code = self.authService.issueOperatorKey(
                discordUserID=discord_user.id,
                capabilities=[capability.value]
            )
        except OperatorKeyAlreadyExistsError:
            await responder.send(
                f"{discord_user.mention} already has an active operator key. "
                f"Use `/keyman modify` to change their capabilities, `/keyman rotate` to reissue "
                f"their code, or `/keyman revoke` first if you want to start over.",
                ephemeral=True
            )
            return
        except InvalidCapabilityError as e:
            await responder.send(str(e), ephemeral=True)
            return

        logger.info(f"Operator key issued to Discord user {discord_user.id} "
                    f"by Head Operator {interaction.user.id}. Capability: {capability.value}")

        await responder.send(
            f"Key issued for {discord_user.mention} with capability `{capability.value}`.\n\n"
            f"**Code:** `{code}`\n\n"
            f"This code is shown only once and is not stored anywhere in plaintext. "
            f"Share it directly and securely with {discord_user.mention}, and instruct them "
            f"to save it somewhere safe. It cannot be recovered or displayed again. "
            f"If it's ever lost or suspected leaked, use `/keyman rotate` to issue a replacement.",
            ephemeral=True
        )

    @keyman_group.command(name="modify", description="Add or remove a single capability on an operator's existing key.")
    @app_commands.describe(
        discord_user="The Server Operator whose key you're modifying.",
        capability="The capability to add or remove.",
        action="Whether to add or remove the capability."
    )
    @app_commands.choices(
        capability=[
            app_commands.Choice(name="File System Access", value="fsaccess"),
            app_commands.Choice(name="Modpack Installs", value="modpackInstalls"),
            app_commands.Choice(name="Instance Lifecycle Management", value="lockAndUnlockInstances"),
            app_commands.Choice(name="Remote Console", value="executeRCON"),
        ],
        action=[
            app_commands.Choice(name="Add", value="add"),
            app_commands.Choice(name="Remove", value="remove"),
        ]
    )
    async def modify(
        self,
        interaction: Interaction,
        discord_user: discord.User,
        capability: app_commands.Choice[str],
        action: app_commands.Choice[str]
    ):
        if not await self.bot.guard.evaluate(interaction, "keyman"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.keyman.modify.requireAuthentication", True):
            if not await _ensureHeadOperatorTicket(responder, self.verifService):
                return

        if not self.bot._is_server_operator(discord_user.id):
            logger.warning(f"{discord_user.id} holds an operator key but is no longer listed as a "
                            f"Server Operator. Consider using /keyman revoke instead of modifying.")

        try:
            updatedCapabilities = self.authService.modifyOperatorKey(
                discordUserID=discord_user.id,
                capability=capability.value,
                action=action.value
            )
        except OperatorKeyNotFoundError:
            await responder.send(
                f"{discord_user.mention} does not have an active operator key. "
                f"Use `/keyman issue` first.",
                ephemeral=True
            )
            return
        except InvalidCapabilityError as e:
            await responder.send(str(e), ephemeral=True)
            return

        logger.info(f"Operator key for Discord user {discord_user.id} modified "
                    f"by Head Operator {interaction.user.id}: {action.value} {capability.value}")

        capabilitiesDisplay = ", ".join(f"`{c}`" for c in updatedCapabilities) if updatedCapabilities else "*(none)*"
        await responder.send(
            f"Updated capabilities for {discord_user.mention}: {capabilitiesDisplay}",
            ephemeral=True
        )

    @keyman_group.command(name="revoke", description="Revoke an operator's key entirely.")
    @app_commands.describe(discord_user="The Server Operator whose key you're revoking.")
    async def revoke(self, interaction: Interaction, discord_user: discord.User):
        if not await self.bot.guard.evaluate(interaction, "keyman"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.keyman.revoke.requireAuthentication", True):
            if not await _ensureHeadOperatorTicket(responder, self.verifService):
                return

        try:
            self.authService.revokeOperatorKey(discordUserID=discord_user.id)
        except OperatorKeyNotFoundError:
            await responder.send(
                f"{discord_user.mention} does not have an active operator key to revoke.",
                ephemeral=True
            )
            return

        logger.info(f"Operator key for Discord user {discord_user.id} revoked "
                    f"by Head Operator {interaction.user.id}.")

        wasStopped = await nxbotCmdGeneral.emergencyStop(discord_user.id)
        if wasStopped:
            logger.warning(f"Revocation of Discord user {discord_user.id}'s key also "
                            f"force-stopped an active operation in progress under that "
                            f"credential.")

        stoppedNote = (
            "\n\nThey also had an active session in progress, which has been immediately terminated."
            if wasStopped else ""
        )
        await responder.send(
            f"Revoked the operator key for {discord_user.mention}.{stoppedNote}",
            ephemeral=True
        )

    @keyman_group.command(name="rotate", description="Revoke and reissue an operator's key, carrying over their capabilities.")
    @app_commands.describe(discord_user="The Server Operator whose key you're rotating.")
    async def rotate(self, interaction: Interaction, discord_user: discord.User):
        if not await self.bot.guard.evaluate(interaction, "keyman"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.keyman.rotate.requireAuthentication", True):
            if not await _ensureHeadOperatorTicket(responder, self.verifService):
                return

        if not self.bot._is_server_operator(discord_user.id):
            await responder.send(
                f"{discord_user.mention} is not a Server Operator. If they no longer should have "
                f"access, use `/keyman revoke` instead. If this is unexpected, confirm they're "
                f"listed under `security.serverOperators`.",
                ephemeral=True
            )
            return

        try:
            newCode = self.authService.rotateOperatorKey(discordUserID=discord_user.id)
        except OperatorKeyNotFoundError:
            await responder.send(
                f"{discord_user.mention} does not have an active operator key to rotate.",
                ephemeral=True
            )
            return

        logger.info(f"Operator key for Discord user {discord_user.id} rotated "
                    f"by Head Operator {interaction.user.id}.")

        await responder.send(
            f"Key rotated for {discord_user.mention}. Their previous code is no longer valid.\n\n"
            f"**New Code:** `{newCode}`\n\n"
            f"This code is shown only once and is not stored anywhere in plaintext. "
            f"Share it directly and securely with {discord_user.mention}, and instruct them "
            f"to save it somewhere safe - it cannot be recovered or displayed again.",
            ephemeral=True
        )

    @keyman_group.command(name="list", description="List active operator keys and their capabilities.")
    @app_commands.describe(discord_user="Optional: filter to a specific Server Operator.")
    async def list_keys(self, interaction: Interaction, discord_user: discord.User = None):
        if not await self.bot.guard.evaluate(interaction, "keyman"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.keyman.list.requireAuthentication", True):
            if not await _ensureHeadOperatorTicket(responder, self.verifService):
                return

        targetID = discord_user.id if discord_user else None
        entries = self.authService.listOperatorKeys(discordUserID=targetID)

        if not entries:
            message = (
                f"{discord_user.mention} has no active operator key."
                if discord_user else
                "There are no active operator keys."
            )
            await responder.send(message, ephemeral=True)
            return

        menu = SimpleMenu(interaction.user)

        if len(entries) == 1 or discord_user is not None:
            for entry in entries:
                capabilitiesDisplay = ", ".join(f"`{c}`" for c in entry["capabilities"]) if entry["capabilities"] else "*(none)*"
                menu.add_page(
                    title=f"Operator Key: <@{entry['discordUserID']}>",
                    description=(
                        f"**Discord User:** <@{entry['discordUserID']}>\n"
                        f"**Capabilities:** {capabilitiesDisplay}\n"
                        f"**Issued On:** {entry['issuedOn']}"
                    )
                )
        else:
            lines = []
            for entry in entries:
                capabilitiesDisplay = ", ".join(entry["capabilities"]) if entry["capabilities"] else "(none)"
                lines.append(f"<@{entry['discordUserID']}> - {capabilitiesDisplay}")
            menu.add_page(
                title="Active Operator Keys",
                description="\n".join(lines)
            )

        # already_deferred now reflects reality instead of being hardcoded --
        # if requireAuthentication was false, this interaction was never
        # deferred, and SimpleMenu needs to know that to respond correctly.
        await menu.send(interaction, already_deferred=responder.deferred)


    # Filesystem Access Command
    @app_commands.command(name="fsaccess", description="Grants Filesystem Access to an instance")
    @app_commands.describe(instance="The instance to open filesystem access to.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def filesystem_access(self, interaction: Interaction, instance: str | None = None):
        if not await self.bot.guard.evaluate(interaction, "fsaccess"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.fsaccess.requireAuthentication", True):
            if not await _ensureOpHasCorrectPerms(responder, self.verifService, ["fsaccess"]):
                return

        # Check instance, lock it, whole nine yards
        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await responder.send(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if tgt.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            await responder.send(
                f"`{instance}` is currently {tgt.status.value} and cannot have its files exposed to.",
                ephemeral=True
            )
            return

        tgt.locked = True
        logger.info(f"Instance '{instance}' locked by {interaction.user} ({interaction.user.id}) in prep for file access.")

        try:
            jail_root = _resolve_instance_folder(self.bot, instance)
        except ValueError as exc:
            logger.error(f"An error occured while setting up the SFTP Connection: {str(exc)}")
            await responder.send("An error occurred.", ephemeral=True)
            tgt.locked = False
            logger.info(f"Instance '{instance}' unlocked automatically by Nexa due to jail root resolution failure.")
            return

        require_approval = self.bot.cmdConfig.get("commands.fsaccess.askHeadOperatorForApproval", True)
        if require_approval:
            head_operator_id = self.bot.config.get("security.headOperator", 0)
            if head_operator_id:
                head_user = await self.bot.fetch_user(head_operator_id)
                if head_user is None:
                    await responder.send("The configured Head Operator could not be resolved.", ephemeral=True)
                    tgt.locked = False
                    logger.info(f"Instance '{instance}' unlocked automatically by Nexa due to head operator resolution failure.")
                    return

                APPROVAL_TIMEOUT_SECONDS = 300  # 5 mins

                view = _HeadOperatorApprovalView(timeout_seconds=APPROVAL_TIMEOUT_SECONDS)
                await head_user.send(
                    f"Operator <@{interaction.user.id}> requested temporary filesystem access to '{jail_root.name}'.",
                    view=view,
                )

                await responder.send(
                    embed=discord.Embed(
                        title="Awaiting Head Operator Approval",
                        description=(
                            "Your filesystem access request is currently being presented "
                            "to the Head Operator for approval. This page will not update "
                            "automatically. You'll receive a new message once a decision "
                            "is made."
                        ),
                        color=discord.Color.blurple()
                    ),
                    ephemeral=True
                )

                approvedTask = asyncio.create_task(view.approved.wait())
                deniedTask = asyncio.create_task(view.denied.wait())
                try:
                    done, pending = await asyncio.wait(
                        {approvedTask, deniedTask},
                        timeout=APPROVAL_TIMEOUT_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in (approvedTask, deniedTask):
                        if not task.done():
                            task.cancel()

                if not done:
                    # responder.send() is guaranteed at least deferred by
                    # this point (the "Awaiting Approval" responder.send()
                    # call above always fires when require_approval is True),
                    # but using responder.send() rather than a raw
                    # interaction.followup.send() keeps this correct even if
                    # that assumption ever changes upstream.
                    await responder.send("Head Operator approval timed out.", ephemeral=True)
                    tgt.locked = False
                    logger.info(f"Instance '{instance}' unlocked automatically by Nexa due to fsaccess command failure.")
                    return

                if view.denied.is_set():
                    await responder.send("Head Operator denied the request.", ephemeral=True)
                    tgt.locked = False
                    logger.info(f"Instance '{instance}' unlocked automatically by Nexa due to fsaccess command failure.")
                    return

        try:
            session = await self.sftpService.beginSession(str(jail_root), interaction.user.id)
        except RuntimeError as exc:
            # This is the actual bug: with requireAuthentication AND
            # askHeadOperatorForApproval both False, interaction.response
            # was never consumed by anything above -- a raw followup.send()
            # here fails silently client-side ("The application did not
            # respond"), because Discord never received an initial response.
            await responder.send(str(exc), ephemeral=True)
            tgt.locked = False
            logger.info(f"Instance '{instance}' unlocked automatically by Nexa due to an unexpected Runtime Error.")
            return

        nxbotCmdGeneral.registerOperation(
            discordUserID=interaction.user.id,
            kind="sftp",
            forceStop=session.closeExplicitly,
            label=jail_root.name,
        )

        asyncio.create_task(self._awaitSftpSessionTeardown(session, tgt=tgt, instance=instance))

        key_bytes = session.privateKeyPem.encode("utf-8") if session.privateKeyPem else b""
        await interaction.user.send(
            embed=discord.Embed(
                title="SFTP Access Ready",
                description=(
                    f"Temporary access is ready for the instance folder `{jail_root}`.\n\n"
                    f"Host: `{session.tunnelHost}`\n"
                    f"Port: `{session.tunnelPort}`\n"
                    f"Username: `nexa`\n"
                    f"Timeout: `{SESSION_TIMEOUT_SECONDS // 60} minutes`"
                ),
                color=discord.Color.green(),
            ),
            file=discord.File(io.BytesIO(key_bytes), filename="nexa_sftp_key.pem"),
            delete_after=90.0
        )

        # Same bug, same fix: this is the exact line you caught in testing.
        # With both toggles off, this was the first and only response
        # attempt on an interaction whose .response had never been touched.
        await responder.send(
            "Your temporary SFTP access details were sent to your DMs. Access this and download your data immediately. The details will be deleted in 90 seconds.",
            ephemeral=True,
        )

    @app_commands.command(
        name="install_mpck",
        description="Install a .mrpack modpack to an instance."
    )
    @app_commands.describe(
        url="Direct URL to the .mrpack file.",
        instance="The instance to install the modpack to."
    )
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def install_mpck(self, interaction: Interaction, url: str, instance: str):
        if not await self.bot.guard.evaluate(interaction, "install_mcpk"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.install_mcpk.requireAuthentication", True):
            if not await _ensureOpHasCorrectPerms(responder, self.verifService, ["modpackInstalls"]):
                return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await responder.send(f"❌ Instance `{instance}` not found.", ephemeral=True)
            return
        if getattr(tgt, "locked", False):
            await responder.send(f"❌ Instance `{instance}` is already locked.", ephemeral=True)
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

        await responder.send(f"🚀 Starting modpack installation for `{instance}`…", ephemeral=True)

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
                    delay_seconds=self.bot.cmdConfig.get("commands.install_mcpk.shutdownWaitPeriodInMins", 15) * 60,
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
                        # This is a separate, fresh interaction from the
                        # button click -- not the original command interaction
                        # -- so it correctly uses response.send_message()
                        # directly rather than going through responder.
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
                            f"{tgt.players} player(s) online. Server shutting down in {self.bot.cmdConfig.get('commands.install_mcpk.shutdownWaitPeriodInMins', 15)} minute(s).",
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

    @app_commands.command(name="execute", description="Execute a raw RCON command on an instance.")
    @app_commands.describe(instance="The instance to run the command on.", command="The RCON command to execute.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def execute(self, interaction: Interaction, instance: str, command: str):
        if not await self.bot.guard.evaluate(interaction, "execute"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.execute.requireAuthentication", True):
            if not await _ensureOpHasCorrectPerms(responder, self.verifService, ["executeRCON"]):
                return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await responder.send(f"Instance `{instance}` not found.", ephemeral=True)
            return

        cleanedCmd = command.lstrip("/")
        if not cleanedCmd.strip():
            await responder.send("Command is empty. Command cannot be empty.", ephemeral=True)
            return

        protected_cmds = tgt.get_protected_commands() or []
        if cleanedCmd.split()[0] in protected_cmds:
            await responder.send(
                f"Command `{cleanedCmd.split()[0]}` is protected and cannot be executed through this interface.",
                ephemeral=True
            )
            return

        try:
            response = await tgt.executeCommand(command)
            embed = discord.Embed(
                title=f"RCON Response: {tgt.name}",
                description=f"**Command:** `{command}`\n**Response:**\n```{response}```",
                color=0x5865F2
            )
            await responder.send(embed=embed, ephemeral=True)
        except RuntimeError as e:
            await responder.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="lock_instance", description="Prevent an instance from being started.")
    @app_commands.describe(instance="The instance to lock.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def lock_instance(self, interaction: Interaction, instance: str):
        if not await self.bot.guard.evaluate(interaction, "lock_instance"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.lock_instance.requireAuthentication", True):
            if not await _ensureOpHasCorrectPerms(responder, self.verifService, ["lockAndUnlockInstances"]):
                return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await responder.send(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if tgt.status in (ServerStatus.ONLINE, ServerStatus.STARTING):
            await responder.send(
                f"`{instance}` is currently {tgt.status.value} and cannot be locked. Ensure the instance is offline before locking.",
                ephemeral=True
            )
            return
        if getattr(tgt, "locked", False):
            await responder.send(f"`{instance}` is already locked.", ephemeral=True)
            return

        tgt.locked = True
        logger.info(f"Instance '{instance}' locked by {interaction.user} ({interaction.user.id}).")
        await responder.send(f"`{instance}` is now locked. It cannot be started until unlocked.", ephemeral=True)

    @app_commands.command(name="unlock_instance", description="Allow a locked instance to be started again.")
    @app_commands.describe(instance="The instance to unlock.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def unlock_instance(self, interaction: Interaction, instance: str):
        if not await self.bot.guard.evaluate(interaction, "unlock_instance"):
            return

        responder = InteractionResponder(interaction)
        if self.bot.cmdConfig.get("commands.unlock_instance.requireAuthentication", True):
            if not await _ensureOpHasCorrectPerms(responder, self.verifService, ["lockAndUnlockInstances"]):
                return

        tgt = self.bot.instance_manager.get_instance(instance)
        if not tgt:
            await responder.send(f"Instance `{instance}` not found.", ephemeral=True)
            return
        if not getattr(tgt, "locked", False):
            await responder.send(f"`{instance}` is not locked.", ephemeral=True)
            return

        tgt.locked = False
        logger.info(f"Instance '{instance}' unlocked by {interaction.user} ({interaction.user.id}).")
        await responder.send(f"`{instance}` is now unlocked.", ephemeral=True)

    @app_commands.command(name="force_stop", description="Force stop an instance immediately.")
    @app_commands.describe(instance="The instance to force stop.")
    @app_commands.autocomplete(instance=_instance_autocomplete)
    async def force_stop(self, interaction: Interaction, instance: str):
        if not await self.bot.guard.evaluate(interaction, "force_stop"):
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

async def setup(bot: "NexaBot"):  # type: ignore
    await bot.add_cog(OperatorCog(bot))