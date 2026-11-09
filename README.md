# MRUpdater Homebrew Patcher

**TL;DR:** Unlock the hidden **HOMEBREW** button in Cart Clinic on Windows or macOS. The patcher creates a separate patched executable or app and preserves the original.

## Requirements

- **Python 3.8+** from [python.org](https://www.python.org/downloads/). No third-party Python packages are required. Tkinter is optional for the GUI.
- Your original `MRUpdater.exe` (Windows) or `ChromaticFirmwareUpdater.app` (macOS) from ModRetro.
- For `.app` patching, run on macOS with Apple's `ditto` and `codesign` tools available.

## Command line

### Windows

```sh
python patch_mrupdater.py MRUpdater.exe
```

This creates `MRUpdater_homebrew.exe` in the same folder. To choose another output path:

```sh
python patch_mrupdater.py MRUpdater.exe "C:\path\to\MRUpdater_homebrew.exe"
```

### macOS

Pass the **whole `.app` bundle**, with a new output location where you have write permission:

```sh
python3 patch_mrupdater.py "/Applications/ChromaticFirmwareUpdater.app" \
  "$HOME/Downloads/ChromaticFirmwareUpdater_homebrew.app"
```

The script copies the app, applies both patches, replaces the invalidated vendor signature with a local ad-hoc signature, and verifies the result. The output appears only after patching and signature verification succeed. The original app remains unchanged.

Open the patched copy by its path:

```sh
open "$HOME/Downloads/ChromaticFirmwareUpdater_homebrew.app"
```

Omitting the output argument creates `ChromaticFirmwareUpdater_homebrew.app` beside the input app, which requires write permission in that directory. Existing output files, apps and symlinks are rejected; choose a new output name or move your previous patched copy first.

The local signature is not a ModRetro Developer ID signature, and the modified app is not notarized by Apple. macOS may require approval through **System Settings > Privacy & Security > Open Anyway** before its first launch; see [Apple's app-opening instructions](https://support.apple.com/en-us/102445). The local signature also clears the copied app's hardened-runtime flags so its embedded vendor-signed libraries can load. These changes apply only to the output app.

### macOS compatibility

- Verified against **ChromaticFirmwareUpdater v1.7.5, Apple Silicon (arm64)**: both bytecode patches, code signing, and app launch. Cartridge flashing was not tested.
- Thin 64-bit Intel executables are accepted by the same patch path and covered by synthetic archive tests; an Intel app has not been tested on hardware.
- Universal/fat executables, other Python bytecode versions, and compressed outer PYZ archives are rejected. The supported app embeds Python 3.10 bytecode.
- The patched module must compress to fit its existing allocation. If it does not, patching stops without creating an output app.

## GUI (optional)

With a Python installation that includes Tkinter, run without arguments:

```sh
python patch_mrupdater.py
```

1. Select the original executable or, on macOS, the `.app` bundle. You can also enter its full path in **Original updater**.
2. Choose a new path for the patched copy. The default adds `_homebrew` to the original name.
3. Click **Patch!** and wait for the success dialog.

Command-line mode works without Tkinter. Run `python patch_mrupdater.py --help` for argument help.

## Flashing a ROM

1. Switch on your Chromatic and connect it to your computer via USB.
2. Open the patched executable or app.
3. Wait for the Chromatic's firmware version to be detected, then navigate to **Cart Clinic**.
4. Click **HOMEBREW**.
5. Select your `.gb` or `.gbc` ROM and wait for flashing to complete.

Cart Clinic requires a connected, detected Chromatic before its controls can be checked.

## What the patcher does

Both updater versions are PyInstaller-bundled Python apps. Cart Clinic contains a HOMEBREW button and ROM flashing subprocess behind an internal `developer_mode` flag. The patcher applies two targeted bytecode changes to `cartclinic.gui` inside the embedded PYZ archive:

| Function | Change |
|----------|--------|
| `CartClinic.update_screens()` | Removes the conditional that hides the HOMEBREW button |
| `CartClinic.cart_clinic_homebrew()` | Removes the early-return guard that blocks the ROM file picker |

Each byte pattern must occur exactly once, or patching fails.

On Windows, the patcher rebuilds the PYZ and outer CArchive as needed. On macOS, it recompresses only the patched module and pads unused space in its original allocation. This preserves the executable's size, Mach-O load commands, archive tables, and other modules before code signing. Code signing then updates the copied app's signature and resource seal.

## Re-patching after an update

Run the patcher against the new **original** updater after a ModRetro update. Already-patched apps and incompatible bytecode layouts are rejected.

## Development checks

```sh
python3 -m unittest -v
```

Tests use synthetic archives and require no proprietary updater files or connected device. They cover native archive preservation, both patches, unsupported layouts, output protection, signing-failure cleanup, and the Windows CLI without Tkinter.

See [CHANGELOG.md](CHANGELOG.md) for changes.

## Disclaimer

This tool is for personal/educational use with ROMs you own. It modifies a local copy of a third-party application. The authors accept no liability for damages arising from use of this tool. Use at your own risk.
