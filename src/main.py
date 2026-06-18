# main.py
# Under the MIT License.
# PLEASE don't modify unless strcitly necessary!

import psutil
import os
import requests
import subprocess
import sys
from pathlib import Path
from bot.discordBot import NexaBot
from backend.instanceManager import InstanceManager, ServerInstance, ServerStatus
from services.nexaDB import unprotectedDB, protectedDB
from services import nexaLoggerFactory
from ensureStability import checkIfAbleToRun
import argparse

# Capture the real executable path before anything else runs.
# sys.argv[0] always points to the original .exe or script, never the temp extraction dir.
SELF_PATH = os.path.abspath(sys.argv[0])

# Load Config
from services.nexaConfig import NexaConfig, NexaInstanceRegistry

config = NexaConfig("NexaBotConfig.yaml")
registry = NexaInstanceRegistry("NexaInstanceRegistry.yaml")

currentNexaVersion = "0.2.2" # This should be updated with every release. Please do not touch it if a release is not being made.
whereIsThatSillyUpdateIndex = "https://raw.githubusercontent.com/StormCode-dev/Nexa/refs/heads/main/updateIndex.json" # This should point to a raw JSON file in the repo with the latest version info. 
# Please do not touch it. It points to the main branch, which is the correct branch.

def check_for_updates():
    try:
        response = requests.get(whereIsThatSillyUpdateIndex, timeout=5)
        response.raise_for_status()
        data = response.json()
        latest = data["latestNexaVersion"]

        if currentNexaVersion != latest:
            print(f"[Nexa] Update available: {currentNexaVersion} → {latest}")
            return 1
        else:
            print(f"[Nexa] Up to date ({currentNexaVersion})")
            return 0

    except requests.exceptions.RequestException as e:
        print(f"[Nexa] Update check failed: {e}")
        return -1
    except (KeyError, ValueError) as e:
        print(f"[Nexa] Malformed update index: {e}")
        return -1
    
def is_playit_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            if "playit" in proc.info["name"].lower():
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False

def build_folder_for_instance(instances_root: Path, instance_name: str, instance_cfg: dict) -> str:
    """
    Determine the folder path for an instance.
    Priority:
      1. explicit 'folder' in instance_cfg
      2. instances_root / instance_name
    Returns a string (matching how your mock used an explicit Windows path).
    """
    if not instance_cfg:
        return str(instances_root / instance_name)
    folder = instance_cfg.get("folder")
    if folder:
        return str(Path(folder))
    return str(instances_root / instance_name)

