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

To view upcoming features, you can look at [ROADMAP.md](ROADMAP.md) to get an idea of what to expect next.

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

You will also need to make a file where Nexa will run from called `setupAuthKey.txt`. Inside this file, put:

```
key=<your key, 16 characters or more in length, exlcuding symbols like :;,!@&$>
```


## 4: Auto-generate everything

Go ahead and run Nexa either by launching the binary or running...

```bash
python src/main.py
```

Immediately stop the proccess. Your chosen auth key will be safely stored and deleted off your disc.

## 5: Modifying the Config

Nexa should've auto-generated the following listed config files as a descendent of its parent. Where flagged, modify as you see fit.

```NexaBotConfig.yaml
general:
  instancesFolder: instances
  primaryInstance: null
  configVersion: 1
discord:
  enable: true
  lockToAuthorizedGuild: false -- CONFIGURE
  authorizedGuilds: [] -- CONFIGURE. THIS IS A LIST. Example: [12475923783, 827469826764]
  statusChannelID: 0 -- CONFIGURE
  healthIssuesChannelID: 0 -- CONFIGURE
  updateInterval: 30
  enableSuperUsers: false -- CONFIGURE
  superUsers: [] -- CONFIGURE. THIS IS A LIST. Example: [12475923783, 827469826764]
security:
  enableServerOperators: false -- CONFIGURE AS TRUE FOR MORE COMMANDS
  serverOperators: [] -- CONFIGURE. THIS IS A LIST. Example: [12475923783, 827469826764]
  headOperator: 0 -- CONFIGURE
  shadyAuthAttemptThreshold: 10 -- CONFIGURE
  sftpConnectionLengthInMins: 15 -- CONFIGURE
  allowNexaDesktop: false
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
  strictModVerification: true -- This will tell Nexa's Automatic Modpack Installer to skip the Modrinth + Compatibility check, essentially a blind download and test run. Configure as you see fit.
serverHealthManagement:
  keepNexaAlive: true -- CONFIGURE. True = Restart Nexa. False = No restart Nexa on failure.
  keepAliveIntervalInSecs: 60
  keepPlayItAlive: true -- CONFIGURE
  updateCheckIntervalInMins: 15 -- CONFIGURE. Frequency of Nexa update checks.

```

```NexaInstanceRegistry.yaml
instances: {} -- CONFIGURE. SHOWN BELOW IN SECTION 6.
```

and

```NexaBotCmdCfg.yaml
commands:
  status:
    enabled: true
    permissionLevel: everyone
  start:
    permissionLevel: everyone
  start_specific:
    permissionLevel: everyone
  stop:
    permissionLevel: superuser
  stop_specific:
    permissionLevel: superuser
  execute:
    enabled: true -- CONFIGURE. This will allow the specified clearance level RCON access to an instance.
    permissionLevel: operator
    requireAuthentication: true
  lock_instance:
    enabled: true
    permissionLevel: operator
    requireAuthentication: false
  unlock_instance:
    enabled: true
    permissionLevel: operator
    requireAuthentication: false
  force_stop:
    enabled: true
    permissionLevel: operator
  install_mcpk:
    enabled: true
    permissionLevel: operator
    requireAuthentication: true
    shutdownWaitPeriodInMins: 15 -- CONFIGURE. This command will schedule an instance shutdown for this given time.
  keyman:
    enabled: true
    permissionLevel: headOperator
    issue:
      requireAuthentication: true
    modify:
      requireAuthentication: true
    revoke:
      requireAuthentication: true
    rotate:
      requireAuthentication: true
    list:
      requireAuthentication: true
  fsaccess:
    enabled: true -- CONFIGURE. This will expose a LIVE FILESYSTEM TO A VERIFIED OPERATOR WITH A VALID CREDENTIAL YOU ISSUE. IF YOU DO NOT TRUST ANYONE, DISABLE THIS TO REMOVE THE 
    -- POTENTIAL SURFACE AREA.
    permissionLevel: operator
    requireAuthentication: true
    askHeadOperatorForApproval: true
  check_updates:
    enabled: true
    permissionLevel: superuser

```

For general info, `requireAuthentication` will gate that command behind Nexa's Web-based Authenticator. See the `keyman` bucket of commands below for more info.

`permissionLevel` and `requireAuthentication` must agree. This isn't enforced, but your command situation will get borked if not set properly. For `operator` as the permission
level and above, `requireAuthentication` should be on. If the permission level is set at `superuser` or `everyone`, and `requireAuthentication` is on, that permission level
won't get to be able to use those commands.

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

Importantly, make sure `version` and `loaderType` are correct. For something non-standard like PaperMC, fill `loaderType` in as `paper`.

## 7: Run...again.

Run Nexa, and stop it again.

This time, you'll see both a status embed pop up in your status channel and a NexaServerSettings.yaml file pop up where your Minecraft Java Server actually lives. It looks something like...

```NexaServerSettings.yaml
configVersion: 1
functionality:
  startCmd: java -Xmx4G -Xms4G -jar server.jar nogui -- CONFIGURE. This is important. This executes at the directory of the instance. Make sure server.jar matches whatever JAR
  -- exists in that directory as the Minecraft Server program. If your instance contains a start.bat or start.sh file, you can use that command and it'll work.
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
| `/start_specific` | Starts the named instance in the command. |
| `/stop_specific` | Stops the named instance in the command gracefully. |
| `/force_stop` | Forcefully stops the named instance, regardless of player count or settings. |

### Instance Management

| Command | Description |
|---|---|
| `/lock_instance` | Locks the named instance from being interacted with for everyone. |
| `/unlock_instance` | Unlocks the named instance. |
| `/execute` | Executes a command on the instance. |

### Modpack Management

| Command | Description |
|---|---|
| `/install_modpack` | Downloads and installs a .mrpack to the specified instance from a direct URL, with redirects followed. |

### User
 
| Command | Description |
|---|---|
| `/userdata` | Allows any user who has interacted with the bot to view or delete their stored data |
 
### Keyman Bucket (/keyman <cmd>)

This is locked behind the Head Operator by default. You hold the credential for this command as the person setting up Nexa. This can generate Authentication Keys for 
users you wish to grant operator-level access for. Make sure the keys generated are not shared with anyone other than the intended person.

| Command | Description |
|---|---|
| `/issue` | Issues a new Authentication Key with one permission to one user who is listed in NexaBotConfig.yaml as a serverOperator. |
| `/list` | Lists the permissions for a user with a valid Authentication Key. |
| `/modify` | Add/Remove a permission from a user with a valid Authentication Key. |
| `/revoke` | Removes an Authentication Key for a user. |
| `/rotate` | Removes and regenerates an Authentication Key for a user. |

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
 
Contributions are welcome. Check CONTRIBUTING.md for more info.
 
---
 
## License
 
Nexa is licensed under the [MIT License](LICENSE).

Pinggy is bundled automatically with compiled version of Nexa. Pinggy is licensed under Apache 2.0. See [`Pinggy's Apache 2.0 license`](legal/pinggy/LICENSE-pinggy.txt).
 
---
 
*Nexa is not affiliated with Mojang Studios or Microsoft.*


