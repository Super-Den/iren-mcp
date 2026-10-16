import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import iren_mcp_server as iren


class ItxSecurityTests(unittest.TestCase):
    def test_archive_paths(self):
        self.assertEqual(iren.safe_image_path("images/diagram.png"), "images/diagram.png")
        for value in ("../secret.txt", "images/../secret.png", "images\\secret.png", "/absolute.png", "C:/secret.png", "images/secret.txt", "images/CON.png"):
            self.assertIsNone(iren.safe_image_path(value), value)

    def test_rejects_unexpected_zip_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.itx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("test.xml", '<irenTest version="6"/>')
                archive.writestr("secret.txt", "should not be published")
            with self.assertRaises(iren.ItxError):
                iren.read_itx(str(path))

    def test_accepts_iren_clipboard_source_data(self):
        xml = b'''<irenTest version="6"><section title="S"><questions>
  <question type="select" tag="1" weight="1" enabled="true">
    <content><img src="images/1.png" clipboardFormat="image/png" clipboardData="images/1.dat"/></content>
    <choices>
      <choice correct="true" fixed="false" negative="false"><content><text value="A"/></content></choice>
      <choice correct="false" fixed="false" negative="false"><content><text value="B"/></content></choice>
    </choices>
  </question>
</questions></section><testInfo lastGeneratedTag="1"/></irenTest>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clipboard.itx"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("test.xml", xml)
                archive.writestr("images/1.png", b"png")
                archive.writestr("images/1.dat", b"original")
            report = iren.validate_test(str(source))
            self.assertTrue(report["valid"])

    def test_reports_missing_iren_clipboard_source_data(self):
        xml = b'''<irenTest version="6"><section title="S"><questions>
  <question type="select" tag="1" weight="1" enabled="true">
    <content><img src="images/1.png" clipboardFormat="image/png" clipboardData="images/1.dat"/></content>
    <choices>
      <choice correct="true" fixed="false" negative="false"><content><text value="A"/></content></choice>
      <choice correct="false" fixed="false" negative="false"><content><text value="B"/></content></choice>
    </choices>
  </question>
</questions></section><testInfo lastGeneratedTag="1"/></irenTest>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "clipboard-missing.itx"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("test.xml", xml)
                archive.writestr("images/1.png", b"png")
            report = iren.validate_test(str(source))
            self.assertFalse(report["valid"])
            self.assertIn("missing_clipboard_data", {item["code"] for item in report["findings"]})

    def test_directory_reader_does_not_collect_unreferenced_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "images").mkdir()
            (root / "secret.txt").write_text("private", encoding="utf-8")
            (root / "test.xml").write_text(
                '<irenTest version="6"><section title=""><questions>'
                '<question type="select" tag="1" weight="1" enabled="true">'
                '<content><text value="Q"/></content><choices>'
                '<choice correct="true" fixed="false" negative="false"><content><text value="A"/></content></choice>'
                '<choice correct="false" fixed="false" negative="false"><content><text value="B"/></content></choice>'
                '</choices></question></questions></section><testInfo lastGeneratedTag="1"/></irenTest>',
                encoding="utf-8",
            )
            _, _, files = iren.read_itx(str(root))
            self.assertEqual(set(files), {"test.xml"})


