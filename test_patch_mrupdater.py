"""Archive preservation, output safety and CLI regression tests.

Run with: python3 -m unittest -v
Fixtures contain synthetic data, not ModRetro application code.
"""

import contextlib
import io
import marshal
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zlib

import patch_mrupdater as patcher


def archive(module=None, cpu=0x0100000C, pyver=310, outer_compressed=False):
    if module is None:
        module = b"synthetic module" + b"".join(item[0] for item in patcher.PATCHES)
    compressed = zlib.compress(module) + b"\0" * 128
    other = zlib.compress(b"another module, unchanged")
    module_offset = 12
    toc_offset = module_offset + len(compressed) + len(other)
    toc = {
        patcher.TARGET_MODULE: (0, module_offset, len(compressed)),
        "unrelated": (0, module_offset + len(compressed), len(other)),
    }
    pyz = (patcher.PYZ_MAGIC + patcher.PYTHON310_MAGIC
           + struct.pack("!i", toc_offset) + compressed + other + marshal.dumps(toc, 2))
    payload = zlib.compress(pyz) if outer_compressed else pyz
    name = b"PYZ.pyz\0"
    entry = struct.pack("!iIIIBc", 18 + len(name), 0, len(payload), len(pyz),
                        int(outer_compressed), b"z") + name
    cookie = struct.pack("!8sIIii64s", patcher.COOKIE_MAGIC,
                         len(payload) + len(entry) + 88, len(payload), len(entry),
                         pyver, b"Python")
    macho = struct.pack("<IiiIIIII", 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0)
    prefix = macho + b"native load commands and padding" * 2
    return prefix + payload + entry + cookie + b"original native signature"


def expected_module(module):
    for before, after, _ in patcher.PATCHES:
        module = module.replace(before, after)
    return module


class QuietTest(unittest.TestCase):
    def setUp(self):
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)


class MachOTests(QuietTest):
    def test_both_patches_preserve_every_byte_outside_module_allocation(self):
        for cpu in (0x0100000C, 0x01000007):
            with self.subTest(cpu=cpu):
                original = archive(cpu=cpu)
                start, size, module = patcher.macho_module(original)
                patched = patcher.patch_macho(original)
                self.assertEqual(len(patched), len(original))
                self.assertEqual(patched[:start], original[:start])
                self.assertEqual(patched[start + size:], original[start + size:])
                self.assertEqual(patcher.macho_module(patched),
                                 (start, size, expected_module(module)))

    def test_repatching_is_rejected(self):
        patched = patcher.patch_macho(archive())
        with self.assertRaisesRegex(ValueError, "NOT FOUND"):
            patcher.patch_macho(patched)

    def test_missing_or_ambiguous_patterns_are_rejected(self):
        first, second = (item[0] for item in patcher.PATCHES)
        for module in (first, first + first + second):
            with self.subTest(module=module), self.assertRaises(ValueError):
                patcher.patch_macho(archive(module=module))

    def test_unsupported_python_version_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Python 3.10"):
            patcher.patch_macho(archive(pyver=311))

    def test_universal_binary_is_rejected(self):
        for magic in patcher.FAT_MAGICS:
            with self.subTest(magic=magic), self.assertRaisesRegex(ValueError, "Universal"):
                patcher.patch_macho(magic + archive()[4:])

    def test_compressed_outer_archive_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Compressed outer PYZ"):
            patcher.patch_macho(archive(outer_compressed=True))

    def test_module_growth_aborts_without_rebuilding_macho(self):
        original = archive()
        _, size, _ = patcher.macho_module(original)
        with mock.patch.object(patcher.zlib, "compress", return_value=b"x" * (size + 1)):
            with self.assertRaisesRegex(ValueError, "does not fit"):
                patcher.patch_macho(original)

    def test_invalid_archive_bounds_are_rejected(self):
        original = bytearray(archive())
        cookie = patcher.find_cookie(original)
        struct.pack_into("!I", original, cookie + 12, len(original) * 2)
        with self.assertRaisesRegex(ValueError, "bounds"):
            patcher.patch_macho(bytes(original))

    def test_overlapping_module_allocations_are_rejected(self):
        original = archive()
        start, _, _ = patcher.macho_module(original)
        # Move the unrelated module into the target's allocation in the PYZ TOC.
        pyz_start = start - 12
        toc_start = pyz_start + struct.unpack_from("!i", original, pyz_start + 8)[0]
        cookie = patcher.find_cookie(original)
        pkg_size, toc_offset = struct.unpack_from("!II", original, cookie + 8)
        outer_toc_start = cookie + 88 - pkg_size + toc_offset
        toc = marshal.loads(original[toc_start:outer_toc_start])
        kind, _, length = toc["unrelated"]
        toc["unrelated"] = (kind, 12, length)
        replacement = marshal.dumps(toc, 2)
        self.assertEqual(len(replacement), outer_toc_start - toc_start)
        malformed = original[:toc_start] + replacement + original[outer_toc_start:]
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            patcher.patch_macho(malformed)


