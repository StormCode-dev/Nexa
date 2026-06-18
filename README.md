# Nexa

> Great features for your server on your hardware, free of charge.

Nexa is a Discord-integrated Minecraft Java server management system. It allows you to manage multiple instances directly from your Discord server. It provides premium featues that one would expect with paid hosters. Start, stop, deploy modpacks, and server control access, all without touching the machine.

---

## Features

- **Multi-instance management**: Run and control multiple Minecraft Java servers directly from the bot interface.
- **Automatic Modpack Deployment**: Ease the pain of deploying modpacks. Install modpacks to an instance, complete with server testing in the process. 
- **Idle auto-shutdown**: Automatically shut down/sleep instances, conserving resources.
- **World Backups**: Schedule automatic, complete world backups.
- **Instance locking**: Hard-lock instances from all users, regardless of permissions.
- **User data management**: Protect user data with encrypted storage. Allows for the management of your data, as well as everybody elses.
- **Guild Authorization**: Set up guilds in which the bot will only work in. Great layer of security.

---

## Requirements:

- Python 3.14+ (if running off source)
- Windows (planned Linux support)
- A discord bot token, with Administrator access
- [Playit.gg](https://playit.gg/) for optional residential network exposure.

---

## Installation (from source)

### 1: Clone the repistory (ignore step if running binary)

```bash
git clone https://github.com/StormCode-dev/Nexa.git
cd nexa
```

## 2: Install dependencies (ignore step if running binary)

```bash
pip install -r requirements.txt
```

## 3: Preparing the run location of Nexa

Nexa requires a few things before it can actually start, all of which are checked beforehand.

Regardless of OS, register the following in your Environment Variables.

BOT_TOKEN=keyForYourDiscordApplication
NEXA_PROTECTED_KEY=aLargeAlphanumericString

Note that the format is {name of the variable}={contents of said variable}.

Additionally, if you want to use PlayIt to tunnel your server instances, install that and **register it to your PATH**.


## 4: Auto-generate everything

Go ahead and run Nexa either by launching the binary or running...

```bash
python src/main.py
```

Immediately stop the proccess.

## 5: Modifying the Config

Nexa should've auto-generated these configs files as a descendent of its parent.

```NexaBotConfig.yaml
general:
  instancesFolder: instances
  primaryInstance: null
  configVersion: 1
discord:
  enable: true
  preventRandomPeopleFromStoppingInstances: true
  lockToAuthorizedGuild: false
  authorizedGuilds: []
  statusChannelID: 0
  updateInterval: 30
  enableSuperUsers: false
  superUsers: []
security:
  enableGatekeeper: true
  blacklistedCapabilities:
  - nexus.decontainerize
  - nexus.loadInstance
  - nexus.unloadInstance
  - nexus.accessCoreDB
  - nexabot.core.subprocesses
  skipLoadingAppsWithBlacklistedCapabilities: true
  shadowZoneApps: true
  useOverrides: false
networking:
  usePlayIt: true
logging:
  enableFileLogging: true
  logFolder: logs
  level: INFO
  maxFileSizeMB: 5
  backupCount: 7
  components:
    rcon: DEBUG
    discord: DEBUG
    vm: INFO
    config: WARNING
automaticModpackBootstrapper:
  strictModVerification: true
```

and

```NexaInstanceRegistry.yaml
instances: {}
```

Because this is YAML, you can swap it for something readable. For the best settings, set in NexaBotConfig...

- discord.lockToAuthorizedGuilds: true
- authorizedGuilds: [serverID, orMultiple, asAWholeNumber]
- enableSuperUsers: true (for added security and management of your server instances. Makes all commands work)
- superUsers: [againAWholeNumberOfYourDiscordUserID, andMoreSinceThisIsAList]
- statusChannelID: Whole number of the channelID from your set up discord server.

I also would keep the update interval the same to prevent discord rate limits, even with Nexa's rate limit safeguards.

## 6: Setting up instances

Each Minecraft Server instance lives under your specified instance folder in the config. By default, it is "instances", so we will roll with that.

Create a directory called "instances" in the same location where NexaBotConfig lands. Go inside, make a new folder that you plan to set up Minecraft Java Server at. This guide won't help you get that set up, nor will Nexa automatically set up a server for you past modpack installs.

Once you have your instance properly set up, you can wire it up in NexaInstanceRegistry.yaml

Below is an example on what an instance in the instances folder called "instance1" might look like:

```NexaInstanceRegistry.yaml
instances:
  # Format: instanceName: instanceConfig
  # This will be iterated through to register instances on startup.
  instance1:
    displayName: "Instance 1"
    version: "1.21.1"
    loaderType: "neoforge"
    icon_url: "https://img.magnific.com/free-psd/grey-boulder-rock-isolated-transparent-background_632498-25568.jpg"
```

You can expand this for multiple instances, like so:

```NexaInstanceRegistry.yaml
instances:
  # Format: instanceName: instanceConfig
  # This will be iterated through to register instances on startup.
  instance1:
    displayName: "Instance 1"
    version: "1.21.1"
    loaderType: "neoforge"
    icon_url: "https://img.magnific.com/free-psd/grey-boulder-rock-isolated-transparent-background_632498-25568.jpg"
  instance2:
    displayName: "Instance 2"
    version: "1.21.1"
    loaderType: "neoforge"
    icon_url: "https://img.magnific.com/free-psd/grey-boulder-rock-isolated-transparent-background_632498-25568.jpg"
```

Of course, don't just paste this in. Follow this format and rename things to agree with your settings.

## 7: Run...again.

Run Nexa, and stop it again.

This time, you'll see both a status embed pop up in your status channel and a NexaServerSettings.yaml file pop up where your Minecraft Java Server actually lives. It looks something like...

```NexaServerSettings.yaml
configVersion: 1
functionality:
  startCmd: java -Xmx4G -Xms4G -jar server.jar nogui
  join_to_wake: false
  watchdog:
    enabled: true
    interval_seconds: 60
    restart_limit: 3
  autosave:
    enabled: true
    interval_days: 3
  auto_shutdown:
    enabled: false
    idle_minutes: 5
security:
  protected_commands:
    enabled: true
    commands:
    - whitelist
    - kick
    - ban
    - op
    - stop
    - execute
```

You can go ahead and make changes now.

## 8: Ready to run fully!

Congratulations! You have now set up Nexa. You can run it. You should have a fully working setup, if you followed the steps correctly.

---

## Discord Commands

All commands are slash command. Users require appropriate discord permissions unless noted otherwise.

### Server Control

| Command | Description |
|---|---|
| `/start` | Starts the primary instance defined in NexaBotConfig. |
| `/stop` | Stops the primary instance defined in NexaBotConfig gracefully. |
| `/start_specific <instance>` | Starts the named instance in the command. |
| `/stop_specific <instance>` | Stops the named instance in the command gracefully. |
| `/force_stop <instance>` | Forcefully stops the named instance, regardless of player count or settings. Superuser Only |

### Instance Management (Superuser Only)

| Command | Description |
|---|---|
| `/lock_instance <instance>` | Locks the named instance from being interacted with, including superusers. |
| `/unlock_instance <instance>` | Unlocks the named instance. |
| `/execute <instance>` | Executes a command on the instance. |

### Modpack Management (Superuser Only)

| Command | Description |
|---|---|
| `/install_modpack <url_to_file> <instance>` | Downloads and installs a .mrpack to the specified instance from a direct URL, with redirects followed. |

### User
 
| Command | Description |
|---|---|
| `/userdata` | Allows any user who has interacted with the bot to view or delete their stored data |
 
---

## Modpack Management

Nexa supports, as you read, installing .mrpack direct from a URL to a specified instance, with redirects followed. You can copy links from CDNs, as an example.

**What happens on install:**

1. Nexa prepares a staging area on the machine running it.
2. It downloads the .mrpack and puts it in the staged directory
3. It performs a byte-level check on the .mrpack to ensure it is not in a different format
4. Once confirmed, it issues the specified instance to shut down fully. The proccess stops here until it is confirmed to be stopped entirely.
5. It locks the Instance, when it is shut down, to prevent unwanted startups and modification not intended.
6. It clones the instance to the staged directory
7. It unzips the .mrpack and compares the index DIRECTLY to Modrinth, skipping client-only mods and downloading the rest to the staged instance.
8. It runs a final compatibility check across all the downloaded mods against the settings of the server. If one or more incompatible mods are found, the install fails, and will tell you.
9. If it passes, it attempts to start the server and check if it can hold an RCON connection.
10. If this passes, the staged server is stopped, mods are merged back to the intended instance, and the staging area is cleaned up.

Instances are **NOT TOUCHED** until Nexa has confirmed the modpack install can boot. It does not resolve dependencies, so you must check to include those in your modpack.

---

## Contributing
 
Contributions are welcome. Please open an issue before submitting a pull request for any significant change.
 
1. Fork the repository
2. Create a feature branch (`git checkout -b feature/your-feature`)
3. Commit your changes
4. Open a pull request against `main`
Bug reports and feature requests can be filed as GitHub Issues.
 
---
 
## License
 
Nexa is licensed under the [MIT License](LICENSE).
 
---
 
*Nexa is not affiliated with Mojang Studios or Microsoft.*


