#!/usr/bin/env python3
"""
MRUpdater Homebrew Patcher
Enables the hidden HOMEBREW button in MRUpdater's Cart Clinic by patching
the developer_mode feature gate in cartclinic/gui.pyc inside PYZ.pyz.

Usage:  python patch_mrupdater.py MRUpdater.exe
Output: MRUpdater_homebrew.exe (same directory)

How it works:
  - Inside MRUpdater.exe is a PyInstaller CArchive (PKG).
  - The PKG contains PYZ.pyz, a zlib-per-module archive of all Python bytecode.
  - cartclinic/gui.pyc contains CartClinic.update_screens() which checks
    self.developer_mode before adding the cc_homebrew widget to the display list.
  - We patch the POP_JUMP_IF_FALSE (which skips showing cc_homebrew when the
    feature flag is False) to NOP + POP_TOP (always falls through).
  - The 8-byte patch: 7c006a2690017222 -> 7c006a2609000100
"""

import struct
import zlib
import marshal
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ──────────────────────────────────────────────
# 1.  CArchive (outer PKG) constants
# ──────────────────────────────────────────────
COOKIE_MAGIC = b"MEI\014\013\012\013\016"
PYINST20_COOKIE_SIZE = 24   # !8siiii
PYINST21_COOKIE_SIZE = 88   # !8sIIii64s

# ──────────────────────────────────────────────
# 2.  PYZ constants
# ──────────────────────────────────────────────
PYZ_MAGIC = b"PYZ\x00"

# ──────────────────────────────────────────────
# 3.  The 8-byte bytecode patch
#     In CartClinic.update_screens:
#       LOAD_FAST(self=0), LOAD_ATTR(developer_mode=38),
#       EXTENDED_ARG(1),   POP_JUMP_IF_FALSE(34)   -> target 580 (word 290)
#
#     After patch:
#       LOAD_FAST(self=0), LOAD_ATTR(developer_mode=38),
#       NOP(0),            POP_TOP(0)               -> always fall through
# ──────────────────────────────────────────────
PATCHES = [
    (
        bytes([0x7c, 0x00, 0x6a, 0x26, 0x90, 0x01, 0x72, 0x22]),
        bytes([0x7c, 0x00, 0x6a, 0x26, 0x09, 0x00, 0x01, 0x00]),
        "update_screens: skip developer_mode jump -> always show HOMEBREW button",
    ),
    (
        bytes([0x7c, 0x00, 0x6a, 0x00, 0x73, 0x05]),
        bytes([0x09, 0x00, 0x09, 0x00, 0x71, 0x05]),
        "cart_clinic_homebrew: skip developer_mode guard -> always open file dialog",
    ),
]
TARGET_MODULE = "cartclinic.gui"


# ──────────────────────────────────────────────
# CArchive helpers
# ──────────────────────────────────────────────

def find_cookie(data: bytes) -> int:
    """Return the offset of the PyInstaller cookie in the exe bytes."""
    idx = data.rfind(COOKIE_MAGIC)
    if idx == -1:
        raise ValueError("PyInstaller cookie not found - not a PyInstaller exe?")
    return idx


def parse_carchive(data: bytes, cookie_pos: int):
    """Return CArchive metadata: (overlay_pos, toc_list)."""
    # Try 2.1
    if len(data) >= cookie_pos + PYINST21_COOKIE_SIZE:
        magic, length_of_pkg, toc_offset, toc_len, pyver, pylibname = struct.unpack_from(
            "!8sIIii64s", data, cookie_pos)
        if magic == COOKIE_MAGIC:
            pyinst_ver = 21
            file_size = len(data)
            tail_bytes = file_size - cookie_pos - PYINST21_COOKIE_SIZE
            overlay_size = length_of_pkg + tail_bytes
            overlay_pos = file_size - overlay_size
            toc_pos = overlay_pos + toc_offset
            print(f"  PyInstaller 2.1+, Python {pyver//100}.{pyver%100}")
            print(f"  Overlay @ 0x{overlay_pos:08x}, TOC @ 0x{toc_pos:08x} (len={toc_len})")
            return overlay_pos, toc_pos, toc_len

    # Try 2.0
    magic, length_of_pkg, toc_offset, toc_len, pyver = struct.unpack_from(
        "!8siiii", data, cookie_pos)
    if magic == COOKIE_MAGIC:
        file_size = len(data)
        tail_bytes = file_size - cookie_pos - PYINST20_COOKIE_SIZE
        overlay_size = length_of_pkg + tail_bytes
        overlay_pos = file_size - overlay_size
        toc_pos = overlay_pos + toc_offset
        print(f"  PyInstaller 2.0, Python {pyver//100}.{pyver%100}")
        return overlay_pos, toc_pos, toc_len

    raise ValueError("Failed to parse CArchive cookie")


