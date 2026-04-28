# MRUpdater Homebrew Patcher

Patches `MRUpdater.exe` to unlock the hidden **HOMEBREW** button inside Cart Clinic, letting you flash any `.gb` or `.gbc` ROM directly to a ModRetro Chromatic cartridge.

---

## Requirements

- **Python 3.8+** — [python.org](https://www.python.org/downloads/)  
  (No third-party packages needed — only standard library modules are used)
- Your original `MRUpdater.exe` from ModRetro

---

## Usage

### GUI (recommended)

Run the script with no arguments to open the graphical interface:

```
python patch_mrupdater.py
```

1. Click **Browse…** next to *MRUpdater.exe* and select your original executable.
2. The output path is filled in automatically (`MRUpdater_homebrew.exe` in the same folder). Change it if you like.
3. Click **Patch!** and wait for the success dialog.

### Command line

```
python patch_mrupdater.py MRUpdater.exe
```

This creates `MRUpdater_homebrew.exe` in the same directory.

Specify a custom output path:

```
python patch_mrupdater.py MRUpdater.exe C:\path\to\output.exe
```

---

## Flashing a ROM

1. Connect your Chromatic to your PC via USB.
2. Open `MRUpdater_homebrew.exe`.
3. Navigate to **Cart Clinic**.
4. Click the **HOMEBREW** button (now visible).
5. Select your `.gb` or `.gbc` ROM file in the file picker.
6. Wait for the flash to complete.

---

## What the patcher does

`MRUpdater.exe` is a PyInstaller-bundled Python app. The Cart Clinic screen already contains a fully working HOMEBREW button and ROM flashing subprocess — they are just hidden behind an internal `developer_mode` feature flag that is never enabled in normal builds.

The patcher makes two targeted 6–8 byte changes to the compiled Python bytecode inside the bundle:

| # | Function | Change |
|---|----------|--------|
| 1 | `CartClinic.update_screens()` | Removes the conditional that hid the HOMEBREW button from the UI |
| 2 | `CartClinic.cart_clinic_homebrew()` | Removes the early-return guard that blocked the file picker from opening |

No files are extracted, no DLLs are modified, and the original `MRUpdater.exe` is never overwritten.

---

## Re-patching after an update

If ModRetro releases a new version of MRUpdater, run the patcher again against the new `MRUpdater.exe`. If the bytecode layout changed significantly the patcher will exit with an error rather than apply a bad patch.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## Disclaimer

This tool is for personal/educational use with ROMs you own. It modifies a local copy of a third-party application. The authors accept no liability for damages arising from use of this tool. Use at your own risk.
