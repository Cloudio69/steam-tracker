# Steam Download Tracker

Steam Download Tracker is a desktop app that shows the status of games being downloaded or updated by Steam. It finds Steam libraries, reads their app manifests, and displays progress in a simple window.

## Features

- Scans common Steam locations and available drives for Steam libraries.
- Lets you add a library folder manually.
- Shows download progress, game size, and recent file activity.
- Can show live download speed, disk activity, and estimated time remaining when Steam's CEF debugging interface is available.
- Includes search and an option to show only active downloads.
- Can optionally shut down the computer after a download finishes. This feature is off by default.
- Can display game header images when Pillow is installed and an internet connection is available.

## Requirements

- Python 3.8 or later.
- Tkinter, which is included with most Python installations. On some Linux distributions, install the system package for Tkinter separately.

The app runs without these packages, but installing them enables all optional features:

```powershell
py -m pip install -r requirements.txt
```

The requirements are:

- `Pillow` for game thumbnails.
- `psutil` for the overall network activity indicator.
- `websocket-client` for live data from Steam.

## Run

From the project directory, start the app with:

```powershell
py SteamTracker.py
```

On systems where `py` is not available, use:

```text
python SteamTracker.py
```

The app scans common Steam locations at startup. Use **Scan drives** to scan again, or **Add folder** to select a Steam library or its `steamapps` folder.

## Live Steam data

Live data requires Steam to expose its CEF remote debugging interface. The app can enable this with the **Enable Steam debug** button. Steam must be restarted for this setting to take effect; any active downloads will pause during the restart and should resume afterward.

The app connects to the local debugging endpoint on port `8080` by default. To use a different port, set the `STEAM_CDP_PORT` environment variable before launching the tracker. For example, in PowerShell:

```powershell
$env:STEAM_CDP_PORT = "8081"
py SteamTracker.py
```

Use **Disable debug** to remove the marker file created by the app. Steam may need to be restarted for the change to take effect.

For reliable, real-time download tracking, enable Steam debug mode. Without it, the tracker relies on Steam library files and manifests, which may be outdated or update less frequently, so download progress and status can be inaccurate.

## Automatic shutdown

Enable the shutdown option and choose a delay in minutes. The tracker waits until it has detected an active download, then confirms that downloads have stopped before starting the timer. If a download resumes, the scheduled shutdown is canceled. On Windows, the system shutdown timer is used; on macOS and Linux, the tracker invokes the system's shutdown command when the countdown ends.

## Notes

- The tracker reads Steam library manifests and files on your computer. It does not start or control downloads.
- The live debugging endpoint is local to your computer. Steam must be running with CEF debugging enabled for live data to appear.
- Thumbnails are saved in the `.steam_download_tracker` folder in your home directory.
- This bullshit was made with Claude... well it works and i'm ashamed of it