def parse_toc(data: bytes, toc_pos: int, toc_len: int, overlay_pos: int):
    """Return list of (abs_file_pos, cmprs_size, uncmprs_size, flag, type_byte, name)."""
    entries = []
    pos = toc_pos
    end = toc_pos + toc_len
    entry_header_size = struct.calcsize("!IIIBc")  # 13 bytes: pos,cmprs,uncmprs,flag,type

    while pos < end:
        (entry_size,) = struct.unpack_from("!i", data, pos)
        if entry_size <= 0:
            break
        name_len = entry_size - 4 - entry_header_size
        (entry_pos, cmprs_size, uncmprs_size, flag, type_byte) = struct.unpack_from(
            "!IIIBc", data, pos + 4)
        name_raw = data[pos + 4 + entry_header_size : pos + 4 + entry_header_size + name_len]
        name = name_raw.rstrip(b"\x00").decode("utf-8", errors="replace")

        abs_pos = overlay_pos + entry_pos
        entries.append((abs_pos, cmprs_size, uncmprs_size, flag, type_byte, name))
        pos += entry_size

    return entries


# ──────────────────────────────────────────────
# PYZ helpers
# ──────────────────────────────────────────────

def parse_pyz(pyz_data: bytes):
    """Parse PYZ.pyz bytes.
    Returns (pyc_magic, toc_dict) where toc_dict = {name: (ispkg, pos, length)}.
    pos is absolute within pyz_data.
    """
    assert pyz_data[:4] == PYZ_MAGIC, "Not a PYZ archive"
    pyc_magic = pyz_data[4:8]
    (toc_position,) = struct.unpack_from("!i", pyz_data, 8)
    
    # Unmarshal the TOC
    # Use Python's marshal since the data from the file was marshaled at runtime
    # The TOC is a dict or list of (name, (ispkg, pos, length))
    raw_toc = pyz_data[toc_position:]
    toc_obj = marshal.loads(raw_toc)
    
    if isinstance(toc_obj, list):
        toc_dict = dict(toc_obj)
    elif isinstance(toc_obj, dict):
        toc_dict = toc_obj
    else:
        raise ValueError(f"Unexpected TOC type: {type(toc_obj)}")
    
    return pyc_magic, toc_dict


def extract_module(pyz_data: bytes, toc_dict: dict, name: str) -> bytes:
    """Return the decompressed marshal bytes for a module."""
    entry = toc_dict.get(name)
    if entry is None:
        raise KeyError(f"Module {name!r} not found in PYZ TOC")
    ispkg, pos, length = entry
    raw = pyz_data[pos : pos + length]
    return zlib.decompress(raw)


def patch_module_bytes(module_bytes: bytes, search: bytes, replace: bytes) -> bytes:
    """Find+replace exactly once in raw marshal bytes."""
    count = module_bytes.count(search)
    if count == 0:
        raise ValueError(f"Search pattern {search.hex()} NOT FOUND in module bytes")
    if count > 1:
        raise ValueError(f"Search pattern found {count} times -- ambiguous, aborting")
    patched = module_bytes.replace(search, replace, 1)
    print(f"  Patched 1 occurrence of {search.hex()} -> {replace.hex()}")
    return patched


