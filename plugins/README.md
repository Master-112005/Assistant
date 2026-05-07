# Nova Assistant Plugins

Plugins live in subdirectories that contain a `manifest.json` file and a Python
entry point. The core assistant discovers and loads them dynamically through
`core.plugin_manager.PluginManager`; adding a plugin must not require editing
core routing imports.

The bundled VS Code plugin is disabled by default because it requests local
filesystem and automation permissions. Enable it with `enable vscode plugin`.
