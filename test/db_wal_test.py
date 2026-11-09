"""Synthetic SQLite/WAL regression cases; no WeChat process or private data."""
import importlib.util
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin/runtime/python-deps'))
package = sys.modules.setdefault('wechatauto', types.ModuleType('wechatauto'))
logger = sys.modules.setdefault('wechatauto.logger', types.ModuleType('wechatauto.logger'))
if not hasattr(logger, 'wxlog'):
    logger.wxlog = logging.getLogger('wal-offline-test')
spec = importlib.util.spec_from_file_location('wal_test_reader', ROOT / 'plugin/runtime/vendor/wechat_db.py')
db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(db)
PAGE = 4096


def checksum(data, seed=(0, 0), endian='<'):
    words = struct.unpack(endian + str(len(data) // 4) + 'I', data)
    a, b = seed
    for index in range(0, len(words), 2):
        a = (a + words[index] + b) & 0xffffffff
        b = (b + words[index + 1] + a) & 0xffffffff
    return a, b


def wal_bytes(frames, endian='<', salt=b'12345678', encode=lambda page: page):
    magic = 0x377f0682 if endian == '<' else 0x377f0683
    header = struct.pack('>IIII', magic, 3007000, PAGE, 0) + salt
    rolling = checksum(header, endian=endian)
    output = header + struct.pack('>II', *rolling)
    for pgno, size, page in frames:
        page = encode(page)
        head = struct.pack('>II', pgno, size)
        rolling = checksum(head + page, rolling, endian)
        output += head + salt + struct.pack('>II', *rolling) + page
    return output


class WalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wal-test-', dir=ROOT.parent)
        self.folder = Path(self.temporary.name)
        self.base = self.database('base.db', 'original')
        self.committed = self.database('committed.db', 'committed')
        self.pending = self.database('pending.db', 'uncommitted')
        self.target = self.folder / 'merged.db'
        self.target.write_bytes(self.base)
        self.wal = self.folder / 'input.wal'
        self.reader = object.__new__(db.WeChatDB)
        self.decrypt = patch.object(db, '_decrypt_page', side_effect=lambda key, page, number: page)
        self.decrypt.start()

    def tearDown(self):
        self.decrypt.stop()
        self.temporary.cleanup()

    def database(self, name, value, auto_vacuum=False):
        file = self.folder / name
        with sqlite3.connect(file) as connection:
            connection.execute('PRAGMA page_size=4096')
            if auto_vacuum:
                connection.execute('PRAGMA auto_vacuum=FULL')
            connection.execute('CREATE TABLE sample(value)')
            connection.execute('INSERT INTO sample VALUES(?)', (value,))
        connection.close()
        return file.read_bytes()

    def merge(self, frames=None, raw=None, **kwargs):
        self.wal.write_bytes(raw if raw is not None else wal_bytes(frames, **kwargs))
        return self.reader._merge_wal(str(self.target), str(self.wal), b'', 0)

    def value(self):
        with sqlite3.connect(self.target) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            result = connection.execute('SELECT value FROM sample').fetchone()[0]
        connection.close()
        return result

    def tail_frames(self):
        return [(2, 2, self.committed[PAGE:2*PAGE]), (2, 0, self.pending[PAGE:2*PAGE])]

    def test_uncommitted_tail_never_overrides_committed_value(self):
        self.assertEqual(self.merge(self.tail_frames()), 1)
        self.assertEqual(self.value(), 'committed')

    def test_uncommitted_only_wal_leaves_database_identical(self):
        self.assertEqual(self.merge([(2, 0, self.pending[PAGE:2*PAGE])]), 0)
        self.assertEqual(self.target.read_bytes(), self.base)

    def test_checksum_failure_stops_before_later_frames(self):
        frames = self.tail_frames() + [(2, 2, self.pending[PAGE:2*PAGE])]
        raw = bytearray(wal_bytes(frames))
        raw[32 + 4120 + 24 + 100] ^= 1
        self.assertEqual(self.merge(raw=raw), 1)
        self.assertEqual(self.value(), 'committed')

    def test_salt_mismatch_stops_instead_of_skipping_to_later_commit(self):
        raw = bytearray(wal_bytes(self.tail_frames() + [(2, 2, self.pending[PAGE:2*PAGE])]))
        raw[32 + 4120 + 8] ^= 1
        self.assertEqual(self.merge(raw=raw), 1)
        self.assertEqual(self.value(), 'committed')

    def test_truncated_tail_keeps_last_complete_transaction(self):
        raw = wal_bytes(self.tail_frames())
        for missing in (1, PAGE, 4110):
            with self.subTest(missing=missing):
                self.target.write_bytes(self.base)
                self.assertEqual(self.merge(raw=raw[:-missing]), 1)
                self.assertEqual(self.value(), 'committed')

    def test_invalid_header_is_rejected_without_touching_destination(self):
        good = wal_bytes(self.tail_frames())
        for offset in (0, 4, 8, 24):
            raw = bytearray(good)
            raw[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.merge(raw=raw)
            self.assertEqual(self.target.read_bytes(), self.base)
        with self.assertRaises(ValueError):
            self.merge(raw=good[:20])

    def test_big_endian_checksum_wal_is_supported(self):
        self.assertEqual(self.merge(self.tail_frames(), endian='>'), 1)
        self.assertEqual(self.value(), 'committed')

    def test_checksums_cover_encoded_pages_before_decryption(self):
        transform = lambda page: bytes(value ^ 0x5a for value in page)
        with patch.object(db, '_decrypt_page', side_effect=lambda key, page, number: transform(page)):
            self.assertEqual(self.merge(self.tail_frames(), encode=transform), 1)
        self.assertEqual(self.value(), 'committed')

    def test_non_btree_pointer_map_and_overflow_pages_are_preserved(self):
        payload = bytes(range(256)) * 100
        updated = self.database('overflow.db', payload, auto_vacuum=True)
        self.assertEqual(updated[PAGE], 1)  # Valid pointer-map page, not a b-tree page.
        pages = len(updated) // PAGE
        frames = [(i, pages if i == pages else 0, updated[(i-1)*PAGE:i*PAGE]) for i in range(1, pages+1)]
        self.assertEqual(self.merge(frames), pages)
        self.assertEqual(self.value(), payload)

    def test_commit_database_size_can_shrink(self):
        self.target.write_bytes(self.database('large.db', 'x' * 30000))
        self.assertGreater(self.target.stat().st_size, len(self.committed))
        self.merge([(1, 0, self.committed[:PAGE]), (2, 2, self.committed[PAGE:])])
        self.assertEqual(self.target.stat().st_size, 2 * PAGE)
        self.assertEqual(self.value(), 'committed')

    def test_invalid_page_number_stops_at_last_commit(self):
        frames = self.tail_frames()[:1] + [(0, 2, self.pending[PAGE:])]
        self.assertEqual(self.merge(frames), 1)
        self.assertEqual(self.value(), 'committed')

    def test_incremental_start_must_be_a_validated_commit(self):
        self.wal.write_bytes(wal_bytes(self.tail_frames()))
        with self.assertRaises(ValueError):
            self.reader._merge_wal(str(self.target), str(self.wal), b'', 2)
        self.assertEqual(self.target.read_bytes(), self.base)

    def test_generation_change_during_capture_is_rejected_before_apply(self):
        self.wal.write_bytes(wal_bytes(self.tail_frames()))
        original = self.reader._wal_checksum
        calls = 0
        def changed(data, seed, endian):
            nonlocal calls
            calls += 1
            result = original(data, seed, endian)
            if calls == 2:
                with self.wal.open('r+b') as stream:
                    stream.seek(16)
                    stream.write(b'new-salt')
            return result
        with patch.object(self.reader, '_wal_checksum', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'WAL changed'):
                self.reader._merge_wal(str(self.target), str(self.wal), b'', 0)
        self.assertEqual(self.target.read_bytes(), self.base)

    def open_reader(self):
        cache = self.folder / 'cache'
        cache.mkdir()
        self.reader._keys = {'base.db': b'fake-key'}
        self.reader.workdir = str(cache)
        self.reader._db_path = lambda rel: str(self.folder / rel)
        self.reader._decrypt_file = lambda source, target, key: shutil.copyfile(source, target)
        return cache

    def test_checkpoint_mutation_retries_without_publishing_mixed_snapshot(self):
        cache = self.open_reader()
        calls = 0
        def change_main(source, target, key):
            nonlocal calls
            calls += 1
            shutil.copyfile(source, target)
            info = os.stat(source)
            os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
        with patch.object(self.reader, '_decrypt_file', side_effect=change_main), patch.object(db.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '一致快照'):
                self.reader._open_unlocked('base.db')
        self.assertEqual(calls, 4)
        self.assertFalse((cache / 'base.db').exists())
        self.assertEqual(list(cache.glob('*.tmp*')), [])

    def test_old_unverified_cache_is_not_a_failure_fallback(self):
        cache = self.open_reader()
        (cache / 'base.db').write_bytes(self.pending)
        (cache / 'base.db.stamp').write_text('3,0,8192,0,0,0')
        with patch.object(self.reader, '_decrypt_file', side_effect=RuntimeError('synthetic failure')), patch.object(db.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '一致快照'):
                self.reader._open_unlocked('base.db')

    def test_verified_cache_reused_until_wal_generation_changes(self):
        self.open_reader()
        source_wal = self.folder / 'base.db-wal'
        source_wal.write_bytes(wal_bytes(self.tail_frames()))
        original = self.reader._decrypt_file
        with patch.object(self.reader, '_decrypt_file', wraps=original) as decrypt:
            self.reader._open_unlocked('base.db').close()
            self.reader._open_unlocked('base.db').close()
            self.assertEqual(decrypt.call_count, 1)
            before = source_wal.stat()
            source_wal.write_bytes(wal_bytes(self.tail_frames(), salt=b'87654321'))
            os.utime(source_wal, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.reader._open_unlocked('base.db').close()
            self.assertEqual(decrypt.call_count, 2)

    def test_main_replacement_with_same_time_and_size_invalidates_cache(self):
        self.open_reader()
        self.reader._open_unlocked('base.db').close()
        source = self.folder / 'base.db'
        before = source.stat()
        replacement = self.folder / 'replace-main.db'
        replacement.write_bytes(self.committed)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(source)
        after = source.stat()
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))
        self.assertNotEqual(before.st_ino, after.st_ino)
        with self.reader._open_unlocked('base.db') as connection:
            self.assertEqual(connection.execute('SELECT value FROM sample').fetchone()[0], 'committed')
        connection.close()

    def test_wal_replacement_with_same_header_time_and_size_invalidates_cache(self):
        self.open_reader()
        source_wal = self.folder / 'base.db-wal'
        source_wal.write_bytes(wal_bytes([(2, 2, self.committed[PAGE:])]))
        self.reader._open_unlocked('base.db').close()
        before = source_wal.stat()
        header = source_wal.read_bytes()[:32]
        updated = self.database('replacement-value.db', 'replacement')
        replacement = self.folder / 'replace-wal'
        replacement.write_bytes(wal_bytes([(2, 2, updated[PAGE:])]))
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(source_wal)
        after = source_wal.stat()
        self.assertEqual((before.st_mtime_ns, before.st_size), (after.st_mtime_ns, after.st_size))
        self.assertEqual(source_wal.read_bytes()[:32], header)
        self.assertNotEqual(before.st_ino, after.st_ino)
        with self.reader._open_unlocked('base.db') as connection:
            self.assertEqual(connection.execute('SELECT value FROM sample').fetchone()[0], 'replacement')
        connection.close()

    def test_replaced_source_cannot_fall_back_to_other_file_identity(self):
        self.open_reader()
        self.reader._open_unlocked('base.db').close()
        source = self.folder / 'base.db'
        before = source.stat()
        replacement = self.folder / 'replace-main.db'
        replacement.write_bytes(self.committed)
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(source)
        with patch.object(self.reader, '_decrypt_file', side_effect=RuntimeError('synthetic failure')), patch.object(db.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '一致快照'):
                self.reader._open_unlocked('base.db')


if __name__ == '__main__':
    unittest.main()