def rebuild_pyz(pyc_magic: bytes, toc_dict: dict, modules: dict) -> bytes:
    """
    Rebuild PYZ.pyz bytes.
    modules: {name: compressed_bytes}  - same dict as toc_dict but with patched data.
    """
    # Header is 12 bytes: PYZ_MAGIC (4) + pyc_magic (4) + toc_position (4, big-endian)
    HEADER_SIZE = 12
    
    # Build data section, tracking positions
    data_parts = []
    new_toc = {}
    current_pos = HEADER_SIZE  # data starts right after header
    
    # Preserve original ordering by iterating toc_dict
    for name, (ispkg, orig_pos, orig_len) in sorted(toc_dict.items(), key=lambda x: x[1][1]):
        compressed = modules[name]
        new_toc[name] = (ispkg, current_pos, len(compressed))
        data_parts.append(compressed)
        current_pos += len(compressed)
    
    toc_position = current_pos  # TOC starts right after data section
    
    # Marshal TOC with version=2 for maximum compatibility
    try:
        toc_bytes = marshal.dumps(new_toc, 2)
    except TypeError:
        toc_bytes = marshal.dumps(new_toc)
    
    # Build the PYZ
    header = PYZ_MAGIC + pyc_magic + struct.pack("!i", toc_position)
    result = header + b"".join(data_parts) + toc_bytes
    return result


def rebuild_carchive(exe_data: bytes, overlay_pos: int, toc_entries: list,
                     old_pyz_abs_pos: int, old_pyz_cmprs_size: int,
                     new_pyz_data: bytes) -> bytes:
    """
    Rebuild the CArchive in the exe with a new PYZ.pyz.
    Returns the new exe bytes.
    """
    size_delta = len(new_pyz_data) - old_pyz_cmprs_size
    print(f"  PYZ size: old={old_pyz_cmprs_size}, new={len(new_pyz_data)}, delta={size_delta:+d}")
    
    if size_delta == 0:
        # Simple in-place replacement
        result = bytearray(exe_data)
        result[old_pyz_abs_pos : old_pyz_abs_pos + old_pyz_cmprs_size] = new_pyz_data
        print("  Same-size patch applied")
        return bytes(result)
    
    # Size changed: need to rebuild the overlay section
    # Split exe into: pre-overlay + overlay
    pre_overlay = exe_data[:overlay_pos]
    overlay = bytearray(exe_data[overlay_pos:])
    
    # The overlay is structured as:
    #   [file data for entry0, entry1, ...entry_n] [TOC entries] [cookie]
    # We need to:
    # 1. Replace PYZ data at its position within the overlay
    # 2. Adjust entryPos for all entries AFTER the PYZ entry
    # 3. Update the PYZ entry's cmprs_size and uncmprs_size
    # 4. Rebuild the TOC
    # 5. Update cookie's lengthOfPackage and toc offset
    
    pyz_overlay_pos = old_pyz_abs_pos - overlay_pos  # relative to overlay start
    
    # 1. Replace PYZ in overlay data section
    new_overlay = (overlay[:pyz_overlay_pos] +
                   bytearray(new_pyz_data) +
                   overlay[pyz_overlay_pos + old_pyz_cmprs_size:])
    
    # 2&3. Rebuild TOC entries
    # Find cookie within original overlay to get toc_pos relative to overlay
    cookie_rel = exe_data.rfind(COOKIE_MAGIC) - overlay_pos
    
    # Rebuild each TOC entry with adjusted positions
    new_toc_bytes = bytearray()
    for abs_pos, cmprs_size, uncmprs_size, flag, type_byte, name in toc_entries:
        entry_pos = abs_pos - overlay_pos
        # Adjust positions for entries that come AFTER the patched PYZ
        if abs_pos > old_pyz_abs_pos:
            entry_pos += size_delta
        # Update PYZ entry itself
        if abs_pos == old_pyz_abs_pos:
            cmprs_size = len(new_pyz_data)
            uncmprs_size = len(new_pyz_data)  # PYZ is stored uncompressed (flag=0)
        
        name_bytes = name.encode("utf-8") + b"\x00"
        entry_header_size = struct.calcsize("!IIIBc")
        entry_size = 4 + entry_header_size + len(name_bytes)
        entry = struct.pack("!iIIIBc", entry_size, entry_pos, cmprs_size, uncmprs_size,
                           flag, type_byte) + name_bytes
        new_toc_bytes += entry
    
    # Figure out where TOC was in the original overlay
    # We need to find original toc_pos and toc_len from the cookie
    cookie_abs = exe_data.rfind(COOKIE_MAGIC)
    # Re-parse cookie to get original values
    try:
        magic, length_of_pkg, toc_offset, toc_len, pyver, pylibname = struct.unpack_from(
            "!8sIIii64s", exe_data, cookie_abs)
        cookie_fmt = "!8sIIii64s"
        cookie_size = PYINST21_COOKIE_SIZE
    except Exception:
        magic, length_of_pkg, toc_offset, toc_len, pyver = struct.unpack_from(
            "!8siiii", exe_data, cookie_abs)
        cookie_fmt = "!8siiii"
        cookie_size = PYINST20_COOKIE_SIZE
    
    # Original TOC position (relative to overlay start)
    orig_toc_rel = toc_offset  # toc_offset is relative to overlay_pos
    # New overlay: keep everything before TOC, then new TOC, then cookie
    data_section_end = orig_toc_rel  # data section ends where TOC begins
    
    # But with size_delta, data section is now longer/shorter
    new_data_section_end = data_section_end + size_delta
    
    # The overlay is: [data section] [TOC] [cookie + anything after]
    # We want: [new data section with PYZ replaced] [new TOC] [cookie]
    
    # Extract cookie from original overlay
    cookie_rel_pos = cookie_abs - overlay_pos
    # Everything after TOC up to cookie (should just be the cookie)
    after_toc = overlay[orig_toc_rel + toc_len : cookie_rel_pos]
    
    # New overlay = new data section + new TOC + after_toc + cookie (updated)
    new_data_section = new_overlay[:new_data_section_end]
    
    new_toc_len = len(new_toc_bytes)
    new_toc_offset = new_data_section_end  # relative to overlay start
    new_length_of_pkg = new_data_section_end + new_toc_len + len(after_toc) + cookie_size
    
    # Rebuild cookie
    if cookie_fmt == "!8sIIii64s":
        new_cookie = struct.pack(cookie_fmt, magic, new_length_of_pkg,
                                new_toc_offset, new_toc_len, pyver, pylibname)
    else:
        new_cookie = struct.pack(cookie_fmt, magic, new_length_of_pkg,
                                new_toc_offset, new_toc_len, pyver)
    
    new_overlay_bytes = new_data_section + new_toc_bytes + after_toc + new_cookie
    return pre_overlay + bytes(new_overlay_bytes)


