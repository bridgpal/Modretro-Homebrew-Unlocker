# Changelog

All notable changes to MRUpdater Homebrew Patcher are documented here.

---

## [0.5.1] — 2026-04-27

### Added
- **Tkinter GUI** — run `patch_mrupdater.py` with no arguments to open the graphical interface instead of using the command line.
- **Browse dialogs** — file pickers for both input (`MRUpdater.exe`) and output (patched exe).
- **Indeterminate progress bar** — animates while patching is in progress.
- **Status label** — live feedback ("Ready.", "Patching…", "Done!", or an error message).
- **About section** — brief description and 3-step instructions displayed at the top of the window.
- **Disclaimer** — personal/educational-use notice displayed at the bottom of the window.
- Output path auto-populated from input selection (defaults to `MRUpdater_homebrew.exe` in the same folder).

### Changed
- CLI mode still works unchanged when arguments are passed (`python patch_mrupdater.py MRUpdater.exe`).

---

## [0.0.1] — Initial release

### Added
- Command-line patcher targeting `cartclinic.gui` inside the PyInstaller CArchive / PYZ bundle.
- **Patch 1** (`CartClinic.update_screens`): removes the `developer_mode` conditional that hid the HOMEBREW button from the Cart Clinic UI.
- **Patch 2** (`CartClinic.cart_clinic_homebrew`): removes the early-return guard that prevented the ROM file picker from opening.
- Supports both PyInstaller 2.0 and 2.1+ cookie formats.
- Handles zlib-compressed PYZ entries.
- Correctly rebuilds the CArchive TOC and cookie when the patched PYZ changes size.
- Outputs a new exe (`MRUpdater_homebrew.exe`) without modifying the original.
- Pure standard-library implementation — no third-party dependencies.
