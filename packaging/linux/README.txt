LiDAR Tree Inventory - Linux
============================

Install (for your user, no root needed):

    ./install.sh

or for all users:

    sudo ./install.sh --system

Then start "LiDAR Tree Inventory" from your applications menu, or run `fip` in a
terminal. Either way the web interface opens in your browser. The terminal window
shows the log; close it (or press Ctrl+C) to stop the app.

Command line:   fip --help
Check install:  fip self-test
Uninstall:      ~/.local/share/lidar-tree-inventory/uninstall.sh
                (or /opt/lidar-tree-inventory/uninstall.sh for --system installs)

Requires x86_64 Linux with glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora 36
or later). No Python installation is needed: everything is bundled.

Logs from the web app go to ~/.local/state/lidar-tree-inventory/logs/app.log.
`fip inventory` writes its log next to its outputs (out/fip.log).