# ──────────────────────────────────────────────
# Main patcher
# ──────────────────────────────────────────────

def patch(input_path: str, output_path: str):
    print(f"Reading {input_path}...")
    with open(input_path, "rb") as f:
        exe_data = f.read()
    print(f"  File size: {len(exe_data):,} bytes")

    # 1. Find cookie
    print("\n[1] Locating PyInstaller cookie...")
    cookie_pos = find_cookie(exe_data)
    print(f"  Cookie @ 0x{cookie_pos:08x}")

    # 2. Parse CArchive
    print("\n[2] Parsing CArchive...")
    overlay_pos, toc_pos, toc_len = parse_carchive(exe_data, cookie_pos)

    # 3. Parse TOC
    print("\n[3] Parsing CArchive TOC...")
    entries = parse_toc(exe_data, toc_pos, toc_len, overlay_pos)
    for e in entries:
        abs_pos, cs, us, flag, tb, name = e
        print(f"  {name:40s}  type={tb.decode()}  size={cs:>8,}  flag={flag}")

    # 4. Find PYZ entry
    print(f"\n[4] Locating PYZ.pyz entry...")
    pyz_entry = None
    for e in entries:
        abs_pos_e, cs_e, us_e, flag_e, tb_e, name_e = e
        if tb_e in (b"z", b"Z") or name_e.endswith(".pyz") or name_e == "PYZ-00.pyz":
            pyz_entry = e
            print(f"  Found: {name_e} @ 0x{abs_pos_e:08x} size={cs_e}")
            break
    
    if pyz_entry is None:
        raise ValueError("Could not find PYZ archive entry in CArchive TOC")

    pyz_abs_pos, pyz_cmprs_size, pyz_uncmprs_size, pyz_flag, pyz_type, pyz_name = pyz_entry

    # 5. Extract PYZ data
    print(f"\n[5] Extracting PYZ data...")
    pyz_data = exe_data[pyz_abs_pos : pyz_abs_pos + pyz_cmprs_size]
    if pyz_flag == 1:
        print("  Decompressing with zlib...")
        pyz_data = zlib.decompress(pyz_data)
    print(f"  PYZ size: {len(pyz_data):,} bytes")

    # 6. Parse PYZ TOC
    print(f"\n[6] Parsing PYZ TOC...")
    pyc_magic, pyz_toc = parse_pyz(pyz_data)
    print(f"  Python magic: {pyc_magic.hex()}")
    print(f"  Modules in PYZ: {len(pyz_toc)}")

    if TARGET_MODULE not in pyz_toc:
        raise ValueError(f"Target module {TARGET_MODULE!r} not found in PYZ TOC")
    ispkg, mod_pos, mod_len = pyz_toc[TARGET_MODULE]
    print(f"  {TARGET_MODULE}: ispkg={ispkg}, pos={mod_pos}, compressed_len={mod_len}")

    # 7. Extract+decompress target module
    print(f"\n[7] Extracting {TARGET_MODULE}...")
    module_bytes = extract_module(pyz_data, pyz_toc, TARGET_MODULE)
    print(f"  Decompressed size: {len(module_bytes):,} bytes")

    # Verify all patterns exist exactly once
    for search, replace, desc in PATCHES:
        count = module_bytes.count(search)
        print(f"  Pattern [{desc}]")
        print(f"    {search.hex()} appears {count} time(s)")
        if count != 1:
            raise ValueError(f"Expected 1 occurrence of {search.hex()}, found {count}")

    # 8. Apply all patches
    print(f"\n[8] Applying {len(PATCHES)} patches...")
    patched_module = module_bytes
    for search, replace, desc in PATCHES:
        patched_module = patch_module_bytes(patched_module, search, replace)

    # 9. Rebuild all modules dict for PYZ
    print(f"\n[9] Rebuilding PYZ...")
    all_modules = {}
    for name, (ispkg, pos, length) in pyz_toc.items():
        raw_compressed = pyz_data[pos : pos + length]
        if name == TARGET_MODULE:
            # Use patched version
            recompressed = zlib.compress(patched_module, 6)
            print(f"  {name}: recompressed {len(module_bytes)} -> {len(recompressed)} bytes "
                  f"(was {length})")
            all_modules[name] = recompressed
        else:
            all_modules[name] = raw_compressed  # keep original compressed bytes

    new_pyz = rebuild_pyz(pyc_magic, pyz_toc, all_modules)
    print(f"  New PYZ size: {len(new_pyz):,} (old: {len(pyz_data):,})")

    # If PYZ was originally zlib-compressed in PKG, recompress it
    if pyz_flag == 1:
        print("  Recompressing PYZ for PKG storage...")
        new_pyz = zlib.compress(new_pyz, 6)

    # 10. Rebuild exe
    print(f"\n[10] Rebuilding executable...")
    new_exe = rebuild_carchive(exe_data, overlay_pos, entries,
                               pyz_abs_pos, pyz_cmprs_size, new_pyz)
    print(f"  New exe size: {len(new_exe):,} (old: {len(exe_data):,})")

    # 11. Write output
    print(f"\n[11] Writing {output_path}...")
    with open(output_path, "wb") as f:
        f.write(new_exe)
    print("  Done!")
    print(f"\nPatched executable: {output_path}")
    print("The HOMEBREW button in Cart Clinic is now enabled.")