class ItxWorkflowTests(unittest.TestCase):
    def test_create_validate_and_render(self):
        spec = {
            "sectionTitle": "Smoke",
            "questions": [{
                "type": "select",
                "content": "Q",
                "choices": [{"text": "A", "correct": True}, {"text": "B"}],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "test.itx"
            result = iren.create_test({"spec": spec, "output": str(output)})
            self.assertTrue(result["validation"]["valid"])
            with self.assertRaises(iren.ItxError):
                iren.create_test({"spec": spec, "output": str(output)})
            preview = root / "preview"
            rendered = iren.render_html(str(output), str(preview))
            self.assertEqual(rendered["questionCount"], 1)
            self.assertTrue((preview / "index.html").is_file())
            with self.assertRaises(iren.ItxError):
                iren.render_html(str(output), str(preview))
            info = iren.test_info(str(output), include_answers=True)
            self.assertEqual(info["questionCount"], 1)
            self.assertEqual(info["questions"][0]["correct"], ["A"])
            unpacked = root / "unpacked"
            unpack_report = iren.unpack_test(str(output), str(unpacked))
            self.assertIn("test.xml", unpack_report["files"])
            self.assertTrue((unpacked / "test.xml").is_file())
            with self.assertRaises(iren.ItxError):
                iren.unpack_test(str(output), str(unpacked))

    def test_mcp_tool_listing(self):
        names = {tool["name"] for tool in iren.TOOL_DEFS}
        self.assertEqual(names, {"create_test", "build_test", "test_info", "unpack_test", "validate_formulas", "validate_test", "render_html", "open_in_iren"})

    def test_rejects_iren_incompatible_exponential_precision(self):
        xml = b'''<?xml version="1.0" encoding="utf-8"?>
<irenTest version="6"><section title="S"><questions>
  <question type="input" tag="1" weight="1" enabled="true">
    <content><text value="Q"/></content>
    <patterns><pattern type="text" value="0.0002" quality="1.0000"
      caseSensitive="false" spaces="normalize" numericAware="true"
      wildcard="false" precision="1e-06"/></patterns>
  </question>
</questions></section></irenTest>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad_precision.itx"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("test.xml", xml)
            report = iren.validate_formulas(str(source))
            self.assertFalse(report["valid"])
            self.assertIn("invalid_pattern_precision", {item["code"] for item in report["findings"]})

    def test_builder_parses_string_booleans(self):
        spec = {
            "sectionTitle": "Boolean strings",
            "questions": [{
                "type": "select",
                "content": "Q",
                "enabled": "false",
                "choices": [
                    {"text": "A", "correct": "true", "fixed": "false", "negative": "false"},
                    {"text": "B", "correct": "false"},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "bools.itx"
            iren.create_test({"spec": spec, "output": str(output)})
            with zipfile.ZipFile(output) as archive:
                xml = archive.read("test.xml").decode("utf-8")
            self.assertIn('enabled="false"', xml)
            self.assertIn('correct="true"', xml)
            self.assertIn('fixed="false"', xml)

    def test_section_negative_modifier_and_disabled_empty_answer(self):
        xml = b'''<irenTest version="6"><section title="S">
  <modifiers><modifier type="addNegativeChoice"/></modifiers><questions>
  <question type="select" tag="1" weight="1" enabled="true"><content><text value="Q"/></content><choices>
    <choice correct="false" fixed="false" negative="false"><content><text value="A"/></content></choice>
    <choice correct="false" fixed="false" negative="false"><content><text value="B"/></content></choice>
  </choices></question></questions></section><testInfo lastGeneratedTag="1"/></irenTest>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "section-negative.itx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("test.xml", xml)
            report = iren.validate_test(str(source))
            self.assertTrue(report["valid"])

    def test_section_profile_must_mirror_sections(self):
        xml = b'''<irenTest version="6"><section title="S"><questions>
  <question type="select" tag="1" weight="1" enabled="true"><content><text value="Q"/></content><choices>
    <choice correct="true" fixed="false" negative="false"><content><text value="A"/></content></choice>
    <choice correct="false" fixed="false" negative="false"><content><text value="B"/></content></choice>
  </choices></question></questions></section><profiles><profile title="P">
  <questionSelection shuffleQuestions="false"/><sessionOptions durationMinutes="unlimited" editableAnswers="false" browsableQuestions="true" weightCues="false"/>
  <sectionProfile title="Wrong" questions="all"/></profile></profiles><testInfo lastGeneratedTag="1"/></irenTest>'''
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "bad-profile.itx"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("test.xml", xml)
            report = iren.validate_test(str(source))
            self.assertFalse(report["valid"])
            self.assertIn("section_profile_name_mismatch", {item["code"] for item in report["findings"]})


if __name__ == "__main__":
    unittest.main()
