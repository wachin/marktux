"""Phase 4 acceptance: tag forest, sync, queries, cascade, persistence.

Behavioural specs ported from vxcore's ``Notebook`` tag operations and its
``SyncTagsToMetadataStore`` / ``FindFilesByTags`` paths.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from marktux.core import (
    InvalidNameError,
    InvalidQueryError,
    ReadOnlyError,
    TagExistsError,
    TagNotFoundError,
    UnsupportedError,
)

from .helpers import make_bundled, make_context, make_raw

FILE_ID = "22222222-2222-2222-2222-222222222222"


class TagCrudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp)
        self.ctx = make_context(self.tmp)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.tags = self.nb.tags

    def test_create_root_and_nested(self):
        self.tags.create("work")
        child = self.tags.create("python", "work")
        self.assertEqual(child.parent, "work")
        self.assertEqual([(n.name, n.parent) for n in self.tags.list()],
                         [("work", ""), ("python", "work")])

    def test_create_path_builds_missing_ancestors(self):
        created = self.tags.create_path("a/b/c")
        self.assertEqual([n.name for n in created], ["a", "b", "c"])
        self.assertEqual({n.name: n.parent for n in self.tags.list()},
                         {"a": "", "b": "a", "c": "b"})

    def test_create_path_is_idempotent(self):
        self.tags.create_path("a/b")
        self.tags.create_path("a/b")
        self.assertEqual(len(self.tags.list()), 2)

    def test_duplicate_tag_rejected(self):
        self.tags.create("work")
        with self.assertRaises(TagExistsError):
            self.tags.create("work")

    def test_missing_parent_rejected(self):
        with self.assertRaises(TagNotFoundError):
            self.tags.create("child", "ghost")

    def test_empty_name_rejected(self):
        with self.assertRaises((InvalidNameError, ValueError)):
            self.tags.create("")

    def test_get_and_has(self):
        self.tags.create("work")
        self.assertTrue(self.tags.has("work"))
        self.assertFalse(self.tags.has("ghost"))
        self.assertEqual(self.tags.get("work").name, "work")
        with self.assertRaises(TagNotFoundError):
            self.tags.get("ghost")

    def test_children(self):
        self.tags.create("work")
        self.tags.create("python", "work")
        self.tags.create("home")
        self.assertEqual([n.name for n in self.tags.children()], ["work", "home"])
        self.assertEqual([n.name for n in self.tags.children("work")], ["python"])

    def test_move_reparents(self):
        self.tags.create("a")
        self.tags.create("b")
        self.tags.create("c", "a")
        self.tags.move("c", "b")
        self.assertEqual(self.tags.get("c").parent, "b")

    def test_move_cycle_rejected(self):
        self.tags.create("a")
        self.tags.create("b", "a")
        with self.assertRaises(InvalidNameError):
            self.tags.move("a", "b")

    def test_move_self_rejected(self):
        self.tags.create("a")
        with self.assertRaises(InvalidNameError):
            self.tags.move("a", "a")

    def test_move_unknown_rejected(self):
        with self.assertRaises(TagNotFoundError):
            self.tags.move("ghost", "")

    def test_read_only_rejects_mutation(self):
        self.ctx.notebooks.set_read_only(self.nb.id, True)
        with self.assertRaises(ReadOnlyError):
            self.tags.create("work")


class TagStoreSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp)
        self.ctx = make_context(self.tmp)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.tags = self.nb.tags

    def test_tags_mirrored_into_store(self):
        self.tags.create("work")
        self.tags.create("python", "work")
        stored = {t.name: t.parent_name for t in self.nb.store.list_tags()}
        self.assertEqual(stored, {"work": "", "python": "work"})
        self.assertEqual(self.nb.store.counts()["tags"], 2)

    def test_sync_noop_when_stamps_equal(self):
        self.tags.create("work")
        self.assertFalse(self.tags.sync_to_store())
        self.assertFalse(self.tags.sync_to_store())

    def test_sync_removes_orphans_and_applies_newer_stamp(self):
        # Seed a config with two tags and sync.
        self.tags.create("keep")
        self.tags.create("orphan")
        # Each create already syncs via _commit, so the mirror is current.
        self.assertEqual({t.name for t in self.nb.store.list_tags()},
                         {"keep", "orphan"})

        # Simulate an external config edit: drop "orphan", add "fresh", bump
        # the stamp beyond tags_synced_utc, then reload the config in memory.
        config = self.nb.config
        config.tags = [n for n in config.tags if n.name != "orphan"]
        from marktux.core import TagNode
        config.tags.append(TagNode(name="fresh"))
        stamp = int(self.nb.store.get_meta("tags_synced_utc")) + 10
        config.tags_modified_utc = stamp
        self.nb.update_config(config)
        self.assertTrue(self.tags.sync_to_store())

        self.assertEqual({t.name for t in self.nb.store.list_tags()},
                         {"keep", "fresh"})
        self.assertEqual(int(self.nb.store.get_meta("tags_synced_utc")), stamp)

    def test_rapid_creates_all_sync(self):
        # Two creates in the same millisecond must not lose the second one.
        for name in ("a", "b", "c", "d"):
            self.tags.create(name)
        self.assertEqual({t.name for t in self.nb.store.list_tags()},
                         {"a", "b", "c", "d"})


class TagFileQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp)
        self.ctx = make_context(self.tmp)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.tags = self.nb.tags
        for name in ("work", "urgent", "python"):
            self.tags.create(name)

    def test_tag_untag_and_counts(self):
        self.tags.tag_file("notes/hello.md", "work")
        self.tags.tag_file("notes/hello.md", "urgent")
        self.assertEqual(sorted(self.nb.store.get_file_tags(FILE_ID)),
                         ["urgent", "work"])
        self.assertEqual(self.tags.count(), {"work": 1, "urgent": 1, "python": 0})

        self.tags.untag_file("notes/hello.md", "work")
        self.assertEqual(self.nb.store.get_file_tags(FILE_ID), ["urgent"])

    def test_tag_undefined_rejected(self):
        with self.assertRaises(TagNotFoundError):
            self.tags.tag_file("notes/hello.md", "undefined")

    def test_update_file_tags_replaces(self):
        self.tags.tag_file("notes/hello.md", "work")
        stored = self.tags.update_file_tags("notes/hello.md", ["python", "urgent"])
        self.assertEqual(sorted(stored), ["python", "urgent"])
        self.assertEqual(sorted(self.nb.store.get_file_tags(FILE_ID)),
                         ["python", "urgent"])

    def test_find_and_or(self):
        self.tags.tag_file("notes/hello.md", "work")
        self.tags.tag_file("notes/hello.md", "python")
        and_result = self.tags.find_files(["work", "python"], match_all=True)
        self.assertEqual([r.file_path for r in and_result], ["notes/hello.md"])
        self.assertEqual([r.file_path for r in
                          self.tags.find_files(["work", "urgent"], match_all=True)], [])
        or_result = self.tags.files(["work", "urgent"], operator="OR")
        self.assertEqual([r.file_path for r in or_result], ["notes/hello.md"])

    def test_find_bad_operator(self):
        with self.assertRaises(InvalidQueryError):
            self.tags.files(["work"], operator="XOR")

    def test_file_tags_event(self):
        seen = []
        bus = getattr(self.nb.manager, "_events", None)
        token = bus.subscribe("file.tags_changed", lambda n, p: seen.append(p))
        self.addCleanup(bus.unsubscribe, token)
        self.tags.tag_file("notes/hello.md", "work")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["path"], "notes/hello.md")

    def test_config_file_records_tags(self):
        self.tags.tag_file("notes/hello.md", "work")
        cfg_path = os.path.join(self.root, "vx_notebook", "contents", "notes", "vx.json")
        with open(cfg_path, encoding="utf-8") as handle:
            cfg = json.load(handle)
        self.assertEqual(cfg["files"][0]["tags"], ["work"])


class TagDeleteCascadeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_bundled(self.tmp)
        self.ctx = make_context(self.tmp)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)
        self.tags = self.nb.tags
        self.tags.create("work")
        self.tags.create("python", "work")
        self.tags.create("nested", "python")
        self.tags.create("keep")
        for tag in ("work", "python", "nested", "keep"):
            self.tags.tag_file("notes/hello.md", tag)

    def test_delete_subtree_detaches_files(self):
        self.tags.delete("work")
        self.assertEqual([n.name for n in self.tags.list()], ["keep"])
        self.assertEqual(self.nb.store.get_file_tags(FILE_ID), ["keep"])
        self.assertEqual({t.name for t in self.nb.store.list_tags()}, {"keep"})

    def test_delete_leaf_keeps_parent(self):
        self.tags.delete("nested")
        self.assertEqual({n.name for n in self.tags.list()},
                         {"work", "python", "keep"})
        self.assertEqual(sorted(self.nb.store.get_file_tags(FILE_ID)),
                         ["keep", "python", "work"])

    def test_delete_unknown_rejected(self):
        with self.assertRaises(TagNotFoundError):
            self.tags.delete("ghost")


class TagPersistenceTest(unittest.TestCase):
    def test_tags_survive_reopen(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root = make_bundled(tmp)
        ctx = make_context(tmp)
        nb = ctx.open_notebook(root)
        nb.tags.create("work")
        nb.tags.create("python", "work")
        nb.tags.tag_file("notes/hello.md", "python")
        ctx.close()

        # A fresh context reopening the same notebook reads the tag forest from
        # config.json and rebuilds the mirror from the tagsModifiedUtc stamp.
        ctx2 = make_context(tmp)
        self.addCleanup(ctx2.close)
        nb2 = ctx2.open_notebook(root)
        self.assertEqual({n.name: n.parent for n in nb2.tags.list()},
                         {"work": "", "python": "work"})
        self.assertEqual({t.name for t in nb2.store.list_tags()},
                         {"work", "python"})
        self.assertEqual(nb2.store.get_file_tags(FILE_ID), ["python"])


class RawTagTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = make_raw(self.tmp)
        self.ctx = make_context(self.tmp)
        self.addCleanup(self.ctx.close)
        self.nb = self.ctx.open_notebook(self.root)

    def test_raw_create_rejected(self):
        with self.assertRaises(UnsupportedError):
            self.nb.tags.create("work")

    def test_raw_file_tag_rejected(self):
        with self.assertRaises(UnsupportedError):
            self.nb.tags.tag_file("a.md", "work")


if __name__ == "__main__":
    unittest.main()