class AppTests(QuietTest):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "Original Updater.app"
        self.destination = self.directory / "Homebrew Updater.app"
        self.binary = self.source / "Contents/MacOS/Updater"
        self.binary.parent.mkdir(parents=True)
        self.original = archive()
        self.binary.write_bytes(self.original)
        self.binary.chmod(0o755)
        with (self.source / "Contents/Info.plist").open("wb") as stream:
            plistlib.dump({"CFBundleExecutable": "Updater"}, stream)
        try:
            (self.source / "Contents/Current").symlink_to("MacOS", target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"App bundle tests require directory symlink support: {exc}")
        self.commands = []

    def run_tool(self, command, check):
        self.assertTrue(check)
        self.commands.append(command)
        if command[0] == "ditto":
            shutil.copytree(command[1], command[2], symlinks=True)

    def patch_app(self, side_effect=None):
        with mock.patch.object(patcher.sys, "platform", "darwin"), \
                mock.patch.object(patcher.shutil, "which", return_value="tool"), \
                mock.patch.object(patcher.subprocess, "run",
                                  side_effect=side_effect or self.run_tool):
            patcher.patch(str(self.source), str(self.destination))

    def test_app_is_copied_patched_signed_and_verified_without_changing_source(self):
        self.patch_app()
        self.assertEqual(self.binary.read_bytes(), self.original)
        result = self.destination / "Contents/MacOS/Updater"
        self.assertNotEqual(result.read_bytes(), self.original)
        self.assertEqual(result.stat().st_mode & 0o777, 0o755)
        self.assertTrue((self.destination / "Contents/Current").is_symlink())
        self.assertEqual([command[0] for command in self.commands],
                         ["ditto", "codesign", "codesign"])
        self.assertIn("--sign", self.commands[1])
        self.assertIn("--strict", self.commands[2])

    def test_signing_failure_cleans_staging_and_leaves_source_untouched(self):
        def fail_sign(command, check):
            self.run_tool(command, check)
            if command[0] == "codesign":
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.patch_app(fail_sign)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.directory.iterdir()), [self.source])
        self.assertEqual(self.binary.read_bytes(), self.original)

    def test_verification_failure_does_not_publish_output(self):
        def fail_verification(command, check):
            self.run_tool(command, check)
            if "--verify" in command:
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.patch_app(fail_verification)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.binary.read_bytes(), self.original)

    def test_existing_output_and_original_cannot_be_overwritten(self):
        self.destination.mkdir()
        marker = self.destination / "keep.txt"
        marker.write_text("preserve this")
        for destination in (self.destination, self.source):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                patcher.patch(str(self.source), str(destination))
        self.assertEqual(marker.read_text(), "preserve this")
        self.assertEqual(self.binary.read_bytes(), self.original)

    def test_broken_output_symlink_is_not_followed(self):
        self.destination.symlink_to(self.directory / "missing.app")
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.patch_app()
        self.assertTrue(self.destination.is_symlink())

    def test_output_inside_original_app_is_rejected(self):
        self.destination = self.source / "nested.app"
        with self.assertRaisesRegex(ValueError, "separate"):
            self.patch_app()

    def test_inner_macho_cannot_use_windows_rebuild_path(self):
        with self.assertRaisesRegex(ValueError, "whole .app"):
            patcher.patch(str(self.binary), str(self.directory / "output"))

    def test_non_macos_host_is_rejected(self):
        with mock.patch.object(patcher.sys, "platform", "linux"):
            with self.assertRaisesRegex(ValueError, "on a Mac"):
                patcher.patch(str(self.source), str(self.destination))

    def test_missing_codesign_is_rejected_before_copying(self):
        with mock.patch.object(patcher.sys, "platform", "darwin"), \
                mock.patch.object(patcher.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "tool not found"):
                patcher.patch(str(self.source), str(self.destination))
        self.assertFalse(self.destination.exists())


class CommandLineTests(QuietTest):
    def test_default_output_names_keep_extensions_and_spaces(self):
        for source, destination in (
            ("MRUpdater.exe", "MRUpdater_homebrew.exe"),
            ("My Updater.app", "My Updater_homebrew.app"),
        ):
            self.assertEqual(patcher.default_output_path(source), Path(destination))

    def test_windows_cli_works_without_tkinter(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "MRUpdater.exe"
            source.write_bytes(b"MZ!!" + archive()[4:])
            original = source.read_bytes()
            script = Path(patcher.__file__).resolve()
            code = ("import runpy, sys; sys.modules['tkinter'] = None; "
                    "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
            result = subprocess.run([sys.executable, "-c", code, str(script), str(source)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(source.read_bytes(), original)
            output = patcher.default_output_path(str(source)).read_bytes()
            overlay, toc_offset, toc_size = patcher.parse_carchive(
                output, patcher.find_cookie(output)
            )
            entries = patcher.parse_toc(output, toc_offset, toc_size, overlay)
            start, size, _, _, _, _ = entries[0]
            pyz = output[start:start + size]
            _, toc = patcher.parse_pyz(pyz)
            self.assertEqual(patcher.extract_module(pyz, toc, patcher.TARGET_MODULE),
                             expected_module(patcher.macho_module(archive())[2]))


if __name__ == "__main__":
    unittest.main()
