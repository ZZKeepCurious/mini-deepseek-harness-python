import unittest

from miniharness.skills.registry import digest_catalog_entries


class SkillCatalogIncompleteTests(unittest.TestCase):
    def test_digest_includes_incomplete_flag(self):
        entries = [{"name": "a", "description": "A"}]
        complete = digest_catalog_entries(entries, False)
        incomplete = digest_catalog_entries(entries, True)
        self.assertNotEqual(complete, incomplete)

    def test_incomplete_catalog_message_source_and_notice(self):
        from miniharness.skills.tool_skill import (
            _render_catalog_message,
            catalog_source_entries,
        )

        skills = [{"name": "demo", "description": "Demo skill"}]
        entries = catalog_source_entries(skills, 500)
        message = _render_catalog_message(entries, True)
        self.assertIs(message["source"]["incomplete"], True)
        self.assertIn("Some skills could not be checked",
                      message["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