def load_overrides():
    """
    Loads overrides from a file called "nxoverrides.dat" in the cwd. Only allows overriding on env vars "BOT_TOKEN" and "NEXABOT_PROTECTED_KEY" for now.

    The file should have lines in the format:
    ENV_VAR_NAME=VALUE
    """
    print("Attempting to load overrides from nxoverrides.dat...")
    try:
        with open("nxoverrides.dat", "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    key, value = line.split("=", 1)
                    if key in ["BOT_TOKEN", "NEXABOT_PROTECTED_KEY"]:
                        print(f"Overriding {key} from nxoverrides.dat")
                        os.environ[key] = value
    except FileNotFoundError:
        print("No nxoverrides.dat file found, skipping overrides.")

def main():
    # Early safety check & setup
    checkIfAbleToRun(config)

    parser = argparse.ArgumentParser()
    parser.add_argument("--isSpawnedProc", action="store_true", help="Run as a spawned process (internal use only)")
    parser.add_argument("--resurrected", action="store_true", help="Indicates this process was restarted after a crash (internal use only)")
    args = parser.parse_args()

    nexaLoggerFactory.setup(config, is_daemon=args.isSpawnedProc)
    logger = nexaLoggerFactory.get_logger("Main")

    def kill_orphaned_java():
        for proc in psutil.process_iter(["name", "pid"]):
            try:
                if any(jvm in proc.info["name"].lower() for jvm in ("java", "javaw", "openjdk")):
                    logger.info(f"Killing orphaned JVM process: {proc.info['name']} (PID {proc.info['pid']})")
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"Could not kill JVM process (PID {proc.info['pid']}): {e}")

    if config.get("serverHealthManagement", {}).get("keepNexaAlive", False) and not args.isSpawnedProc:
        logger.info("Starting Nexa in watchdog mode...")
        first_spawn = True
        while True:
            if SELF_PATH.endswith(".exe"):
                cmd = [SELF_PATH, "--isSpawnedProc"]
            else:
                cmd = [sys.executable, SELF_PATH, "--isSpawnedProc"]
            
            if not first_spawn:
                cmd.append("--resurrected")

            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            first_spawn = False

            stdout, stderr = process.communicate()
            if process.returncode == 0:
                logger.info("[Nexa Watchdog] Nexa exited normally. Restarting...")
            else:
                logger.error(f"[Nexa Watchdog] Nexa crashed with exit code {process.returncode}. Restarting...")
                logger.debug(f"[Nexa Watchdog] Stdout: {stdout.decode()}")
                logger.debug(f"[Nexa Watchdog] Stderr: {stderr.decode()}")

    logger.info("Nexa is starting as the actual proccess.")

    kill_orphaned_java()  # Clean up any leftover Java processes from previous runs before starting

    logger.info("Checking for updates...")
    update_status = check_for_updates()

    if update_status == 1:
        logger.warning("A new version of Nexa is available! Please check the GitHub repository for updates.")
    elif update_status == -1:
        logger.warning("Could not check for updates.")
    else:
        logger.info("Nexa is up to date.")

    if config.get("security.useOverrides", False):
        load_overrides()

    # Token via environment variable
    token = os.getenv("BOT_TOKEN")
    if not token:
        logger.error("Environment Variable 'BOT_TOKEN' is not set. You should have never encountered this error. If you did, something went very wrong with the early stability checks. Exiting now to prevent further issues.")
        raise RuntimeError("BOT_TOKEN environment variable not set")

    # Check if networking allows PlayIt
    use_playit = config.get("networking.usePlayIt", False)
    if use_playit and not is_playit_running():
        try:
            subprocess.Popen(["playit"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("Started PlayIt client.")
        except Exception as e:
            logger.error("PlayIt is not set up. You should have never encountered this error. If you did, something went very wrong with the early stability checks. Exiting now to prevent further issues.")
            print(f"Failed to start PlayIt client: {e}. Please make sure you have set up PlayIt correctly.", file=sys.stderr, flush=True)
            sys.exit(1)

    # Setup instance manager
    manager = InstanceManager()

    # Resolve instances root folder from NexaConfig (relative to cwd)
    instances_folder_setting = config.get("general.instancesFolder", "instances")
    instances_root = Path.cwd() / Path(instances_folder_setting)

    # Load instances from registry
    try:
        instance_names = registry.list_instances()
    except Exception:
        instance_names = list((registry._data.get("instances") or {}).keys()) if getattr(registry, "_data", None) else []

    if instance_names:
        for name in instance_names:
            try:
                inst_cfg = registry.get_instance(name) or {}
                folder = build_folder_for_instance(instances_root, name, inst_cfg)
                version = inst_cfg.get("version", "")
                loader = inst_cfg.get("loaderType") or inst_cfg.get("loader") or ""
                icon_url = inst_cfg.get("icon_url") or inst_cfg.get("icon") or None

                manager.add_instance(ServerInstance(
                    name=name,
                    folder=folder,
                    version=version,
                    loader=loader,
                    icon_url=icon_url
                ))
                logger.info(f"Registered instance '{name}' -> folder={folder}")
            except Exception as e:
                logger.error(f"Failed to register instance '{name}': {e}")
    else:
        primary = config.get("general.primaryInstance", None)
        if primary:
            try:
                primary_cfg = registry.get_instance(primary) or {}
                folder = build_folder_for_instance(instances_root, primary, primary_cfg)
                version = primary_cfg.get("version", "")
                loader = primary_cfg.get("loaderType") or primary_cfg.get("loader") or ""
                icon_url = primary_cfg.get("icon_url") or None

                manager.add_instance(ServerInstance(
                    name=primary,
                    folder=folder,
                    version=version,
                    loader=loader,
                    icon_url=icon_url
                ))
                logger.info(f"Registered primary instance '{primary}' -> folder={folder}")
            except Exception as e:
                logger.error(f"Failed to register primary instance '{primary}': {e}")
        else:
            logger.warning("No instances found in registry and no primaryInstance configured.")

    # Start Discord bot IF enabled in config
    if config.get("discord.enable", False):
        bot = NexaBot(token=token, instance_manager=manager, registry=registry, config=config, statusChannelID=config.get("discord.statusChannelID", None), nexaUpdateStatus=update_status, isResurrected=args.resurrected)
        bot.start_bot()
        logger.info("Discord bot started.")

if __name__ == "__main__":
    main()