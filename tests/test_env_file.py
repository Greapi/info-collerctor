import unittest

from env_file import EnvDocument, mask_secret, quote_value, revision_of


class EnvDocumentTests(unittest.TestCase):
    def test_preserves_comments_blank_lines_and_unknown_keys(self):
        text = "# model\n\nFOO=bar\nOPENAI_MODEL=deepseek-chat\nexport CUSTOM='x y'\n"
        document = EnvDocument.parse(text, source=".env")
        self.assertEqual(document.errors, [])
        self.assertEqual(document.get("FOO"), "bar")
        self.assertEqual(document.get("CUSTOM"), "x y")
        document.set("OPENAI_MODEL", "deepseek-reasoner")
        rendered = document.render()
        self.assertIn("# model", rendered)
        self.assertIn("FOO=bar", rendered)
        self.assertIn("OPENAI_MODEL=deepseek-reasoner", rendered)
        self.assertIn("export CUSTOM='x y'", rendered)

    def test_updates_all_duplicate_keys(self):
        document = EnvDocument.parse("A=1\nA=2\n")
        document.set("A", "3")
        self.assertIn("A=3\nA=3", document.render())

    def test_quotes_values_when_needed_and_round_trips(self):
        for value in ("simple", "with space", 'quote"inside', "a#b", ""):
            rendered = EnvDocument.parse(f"KEY={quote_value(value)}\n").get("KEY")
            self.assertEqual(rendered, value)

    def test_invalid_line_is_reported_and_preserved(self):
        document = EnvDocument.parse("not-a-key-value\n", source=".env")
        self.assertEqual(len(document.errors), 1)
        self.assertIn("not-a-key-value", document.render())

    def test_mask_secret(self):
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret("abcd"), "****")
        self.assertEqual(mask_secret("abcdefgh"), "****efgh")
        self.assertEqual(revision_of("x"), revision_of("x"))


if __name__ == "__main__":
    unittest.main()
