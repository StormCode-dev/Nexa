# Contributing to Nexa

Thank you for showing interest in contributing to Nexa! This document serves as a sort of handbook for contributing and covers what you need to know before opening a PR. Please read it fully, as it most likely will catch something that otherwise would get your PR rejected.

## Getting Started

To get your workstation set up, follow the steps below:

1. Fork the repository and clone your fork.
2. Make sure your system has **Python 3.14** installed. Python 3.14 is required, and Nexa targets this specific python version intentionally. PRs that introduce code incompatible with Python 3.14, or that change the pinned version, will not be merged without prior discussion.
3. Install the dependencies in your desired environment (assuming cd'd to local repo):
```bash
pip install -r requirements.txt
```
Nexa will also check the Java version. This is always updated to be the latest LTS release of Java, as provided by Oracle. Make sure you have this installed on your machine, or any Java version set as current latest LTS. You can find them here: https://www.oracle.com/java/technologies/downloads/

4. Fill in your environment variables Nexa requires. The following are required:
```
BOT_TOKEN=<a-token-for-your-discord-bot-goes-here> 
NEXA_PROTECTED_KEY=<put-your-own-32-char-key-here>
```

When you commit, PLEASE make sure you don't hardcode any of these values in, for your own safety.
5. Run the bot locally to ensure everything works.

If all goes well, you should have Nexa set up to run locally.

## Project Structure

Everything programmatical for Nexa lives in the ```src``` folder.

Nexa's various systems are categorized even more into the following folders: ```backend```, ```bot```, ```services```, and ```sharedLibs```.

```backend``` contains parts of the program that tackle the server side portion of Nexa, which means things like Minecraft and the config.

```bot``` contains everything that Nexa needs to interact with a Discord server, proccess commands, parse queries, etc. Whatever it may be, if it touches Discord, it will go through here. You can view command functionality in the ```cogs``` folder, which houses ```general.py``` (everyday commands), ```instances.py``` (commands that interact with Minecraft Server instances), ```superuser.py``` (commands only executable from designated superusers), and ```system.py``` (bot operations that Nexa uses to manage the system and itself). ```discordBot.py``` is the Nexa entry loop, and ```ui.py``` contain custom user interface components that Nexa's bot code uses.

```services``` houses the various utility systems needed for Nexa to run smoothly. This, notably, includes the custom Modrinth API, database management, logger support, and other stuff.

```sharedlibs``` is currently unused at the moment, and may be removed.

## Build Tooling

Nexa is compiled with **Nuitka**. This was chosen over PyInstaller, cx_Freeze, or any other freezer, for performance on the server. PRs that swap the build backend **will be closed.** If you hit Nuitka-specific issues, raise them as your own issue rather than working around them by switching tools.

## CI and Workflow Permissions

I am okay with CI tools proposed, but do traditionally err on the local side of tools over cloud solutions like GitHub Actions and whatnot. If you submit something like a GitHub Action, make sure it has the least amount of permissions possible. Don't request ```contents: write``` unless the tool absolutely requires it. Please explain why it needs this permission in the PR description, if it does. Always assume these proposals will be scrutinized.


## Security-sensitive Areas.

Nexa is built on a zero-trust security model, and as such, the following areas get extra review scrutiny, and PRs touching them should explain the security reasoning in the description, not just the functional change:

- IPC
- protectedDB and anything touching encryption

If you're unsure whether something counts as security-sensitive, please ask in an issue before submitting the PR.

## Programming Style

- Following existing code format conventions as the codebase. Don't introduce a brand new style for literally no reason.
- Keep cogs you write async-consistent. Don't introduce blocking calls into the event loop.
- Comment non-obvious security or protocol logic. Assume the next reader doesn't have the same context as you do.

## Almost Guaranteed Instant-rejects

- PR bumps updateIndex.json. I bump that on release when I decide to compile as a release.
- You touch the Build tools or modify them without discussing it prior.

## PR Checklist

Before opening a PR, confirm:

- The code targets Python 3.14
- No build tools changed, unless discussed first
- No unnecessary CI permission elevation
- Commands are in the correct cog
- Security-sensitive changes include a rationale in the description
- You've tested the changes against a local instance of Nexa

PRs that don't check this beforehand will likely be rejected.

## Code of Conduct

Most contributors don't have all the free time in the world, and dedicate the time that they can give to working on Nexa, which I am forever grateful for. As such, be respectful and patient with maintainers and contributors. Disagreements are fine. Disrespect isn't.