# ──────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────

class PatcherGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("MRUpdater Homebrew Patcher")
        root.resizable(False, False)

        # ── Description ──
        description = (
            "Unlocks the hidden HOMEBREW button in MRUpdater's Cart Clinic.\n"
            "1. Browse for your MRUpdater.exe.\n"
            "2. Choose where to save the patched copy (default: MRUpdater_homebrew.exe).\n"
            "3. Click Patch!  — your original file is never modified."
        )
        frm_desc = ttk.LabelFrame(root, text="About", padding=8)
        frm_desc.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(frm_desc, text=description, justify="left").pack(anchor="w")

        # ── Input file ──
        frm_input = ttk.LabelFrame(root, text="Input", padding=8)
        frm_input.pack(fill="x", padx=12, pady=4)

        ttk.Label(frm_input, text="MRUpdater.exe:").grid(row=0, column=0, sticky="w")
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(frm_input, textvariable=self.input_var, width=52)
        self.input_entry.grid(row=0, column=1, padx=(6, 4), sticky="ew")
        ttk.Button(frm_input, text="Browse…", command=self._browse_input).grid(row=0, column=2)
        frm_input.columnconfigure(1, weight=1)

        # ── Output file ──
        frm_output = ttk.LabelFrame(root, text="Output", padding=8)
        frm_output.pack(fill="x", padx=12, pady=4)

        ttk.Label(frm_output, text="Patched exe:").grid(row=0, column=0, sticky="w")
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(frm_output, textvariable=self.output_var, width=52)
        self.output_entry.grid(row=0, column=1, padx=(6, 4), sticky="ew")
        ttk.Button(frm_output, text="Browse…", command=self._browse_output).grid(row=0, column=2)
        frm_output.columnconfigure(1, weight=1)

        # ── Progress bar ──
        frm_prog = ttk.Frame(root, padding=(12, 4))
        frm_prog.pack(fill="x")
        self.progress = ttk.Progressbar(frm_prog, mode="indeterminate", length=480)
        self.progress.pack(fill="x")

        # ── Status label ──
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(root, textvariable=self.status_var, anchor="center").pack(pady=(2, 4))

        # ── Patch button ──
        self.patch_btn = ttk.Button(root, text="Patch!", command=self._run_patch, width=20)
        self.patch_btn.pack(pady=(0, 8))

        # ── Disclaimer ──
        disclaimer = (
            "⚠  For personal / educational use only.  "
            "The authors accept no liability for damages arising from use of this tool."
        )
        frm_disc = ttk.Frame(root, padding=(12, 0, 12, 10))
        frm_disc.pack(fill="x")
        ttk.Label(frm_disc, text=disclaimer, justify="center",
                  foreground="#b05000").pack(anchor="center")

    def _browse_input(self):
        path = filedialog.askopenfilename(
            title="Select MRUpdater.exe",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.input_var.set(path)
            if not self.output_var.get():
                p = Path(path)
                self.output_var.set(str(p.with_stem(p.stem + "_homebrew")))

    def _browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Save patched exe as",
            defaultextension=".exe",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if path:
            self.output_var.set(path)

    def _run_patch(self):
        inp = self.input_var.get().strip()
        out = self.output_var.get().strip()

        if not inp:
            messagebox.showwarning("Missing input", "Please select the input MRUpdater.exe.")
            return
        if not out:
            messagebox.showwarning("Missing output", "Please specify the output path.")
            return

        self.patch_btn.configure(state="disabled")
        self.status_var.set("Patching…")
        self.progress.start(12)

        threading.Thread(target=self._patch_thread, args=(inp, out), daemon=True).start()

    def _patch_thread(self, inp: str, out: str):
        try:
            patch(inp, out)
            self.root.after(0, self._on_success)
        except Exception as exc:
            self.root.after(0, self._on_error, str(exc))

    def _on_success(self):
        self.progress.stop()
        self.progress["value"] = 100
        self.status_var.set("Done! Patch applied successfully.")
        self.patch_btn.configure(state="normal")
        messagebox.showinfo("Success",
            "Patched successfully!\nThe HOMEBREW button in Cart Clinic is now enabled.")
        self.progress["value"] = 0

    def _on_error(self, msg: str):
        self.progress.stop()
        self.progress["value"] = 0
        self.status_var.set(f"Error: {msg}")
        self.patch_btn.configure(state="normal")
        messagebox.showerror("Patch failed", msg)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        root = tk.Tk()
        PatcherGUI(root)
        root.mainloop()
    else:
        inp = sys.argv[1]
        if len(sys.argv) >= 3:
            out = sys.argv[2]
        else:
            p = Path(inp)
            out = str(p.with_stem(p.stem + "_homebrew"))
        patch(inp, out)

