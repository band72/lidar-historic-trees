"""Frozen-program entry point: no arguments starts the web app, otherwise the `fip` CLI."""

from forest_inventory_pipeline.app.launch import main

if __name__ == "__main__":
    main()
