# instanceManager.py
# Under the MIT License.

from typing import Dict, Optional
from pathlib import Path
from enum import Enum
import subprocess
from backend.configLib import parseConfig, getConfigVal, parseServerProperties
from services.nexaConfig import NexaInstanceConfig
from services import nexaLoggerFactory
from mcrcon import MCRcon
import asyncio
import sys
import re
import zipfile
from datetime import datetime, timedelta

class ServerStatus(str, Enum):
    OFFLINE = "offline"
    STARTING = "starting"
    ONLINE = "online"
    SLEEPING = "sleeping"


logger = nexaLoggerFactory.get_logger("InstanceManager")

class ServerInstance:
    def __init__(self, name: str, folder: str, version: str = "Unknown", loader: str = "Unknown", icon_url: Optional[str] = None):
        self.name = name
        self.folder = Path(folder)
        self.version = version
        self.loader = loader
        self.icon_url = icon_url
 
        self.status = ServerStatus.OFFLINE
        self.players = 0

        self._shutdown_task: Optional[asyncio.Task] = None
 
        # Load instance config via NexaInstanceConfig
        self.config = NexaInstanceConfig(self.folder)
 
        self.server_props = self._load_server_properties()
 
        self.join_to_wake = self.config.get("functionality.join_to_wake", False)
 
        self.rcon_enabled = self._get_bool("enable-rcon")
        self.rconPass = self.server_props.get("rcon.password")
        self.rcon_port = int(self.server_props.get("rcon.port"))
        self.max_players = int(self.server_props.get("max-players"))
        self.startCmd = self.config.get("functionality.startCmd")
 
        # Idle instance folder
        self.idle_folder = self.folder / "nexaIdleInstance"
 
        # Process tracking
        self.active_process: Optional[subprocess.Popen] = None
        self.idle_process: Optional[subprocess.Popen] = None
 
        # Auto shutdown state
        self.auto_shutdown_enabled: bool = self.config.get("functionality.auto_shutdown.enabled", False)
        self.auto_shutdown_idle_minutes: int = self.config.get("functionality.auto_shutdown.idle_minutes", 5)
        self._idle_seconds: float = 0.0
 
        # Backup state
        self.backup_enabled: bool = self.config.get("functionality.autosave.enabled", True)
        self.backup_interval_days: int = self.config.get("functionality.autosave.interval_days", 3)
 
        # Watchdog state
        self.watchdog_enabled: bool = self.config.get("functionality.watchdog.enabled", True)
        self.watchdog_interval: int = self.config.get("functionality.watchdog.interval_seconds", 60)
        self.watchdog_restart_limit: int = self.config.get("functionality.watchdog.restart_limit", 3)
        self._watchdog_restart_count: int = 0
        self._stopping: bool = False
 
    def _load_server_properties(self) -> dict:
        path = self.folder / "server.properties"
        if not path.exists():
            raise FileNotFoundError(f"{path} missing")
        return parseServerProperties(path)
 
    def _get_bool(self, key: str, default=False) -> bool:
        val = self.server_props.get(key)
        if val is None:
            return default
        return val.lower() == "true"
 
    def _get_server_players(self):
        try:
            with MCRcon("127.0.0.1", self.rconPass, port=self.rcon_port) as mcr:
                response = mcr.command("/list")
                #print(f"Raw Response: {response}")
 
                count_match = re.search(r"There are (\d+) of a max", response)
                player_count = int(count_match.group(1)) if count_match else 0
 
                names = ""
                if ":" in response:
                    names = response.split(":", 1)[1].strip()
 
                return player_count, names
        except Exception as e:
            return 0, ""
 
    async def refresh_players(self):
        """Run the blocking _get_server_players in a thread and update self.players."""
        try:
            loop = asyncio.get_running_loop()
            count, names = await loop.run_in_executor(None, self._get_server_players)
        except RuntimeError:
            count, names = self._get_server_players()
 
        try:
            self.players = int(count)
        except Exception:
            self.players = 0
 
        #print(f"self.players: {self.players}")
        return self.players, names
 
    def update_status(self, status: ServerStatus):
        self.status = status
        try:
            self.players = int(self.players or 0)
        except Exception:
            self.players = 0

    def get_protected_commands(self):
        """Returns a list of protected commands as defined in the instance's config"""
        return self.config.get("security.protected_commands.commands")
 
    def executeCommand(self, command: str) -> str:
        """
        Sends a raw RCON command to this instance and returns the response string.
        Raises RuntimeError if the instance is not online or RCON fails.
        """
        if self.status != ServerStatus.ONLINE:
            raise RuntimeError(f"Instance '{self.name}' is not online.")
        try:
            with MCRcon("127.0.0.1", self.rconPass, port=self.rcon_port) as mcr:
                return mcr.command(command) or "(no response)"
        except Exception as e:
            raise RuntimeError(f"RCON command failed for '{self.name}': {e}")
 

