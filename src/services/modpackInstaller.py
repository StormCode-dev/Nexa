# modpackInstaller.py
# Part of Nexa. Handles automatic modpack installation from a .mrpack URL.
# Owns the full pipeline: download, validate, diff, stage, test, merge.
# Under the MIT License.

import asyncio
import hashlib
import json
import shutil
import socket
import zipfile
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Callable, Awaitable, Optional
import os
import stat
 
import aiohttp
from aiomcrcon import Client
 
from backend.instanceManager import InstanceManager, ServerStatus
from services.nexaConfig import NexaInstanceRegistry, NexaConfig
from services import modrinth
from services import nexaLoggerFactory
 
logger = nexaLoggerFactory.get_logger("ModpackInstaller")
config = NexaConfig("NexaBotConfig.yaml")
 
# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------
 
class InstallStage(Enum):
    DOWNLOADING_MRPACK   = auto()
    VALIDATING_MRPACK    = auto()
    WAITING_FOR_SHUTDOWN = auto()
    LOCKING              = auto()
    CLONING_INSTANCE     = auto()
    DIFFING_MODS         = auto()
    DOWNLOADING_MODS     = auto()
    VERIFYING_MODS       = auto()
    APPLYING_MODS        = auto()
    STARTING_STAGED      = auto()
    TESTING_STAGED       = auto()
    MERGING              = auto()
    COMPLETE             = auto()
    FAILED               = auto()
 
 
STAGE_LABELS = {
    InstallStage.DOWNLOADING_MRPACK:   "□□□□□□□□ | Downloading modpack…",
    InstallStage.VALIDATING_MRPACK:    "■□□□□□□□ | Validating modpack…",
    InstallStage.WAITING_FOR_SHUTDOWN: "■■□□□□□□ | Waiting for server shutdown…",
    InstallStage.LOCKING:              "■■□□□□□□ | Locking instance…",
    InstallStage.CLONING_INSTANCE:     "■■■□□□□□ | Cloning instance to staging…",
    InstallStage.DIFFING_MODS:         "■■■□□□□□ | Comparing mods against Modrinth…",
    InstallStage.DOWNLOADING_MODS:     "■■■□□□□□ | Downloading updated mods…",
    InstallStage.VERIFYING_MODS:       "■■■■□□□□ | Verifying mod compatibility…",
    InstallStage.APPLYING_MODS:        "■■■■□□□□ | Applying mod updates…",
    InstallStage.STARTING_STAGED:      "■■■■■□□□ | Starting staged server for testing…",
    InstallStage.TESTING_STAGED:       "■■■■■■□□ | Testing staged server…",
    InstallStage.MERGING:              "■■■■■■■□ | Merging staged files back to instance…",
    InstallStage.COMPLETE:             "■■■■■■■■ | Installation complete.",
    InstallStage.FAILED:               "❌ | Installation failed.",
}
 
 
@dataclass
class InstallStatus:
    stage:   InstallStage
    detail:  str = ""
    failed:  bool = False
 
 
# A callback the Discord command provides to receive live status updates.
# The installer calls this whenever state changes; the command updates the embed.
StatusCallback = Callable[[InstallStatus], Awaitable[None]]
 
 
# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
 
@dataclass
class InstallResult:
    success: bool
    message: str
 
 
# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
 
def _sha512_file(path: Path) -> str:
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
 
 
def _find_free_port(start: int = 25600, end: int = 25700) -> Optional[int]:
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return None
 
 
def _patch_server_properties(props_path: Path, port: int):
    """Rewrite server-port in server.properties to the given port."""
    lines = props_path.read_text(encoding="utf-8").splitlines()
    patched = []
    for line in lines:
        if line.startswith("server-port="):
            patched.append(f"server-port={port}")
        elif line.startswith("rcon.port="):
            patched.append(f"rcon.port={port + 1}")
        else:
            patched.append(line)
    props_path.write_text("\n".join(patched), encoding="utf-8")
 
 
