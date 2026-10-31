import unittest

from miniharness.skills.registry import digest_catalog_entries


class SkillCatalogIncompleteTests(unittest.TestCase):
    def test_digest_includes_incomplete_flag(self):
        entries = [{"name": "a", "description": "A"}]
        complete = digest_catalog_entries(entries, False)
        incomplete = digest_catalog_entries(entries, True)
        self.assertNotEqual(complete, incomplete)


if __name__ == "__main__":
    unittest.main()