class InstanceManager:
    def __init__(self):
        self.instances: Dict[str, ServerInstance] = {}
        self._shutdown_task: Optional[asyncio.Task] = None

    async def start(self):
        """Call once the event loop is running to begin background tasks."""
        asyncio.create_task(self._status_loop())
        asyncio.create_task(self._backup_loop())

    def add_instance(self, instance: ServerInstance):
        self.instances[instance.name] = instance

    def get_instance(self, name: str) -> Optional[ServerInstance]:
        return self.instances.get(name)

    def get_primary_instance(self) -> Optional[ServerInstance]:
        return next(iter(self.instances.values()), None)

    async def start_instance(self, name: str):
        instance = self.get_instance(name)
        if not instance:
            raise ValueError(f"No instance named {name}")

        instance.update_status(ServerStatus.STARTING)

        proc = subprocess.Popen(
            instance.startCmd,
            cwd=str(instance.folder),
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        # A different one with console output on
        #proc = subprocess.Popen(
        #    instance.startCmd,
        #    cwd=str(instance.folder),
        #    shell=True,
        #    stdout=subprocess.PIPE,
        #    stderr=subprocess.STDOUT,
        #    text=True
        #)

        instance.active_process = proc
        print(f"[InstanceManager] Launched {name} with PID {proc.pid}")

        await self._wait_for_rcon(instance)

        instance.update_status(ServerStatus.ONLINE)
        print(f"[InstanceManager] {name} is ONLINE")

    async def stop_instance(self, name: str, update_embed_callback=None, hard: bool = False):
        """Stops the active server instance, optionally starts idle monitoring if join_to_wake=True"""
        instance = self.get_instance(name)
        if not instance:
            raise ValueError(f"No instance named {name}")

        if instance.status in (ServerStatus.OFFLINE, ServerStatus.SLEEPING):
            return
        
        instance._stopping = True

        if update_embed_callback:
            await update_embed_callback(instance)

        # Attempt graceful shutdown via RCON
        if instance.rconPass and instance.active_process:
            try:
                with MCRcon("localhost", instance.rconPass, port=instance.rcon_port) as rcon:
                    rcon.command("stop")
            except Exception as e:
                print(f"[InstanceManager] RCON stop failed: {e}")

        # Wait for process exit
        if instance.active_process:
            for _ in range(10):
                if instance.active_process.poll() is not None:
                    break
                await asyncio.sleep(2)

            # Force kill if still alive
            if instance.active_process.poll() is None:
                instance.active_process.terminate()
                try:
                    instance.active_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    instance.active_process.kill()
                    print(f"[InstanceManager] WARNING: {instance.name} was forcibly killed. World data may be corrupted.", file=sys.stderr, flush=True)
            instance.active_process = None

        # Start idle monitor if join_to_wake is enabled
        if instance.join_to_wake and not hard:
            instance.update_status(ServerStatus.SLEEPING)
            if update_embed_callback:
                await update_embed_callback(instance)
            asyncio.create_task(self._idle_monitor(instance))
        else:
            instance.update_status(ServerStatus.OFFLINE)
            if update_embed_callback:
                await update_embed_callback(instance)

    async def _wait_for_rcon(self, instance: ServerInstance, timeout: int = 300):
        """Blocks until RCON responds or timeout is reached."""
        elapsed = 0
        while elapsed < timeout:
            try:
                with MCRcon("localhost", instance.rconPass, port=instance.rcon_port) as rcon:
                    response = rcon.command("list")
                    if response is not None:
                        return
            except Exception:
                await asyncio.sleep(2)
                elapsed += 2

        # Timeout reached. Server failed to start
        
        instance.update_status(ServerStatus.OFFLINE)
        if instance.active_process and instance.active_process.poll() is None:
            instance.active_process.terminate()
        instance.active_process = None
        print(f"[InstanceManager] Critical failure for {instance.name}: failed to start within {timeout} seconds.", file=sys.stderr, flush=True)
        sys.exit(1)

    async def _idle_monitor(self, instance: ServerInstance):
        """Monitors join attempts on idle instance and starts active server on join."""
        if not instance.join_to_wake or not instance.idle_folder.exists():
            return

        kick_msg = (
            f"NEXABOT\n\n-----------------\n\n"
            f"You joined ({instance.name}) while in idle mode. "
            f"The server is waking up. Please wait and rejoin."
        )

        while instance.status == ServerStatus.SLEEPING:
            # Start idle server if not running
            if not instance.idle_process or instance.idle_process.poll() is not None:
                instance.idle_process = subprocess.Popen(
                    instance.startCmd, cwd=str(instance.idle_folder), shell=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                print(f"[InstanceManager] Idle instance started for {instance.name}")

            # Check players via RCON
            try:
                with MCRcon("localhost", instance.rconPass, port=instance.rcon_port) as rcon:
                    response = rcon.command("list") or ""
                    if ":" in response:
                        players_part = response.split(":", 1)[1].strip()
                        players = [n.strip() for n in players_part.split(",") if n.strip()]
                        if players:
                            for p in players:
                                rcon.command(f"kick {p} {kick_msg}")
                                print(f"[InstanceManager] Kicked {p} from idle instance")

                            rcon.command("stop")

                            if instance.idle_process:
                                instance.idle_process.terminate()
                                try:
                                    instance.idle_process.wait(timeout=5)
                                except subprocess.TimeoutExpired:
                                    instance.idle_process.kill()
                                instance.idle_process = None
                                print(f"[InstanceManager] Idle instance stopped for {instance.name}")

                            instance.update_status(ServerStatus.STARTING)
                            await self.start_instance(instance.name)
                            return
            except Exception as e:
                print(f"[InstanceManager] Idle monitor RCON error for {instance.name}: {e}")

            await asyncio.sleep(0.5)

    async def backup_instance(self, name: str) -> bool:
        """
        Zips the world folder of the named instance into
        <instance_folder>/worldBackups/YYYY-MM-DD.zip.
        Returns True on success, False on failure.
        Skips if the server is currently online to avoid zipping a live world.
        """
        instance = self.get_instance(name)
        if not instance:
            logger.error(f"Instance not found: {name}")
            print(f"[Backup] No instance named {name}.", file=sys.stderr, flush=True)
            return False

        if instance.status == ServerStatus.ONLINE:
            logger.warning(f"{name} is online. Skipping backup to avoid zipping a live world.")
            print(f"[Backup] {name} is online. Skipping backup to avoid zipping a live world.", flush=True)
            return False

        world_folder = instance.folder / "world"
        if not world_folder.exists():
            print(f"[Backup] World folder not found for {name} at {world_folder}.", file=sys.stderr, flush=True)
            return False

        backup_dir = instance.folder / "worldBackups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")
        backup_path = backup_dir / f"{date_str}.zip"

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._zip_world, world_folder, backup_path)
            #print(f"[Backup] {name} backed up to {backup_path}", flush=True)
            logger.info(f"{name} backed up to {backup_path}")
            return True
        except Exception as e:
            #print(f"[Backup] Failed to back up {name}: {e}", file=sys.stderr, flush=True)
            logger.error(f"Failed to back up {name}: {e}")
            return False

    def _zip_world(self, world_folder: Path, backup_path: Path):
        """Blocking zip operation. Run in executor to avoid blocking the event loop."""
        with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in world_folder.rglob("*"):
                if file.is_file():
                    zf.write(file, file.relative_to(world_folder.parent))

    async def _backup_loop(self):
        """Periodic backup loop. Fires a backup for each instance based on its configured interval."""
        # Track last backup time per instance
        last_backup: dict[str, datetime] = {}

        while True:
            now = datetime.now()
            for inst in list(self.instances.values()):
                if not inst.backup_enabled:
                    continue

                last = last_backup.get(inst.name)
                due = last is None or (now - last) >= timedelta(days=inst.backup_interval_days)

                if due:
                    #print(f"[Backup] Scheduled backup starting for {inst.name}.", flush=True)
                    logger.info(f"Scheduled backup starting for {inst.name}.")
                    success = await self.backup_instance(inst.name)
                    if success:
                        last_backup[inst.name] = now

            # Check once per hour. No need to poll more frequently for day-scale intervals
            await asyncio.sleep(3600)

    async def _status_loop(self, interval: float = 10.0):
        """Background loop that periodically refreshes status/players for all instances.
        Also runs watchdog logic. If a server process dies unexpectedly, attempts restart
        up to the configured restart_limit.
        """
        while True:
            #print("[InstanceManager] Checking instances...")
            for inst in list(self.instances.values()):
                was_online = inst.status == ServerStatus.ONLINE

                if inst.active_process and inst.active_process.poll() is None:
                    # Process is alive
                    inst.status = ServerStatus.ONLINE
                    inst._watchdog_restart_count = 0  # reset counter on healthy tick
                elif inst.idle_process and inst.idle_process.poll() is None:
                    inst.status = ServerStatus.SLEEPING
                else:
                    # No alive process
                    if was_online and inst.active_process is not None:
                        inst.active_process = None

                        if inst._stopping:
                            inst._stopping = False
                            inst.update_status(ServerStatus.OFFLINE)
                        elif inst.watchdog_enabled:
                            if inst._watchdog_restart_count < inst.watchdog_restart_limit:
                                inst._watchdog_restart_count += 1
                                print(f"[Watchdog] {inst.name} crashed. Restart attempt {inst._watchdog_restart_count}/{inst.watchdog_restart_limit}.")
                                asyncio.create_task(self.start_instance(inst.name))
                            else:
                                print(f"[Watchdog] {inst.name} has crashed {inst.watchdog_restart_limit} times. Giving up.", file=sys.stderr)
                                inst.update_status(ServerStatus.OFFLINE)
                        else:
                            inst.update_status(ServerStatus.OFFLINE)
                    else:
                        inst.status = ServerStatus.OFFLINE

                if inst.status in (ServerStatus.ONLINE, ServerStatus.SLEEPING):
                    await inst.refresh_players()

                # Auto shutdown check
                if inst.status == ServerStatus.ONLINE and inst.auto_shutdown_enabled:
                    if inst.players == 0:
                        inst._idle_seconds += interval
                        if inst._idle_seconds >= inst.auto_shutdown_idle_minutes * 60:
                            print(
                                f"[AutoShutdown] {inst.name} has been empty for "
                                f"{inst.auto_shutdown_idle_minutes} minute(s). Shutting down.",
                                flush=True
                            )
                            inst._idle_seconds = 0.0
                            asyncio.create_task(self.stop_instance(inst.name))
                    else:
                        inst._idle_seconds = 0.0
            await asyncio.sleep(interval)

    async def schedule_shutdown(
        self,
        name: str,
        delay_seconds: int,
        reason: str = "Server shutting down.",
        update_embed_callback=None,
        hard: bool = False
    ):
        """
        Schedule a graceful shutdown for an instance after a delay.
        Broadcasts warnings to players at 10min, 5min, 1min, and 30s marks if time allows.
        Cancels any existing scheduled shutdown for the same instance first.
        """
        instance = self.get_instance(name)
        if not instance:
            logger.error(f"Instance not found: {name}")
            raise ValueError(f"No instance named {name}")

        # Cancel any existing scheduled shutdown
        if instance._shutdown_task and not instance._shutdown_task.done():
            instance._shutdown_task.cancel()
            logger.info(f"Scheduled shutdown for {name} cancelled.")

        async def _run():
            warnings = [
                (600, "Server shutting down in 10 minutes."),
                (300, "Server shutting down in 5 minutes."),
                (60,  "Server shutting down in 1 minute."),
                (30,  "Server shutting down in 30 seconds."),
            ]

            # Only include warnings that fall within our delay window
            pending = [(t, msg) for t, msg in warnings if t < delay_seconds]

            elapsed = 0
            for warn_threshold, warn_msg in sorted(pending, reverse=True):
                wait = delay_seconds - elapsed - warn_threshold
                if wait > 0:
                    await asyncio.sleep(wait)
                    elapsed += wait
                try:
                    full_msg = f"{warn_msg} Reason: {reason}"
                    instance.executeCommand(f"say {full_msg}")
                    print(f"[ScheduledShutdown] [{name}] Broadcast: {full_msg}")
                except Exception as e:
                    print(f"[ScheduledShutdown] Failed to broadcast warning to {name}: {e}")

            # Sleep remaining time to shutdown
            remaining = delay_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(remaining)

            print(f"[ScheduledShutdown] Shutting down {name} now.")
            await self.stop_instance(name, update_embed_callback=update_embed_callback, hard=hard)
            instance._shutdown_task = None

        instance._shutdown_task = asyncio.create_task(_run())
        print(f"[ScheduledShutdown] {name} scheduled for shutdown in {delay_seconds}s. Reason: {reason}")


    def cancel_shutdown(self, name: str) -> bool:
        """
        Cancel a pending scheduled shutdown. Returns True if one was cancelled, False if none existed.
        """
        instance = self.get_instance(name)
        if not instance:
            raise ValueError(f"No instance named {name}")

        if instance._shutdown_task and not instance._shutdown_task.done():
            instance._shutdown_task.cancel()
            instance._shutdown_task = None
            #print(f"[ScheduledShutdown] Scheduled shutdown for {name} cancelled.")
            logger.info(f"Scheduled shutdown for {name} cancelled.")
            return True
        return False