def _merge_directories(src: Path, dst: Path, exclude: set[str] = None):
    """
    Merge src into dst:
    - Files present in src replace their counterparts in dst unconditionally.
    - Files present in src but not in dst are added.
    - Files present in dst but not in src are left untouched.
    - Files in exclude (by filename) are skipped entirely.
    """
    exclude = exclude or set()
    for item in src.rglob("*"):
        if item.is_file():
            if item.name in exclude:
                continue
            relative = item.relative_to(src)
            target   = dst / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)

def _force_rmtree(path: str):
    root = Path(path)
    # Strip read-only from every file and directory first
    for item in root.rglob("*"):
        try:
            os.chmod(item, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        except Exception:
            pass
    # Also chmod the root itself
    try:
        os.chmod(root, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    except Exception:
        pass
    shutil.rmtree(path)
 
 
# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------
 
class ModpackInstaller:
    """
    Owns the full modpack installation pipeline for a single install operation.
    Instantiate once per install attempt.
    """
 
    def __init__(
        self,
        url:              str,
        instance_name:    str,
        instance_manager: InstanceManager,
        registry:         NexaInstanceRegistry,
        on_status:        StatusCallback,
    ):
        self.url              = url
        self.instance_name    = instance_name
        self.instance_manager = instance_manager
        self.registry         = registry
        self._on_status       = on_status
 
        # Resolved at runtime
        self._staging_root:  Optional[Path] = None   # <cwd>/staging/<instance_name>/
        self._mrpack_path:   Optional[Path] = None
        self._staged_mods:   Optional[Path] = None   # staging/stagedMods/
        self._manifest:      Optional[dict] = None
 
    # ---------------------------------------------------------------------------
    # Public entry point
    # ---------------------------------------------------------------------------
 
    async def run(self) -> InstallResult:
        try:
            return await self._run()
        except Exception as e:
            logger.error(f"Unhandled exception during modpack install for '{self.instance_name}': {e}", exc_info=True)
            await self._report(InstallStage.FAILED, str(e))
            await self._cleanup()
            return InstallResult(success=False, message=f"Unexpected error: {e}")
 
    # ---------------------------------------------------------------------------
    # Pipeline
    # ---------------------------------------------------------------------------
 
    async def _run(self) -> InstallResult:
        instance = self.instance_manager.get_instance(self.instance_name)
        if not instance:
            return InstallResult(success=False, message=f"Instance `{self.instance_name}` not found.")
 
        inst_cfg  = self.registry.get_instance(self.instance_name) or {}
        game_version = inst_cfg.get("version", "")
        loader       = (inst_cfg.get("loaderType") or inst_cfg.get("loader") or "").lower()
 
        # --- Stage 1: Download mrpack ---
        await self._report(InstallStage.DOWNLOADING_MRPACK, self.url)
        staging_root = Path.cwd() / "staging" / self.instance_name
 
        # Clear any leftover staging data from a previous run
        if staging_root.exists():
            logger.info(f"Clearing leftover staging data for '{self.instance_name}'.")
            for attempt in range(3):
                try:
                    _force_rmtree(staging_root)
                    break
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Staging cleanup attempt {attempt + 1} failed: {e}. Retrying...")
                        await asyncio.sleep(1)
                    else:
                        try:
                            staging_root.unlink()
                            logger.warning(f"Deleted staging file {staging_root} to recover from cleanup failure.")
                        except Exception as e2:
                            logger.error(f"Failed to delete staging file {staging_root}: {e2}")
                            return InstallResult(success=False, message=f"Could not clear staging folder: {e} (also failed to delete file: {e2})")
 
        staging_root.mkdir(parents=True, exist_ok=True)
        self._staging_root = staging_root
 
        mrpack_path = staging_root / "modpack.mrpack"
        self._mrpack_path = mrpack_path
 
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(self.url) as resp:
                    resp.raise_for_status()
                    with open(mrpack_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
        except Exception as e:
            return await self._fail(f"Failed to download modpack: {e}")
 
        # --- Stage 2: Validate mrpack ---
        await self._report(InstallStage.VALIDATING_MRPACK)
        if not zipfile.is_zipfile(mrpack_path):
            return await self._fail("File is not a valid ZIP archive.")
 
        with zipfile.ZipFile(mrpack_path, "r") as zf:
            if "modrinth.index.json" not in zf.namelist():
                return await self._fail("modrinth.index.json not found in modpack.")
            manifest = json.loads(zf.read("modrinth.index.json"))
 
        self._manifest = manifest
 
        required_fields = {"files", "dependencies", "name", "versionId"}
        if not required_fields.issubset(manifest.keys()):
            return await self._fail(f"modrinth.index.json is missing required fields: {required_fields - manifest.keys()}")
 
        # --- Stage 3: Shut down instance if needed ---
        await self._report(InstallStage.WAITING_FOR_SHUTDOWN)
 
        await instance.refresh_players()
 
        if instance.status == ServerStatus.ONLINE:
            if instance.players > 0:
                # Wait until offline
                while instance.status != ServerStatus.OFFLINE:
                    await asyncio.sleep(2)
            else:
                # Empty. Burn it. Pillage it. Leave nothing but a smoking crater.
                await self.instance_manager.stop_instance(self.instance_name, hard=True)
                while instance.status != ServerStatus.OFFLINE:
                    await asyncio.sleep(2)
        elif instance.status == ServerStatus.SLEEPING:
            await self.instance_manager.stop_instance(self.instance_name, hard=True)
            while instance.status != ServerStatus.OFFLINE:
                await asyncio.sleep(2)
 
        # --- Stage 4: Lock instance ---
        await self._report(InstallStage.LOCKING)
        instance.locked = True
        logger.info(f"Instance '{self.instance_name}' locked for modpack installation.")
 
        # --- Stage 5: Clone instance to staging ---
        await self._report(InstallStage.CLONING_INSTANCE)
        staged_instance = staging_root / "instance"
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, shutil.copytree, str(instance.folder), str(staged_instance)
            )
        except Exception as e:
            return await self._fail(f"Failed to clone instance: {e}")
 
        # --- Stage 6: Diff mods ---
        await self._report(InstallStage.DIFFING_MODS)
        staged_mods_folder = staged_instance / "mods"
        staged_mods_folder.mkdir(parents=True, exist_ok=True)

        # Hash all existing mods in the staged instance
        existing_hashes: dict[str, Path] = {}
        for jar in staged_mods_folder.glob("*.jar"):
            existing_hashes[_sha512_file(jar)] = jar

        # Filter out client-only mods and determine what needs downloading
        manifest_files = manifest.get("files", [])
        to_download: list[dict] = []
        skipped_client_only: list[str] = []

        for mf in manifest_files:
            file_hashes = mf.get("hashes", {})
            sha512      = file_hashes.get("sha512")
            filename    = Path(mf.get("path", "unknown.jar")).name

            # Resolve project_id via hash lookup to check side support
            version_info = await modrinth.get_version_from_hash(sha512) if sha512 else None
            if version_info:
                project_id = version_info.get("project_id")
                if project_id:
                    project = await modrinth.get_project(project_id)
                    if project and modrinth.is_client_only(project):
                        logger.info(f"Skipping client-only mod: {filename} (project {project_id})")
                        skipped_client_only.append(filename)
                        continue
                    
            if sha512 and sha512 in existing_hashes:
                logger.info(f"Skipping {filename}. Already up to date.")
            else:
                to_download.append(mf)

        if skipped_client_only:
            await self._report(
                InstallStage.DIFFING_MODS,
                f"{len(to_download)} mod(s) to update, "
                f"{len(manifest_files) - len(to_download) - len(skipped_client_only)} already up to date, "
                f"{len(skipped_client_only)} client-only (skipped)."
            )
        else:
            await self._report(
                InstallStage.DIFFING_MODS,
                f"{len(to_download)} mod(s) need updating, "
                f"{len(manifest_files) - len(to_download)} already up to date."
            )
 
        # --- Stage 7: Download updated mods to stagedMods ---
        await self._report(InstallStage.DOWNLOADING_MODS, f"Downloading {len(to_download)} mod(s)…")
        staged_mods_dl = staging_root / "stagedMods"
        staged_mods_dl.mkdir(parents=True, exist_ok=True)
        self._staged_mods = staged_mods_dl
 
        download_failures: list[str] = []
        async with aiohttp.ClientSession() as session:
            for mf in to_download:
                urls     = mf.get("downloads", [])
                sha512   = mf.get("hashes", {}).get("sha512")
                filename = Path(mf.get("path", "unknown.jar")).name
 
                if not urls:
                    download_failures.append(f"{filename}: no download URL in manifest")
                    continue
 
                downloaded = False
                for url in urls:
                    try:
                        dest = staged_mods_dl / filename
                        async with session.get(url) as resp:
                            resp.raise_for_status()
                            with open(dest, "wb") as f:
                                async for chunk in resp.content.iter_chunked(1024 * 1024):
                                    f.write(chunk)
 
                        # Verify hash immediately after download
                        actual = _sha512_file(dest)
                        if sha512 and actual != sha512:
                            dest.unlink(missing_ok=True)
                            logger.warning(f"Hash mismatch for {filename} from {url}, trying next URL.")
                            continue
 
                        downloaded = True
                        await self._report(InstallStage.DOWNLOADING_MODS, f"Downloaded: {filename}")
                        break
                    except Exception as e:
                        logger.warning(f"Failed to download {filename} from {url}: {e}")
                        continue
 
                if not downloaded:
                    download_failures.append(f"{filename}: all download URLs failed or hash mismatch")
 
        if download_failures:
            return await self._fail(
                "Download/verification failed for:\n" + "\n".join(f"  - {f}" for f in download_failures)
            )
 
        # --- Stage 8: Verify mod compatibility ---
        strict_verification = config.get("automaticModpackBootstrapper.strictModVerification", True)

        if strict_verification:
            await self._report(InstallStage.VERIFYING_MODS, f"Verifying against {loader} / {game_version}…")
            compat_failures: list[str] = []
    
            for jar in staged_mods_dl.iterdir():
                if not jar.suffix == ".jar":
                    continue
                sha512 = _sha512_file(jar)
                result = await modrinth.search_by_hash(sha512, game_version, loader)
                if result is None:
                    compat_failures.append(f"{jar.name}: not found on Modrinth or incompatible with {loader} {game_version}")


            #if compat_failures:
            #    return await self._fail(
            #        "Compatibility verification failed:\n" + "\n".join(f"  - {f}" for f in compat_failures)
            #    )

            # Better implementation of the above commented code that only shows 5 failures and adds how many total failures there are, since there can be a lot of mods that fail this check and it can be noisy.
            # Also adds a number at the end, like "26 more unshown failures" if there are more than 5 failures.
            if compat_failures:
                max_display = 5
                displayed_failures = compat_failures[:max_display]
                remaining_count = len(compat_failures) - max_display
                message = "Compatibility verification failed:\n" + "\n".join(f"  - {f}" for f in displayed_failures)
                if remaining_count > 0:
                    message += f"\n  ... and {remaining_count} more unshown failure(s)."
                    if not strict_verification:
                        message += f"\n\nYou can excuse tight checking for specific mods by going to your config and toggling 'strictModVerification' to false. This will skip the Modrinth hash check and just verify that the mod is present, which should be sufficient for most cases and will allow you to install modpacks with custom or private mods that aren't on Modrinth."
                return await self._fail(message)
    
        # --- Stage 9: Apply mods to staged instance ---
        await self._report(InstallStage.APPLYING_MODS)
 
        # Remove mods from staged instance that are being replaced
        manifest_filenames = {Path(mf.get("path", "")).name for mf in to_download}
        for jar in staged_mods_folder.glob("*.jar"):
            if jar.name in manifest_filenames:
                jar.unlink()
 
        # Copy downloaded mods into staged instance mods folder
        for jar in staged_mods_dl.iterdir():
            if jar.suffix == ".jar":
                shutil.copy2(jar, staged_mods_folder / jar.name)
 
        # Apply overrides from mrpack
        with zipfile.ZipFile(mrpack_path, "r") as zf:
            for member in zf.namelist():
                if member.startswith("overrides/") and not member.endswith("/"):
                    relative = member[len("overrides/"):]
                    dest     = staged_instance / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
 
        # --- Stage 10: Start staged server for testing ---
        await self._report(InstallStage.STARTING_STAGED, "Finding free port and starting staged server…")
 
        staging_port = _find_free_port()
        if staging_port is None:
            return await self._fail("Could not find a free port for staged server.")
 
        staged_props = staged_instance / "server.properties"
        if staged_props.exists():
            _patch_server_properties(staged_props, staging_port)
 
        staged_rcon_port     = staging_port + 1
        staged_rcon_password = instance.rconPass
 
        import subprocess, sys
        staged_proc = subprocess.Popen(
            instance.startCmd,
            cwd=str(staged_instance),
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
 
        # --- Stage 11: Test staged server via RCON ---
        await self._report(InstallStage.TESTING_STAGED, "Waiting for staged server to accept RCON…")
 
        rcon_ok  = False
        timeout  = 300
        elapsed  = 0
 
        while elapsed < timeout:
            try:
                async with Client("127.0.0.1", staged_rcon_port, staged_rcon_password) as rcon:
                    # Test 1: Basic command to see if RCON is responsive
                    response, _ = await rcon.send_cmd("list", timeout=timeout)
                    if response is not None:
                        rcon_ok = True
                        break
            except Exception:
                pass
            await asyncio.sleep(3)
            elapsed += 3
 
        # Stop staged server regardless of outcome
        try:
            async with Client("127.0.0.1", staged_rcon_port, staged_rcon_password) as rcon:
                await rcon.send_cmd("stop")
        except Exception:
            pass
 
        for _ in range(10):
            if staged_proc.poll() is not None:
                break
            await asyncio.sleep(2)
 
        if staged_proc.poll() is None:
            staged_proc.terminate()
            try:
                staged_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                staged_proc.kill()
 
        if not rcon_ok:
            return await self._fail("Staged server failed to start or accept RCON within timeout. The modpack may be incompatible with the current world.")
 
        # --- Stage 12: Merge staged instance back to real instance ---
        await self._report(InstallStage.MERGING, "Merging staged files back to instance…")
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, _merge_directories, staged_instance, instance.folder, {"server.properties", ""}
            )
        except Exception as e:
            return await self._fail(f"Failed to merge staged instance: {e}")
 
        # --- Done ---
        await self._cleanup()
        instance.locked = False
        logger.info(f"Modpack installation complete for '{self.instance_name}'.")
        version_str = f" v{manifest.get('versionId')}" if manifest.get('versionId') else ""
        await self._report(InstallStage.COMPLETE, f"Successfully installed `{manifest.get('name', 'modpack')}`{version_str}.")
        return InstallResult(success=True, message=f"Modpack `{manifest.get('name')}`{version_str} installed successfully.")
 
    # ---------------------------------------------------------------------------
    # Internals
    # ---------------------------------------------------------------------------
 
    async def _report(self, stage: InstallStage, detail: str = ""):
        label = STAGE_LABELS.get(stage, stage.name)
        logger.info(f"[{self.instance_name}] {label} {detail}".strip())
        await self._on_status(InstallStatus(stage=stage, detail=detail, failed=(stage == InstallStage.FAILED)))
 
    async def _fail(self, reason: str) -> InstallResult:
        logger.error(f"[{self.instance_name}] Install failed: {reason}")
        await self._report(InstallStage.FAILED, reason)
        await self._cleanup()
 
        # Ensure instance is unlocked on failure
        instance = self.instance_manager.get_instance(self.instance_name)
        if instance:
            instance.locked = False
 
        return InstallResult(success=False, message=reason)
 
    async def _cleanup(self):
        await asyncio.sleep(5)  # Wait for JVM to fully release file handles before cleanup
        print("Cleaning up staging files…")
        if self._staging_root and self._staging_root.exists():
            try:
                _force_rmtree(self._staging_root)
                logger.info(f"Cleaned up staging folder for '{self.instance_name}'.")
            except Exception as e:
                logger.warning(f"Failed to clean up staging folder for '{self.instance_name}': {e}")