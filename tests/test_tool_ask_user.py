"""tool-ask-user 模型可见文案（对齐 alpha.2 skippedAnswerGuidance）。"""
import unittest

from miniharness.interaction.tool_ask_user import (
    DESCRIPTION,
    PENDING_NOTICE,
    SKIPPED_ANSWER_GUIDANCE,
    TIMED_DESCRIPTION,
)


class AskUserQuestionDescriptionTest(unittest.TestCase):
    def test_legacy_description_includes_skip_guidance(self):
        self.assertIn("Ask the user a concise question", DESCRIPTION)
        self.assertIn(SKIPPED_ANSWER_GUIDANCE, DESCRIPTION)

    def test_timed_description_includes_skip_and_pending(self):
        self.assertIn(SKIPPED_ANSWER_GUIDANCE, TIMED_DESCRIPTION)
        self.assertIn("A pending result instead means", TIMED_DESCRIPTION)

    def test_pending_notice_distinguishes_skip(self):
        self.assertIn("This is pending, not a skipped answer.", PENDING_NOTICE)


if __name__ == "__main__":
    unittest.main()
