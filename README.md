# Spatial Hub

Spatial Hub is an experimental Windows spatial-intelligence hub. It runs in the background, keeps a local 3D dashboard, accepts phone sensor nodes on the same trusted LAN, and is designed to update itself from this repository without reinstalling the app.

## Install on Windows

1. Download `install.ps1` and `Install-SpatialHub.cmd` into the same folder, or use the installer ZIP supplied from ChatGPT.
2. Double-click `Install-SpatialHub.cmd`.
3. If Windows Firewall asks, allow Python on **Private networks only**.
4. Spatial Hub creates shortcuts on the Desktop, Start menu and Windows Startup.

Python 3.11+ is required. The current test machine uses Python 3.13.

## Automatic updates

A small bootstrap is installed to `%LOCALAPPDATA%\SpatialHub\bootstrap\launcher.pyw`. On every start it reads `update/manifest.json`, downloads a newer payload when the version changes, updates Python dependencies when required, and launches the current app.

User data is kept separately under `%LOCALAPPDATA%\SpatialHub\data`, so normal application updates do not overwrite learned state or history.

To publish a future update, update the files in `payload/` and then increment the version in `update/manifest.json`. Installed clients will pull the new version automatically.

## Current Windows v0.3.0

- Background process with system-tray icon.
- Starts automatically after Windows sign-in.
- Responsive local 3D dashboard.
- QR pairing page for phone sensor nodes.
- Phone telemetry can continue while the web page remains active; a future native iOS/Android client will remove the browser limitation.
- Spatial map appears immediately when a device joins.
- Explicit uncertainty instead of fake centimeter-level coordinates.
- Windows CPU/RAM/battery telemetry.
- Local event history and persistent state.
- Automatic update channel via this GitHub repository.

## Accuracy limitation

Ordinary Wi-Fi RSSI, Bluetooth RSSI, LAN presence and network latency do **not** provide centimeter-accurate indoor XYZ coordinates. Exact indoor mapping requires additional spatial/ranging sources such as UWB, ARKit/visual-inertial SLAM, LiDAR, Wi-Fi CSI arrays, or calibrated anchors.

Use network/radio discovery and connected-node functionality only with networks and devices you own or are authorized to inspect.
