from __future__ import annotations

import hashlib
import json
import unittest

from noadcast.classify.base import ClassifierError
from noadcast.classify.prompts import (
    AUDIO_SEGMENTS_PROMPT,
    LINE_RESPONSE_SCHEMA,
    PROMPTS,
    RESPONSE_SCHEMA,
    SEGMENTS_ONLY_PROMPT,
    SchemaViolation,
    get_prompt,
    json_schema_for_claude,
    strip_json_fence,
)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RecoveredPromptTests(unittest.TestCase):
    """Digests of the recovered sources, taken from `git show`, so any edit to
    a published prompt fails loudly."""

    def test_v1_prompt_is_the_recovered_segments_only_prompt(self) -> None:
        # SEGMENTS_ONLY_PROMPT in 3e6d713:server/main.py
        self.assertEqual(sha256(SEGMENTS_ONLY_PROMPT), "f2efe5050f3d5a9b8542d93a3157ee1818e5ff1d1ce019765de9df35825eb074")

    def test_v1_schema_is_the_recovered_response_schema(self) -> None:
        # RESPONSE_SCHEMA in 3e6d713:server/main.py
        self.assertEqual(
            sha256(json.dumps(RESPONSE_SCHEMA, sort_keys=True)),
            "a3d7193ca58a0f29aef952f457164f5ec95dc742e23ee3b64d98405ac6be8aeb",
        )

    def test_audio_prompt_is_the_apps_segments_only_prompt(self) -> None:
        # CloudAdDetectionService.segmentsOnlyPrompt at c1a53ce, as the Swift literal evaluates.
        self.assertEqual(sha256(AUDIO_SEGMENTS_PROMPT), "b9e876598bf2bb0007ed9350873a9491ac05c25c1e49efca857300b0a58c267f")

    def test_v1_user_message_matches_the_recovered_request(self) -> None:
        spec = get_prompt("segments-v1", "seconds")
        text = spec.user_message("[0.00 - 1.00] Hi.", "The complete episode ends at 9.00 seconds.")
        self.assertEqual(
            text,
            "Classify only the following transcript. Segment starts, intros, and ads must stay within these "
            "transcript ranges.\n\nThe complete episode ends at 9.00 seconds.\n\n[0.00 - 1.00] Hi.",
        )


class PromptV2Tests(unittest.TestCase):
    def test_v2_adds_the_three_restored_rules(self) -> None:
        for fmt in ("index", "seconds"):
            with self.subTest(fmt=fmt):
                system = " ".join(get_prompt("segments-v2", fmt).system.split())
                self.assertIn("host or show introductions that are likely the same in every episode", system)
                self.assertIn(
                    "Do NOT include host, interviewee, guest, or episode introductions that are specific to this episode",
                    system,
                )
                self.assertIn(
                    "Editorial mentions, listener mail, the host's own products discussed editorially, "
                    "and interview segments are NOT ads.",
                    system,
                )
                self.assertIn("Merge multiple ads that are adjacent to each other into a single segment", system)
                self.assertIn("only if they are not separated by substantive content", system)
                self.assertIn("Do not omit an outro merely because", system)

    def test_index_variant_cites_lines(self) -> None:
        spec = get_prompt("segments-v2", "index")
        self.assertTrue(spec.cites_lines)
        self.assertIs(spec.gemini_schema, LINE_RESPONSE_SCHEMA)
        self.assertIn("`line|start| text`", spec.system)
        self.assertIn("--- Ns of no speech ---", spec.system)
        self.assertIn("`startLine`", spec.system)
        items = LINE_RESPONSE_SCHEMA["properties"]["segments"]["items"]
        self.assertEqual(items["properties"]["startLine"], {"type": "INTEGER"})
        self.assertIn("endLine", items["required"])

    def test_seconds_variant_keeps_the_v1_schema(self) -> None:
        spec = get_prompt("segments-v2", "seconds")
        self.assertFalse(spec.cites_lines)
        self.assertIs(spec.gemini_schema, RESPONSE_SCHEMA)
        self.assertIn("`[start - end] text`", spec.system)

    def test_unsupported_combinations_are_permanent_errors(self) -> None:
        with self.assertRaises(ClassifierError) as unknown:
            get_prompt("segments-v9", "index")
        self.assertTrue(unknown.exception.permanent)
        with self.assertRaises(ClassifierError) as mismatch:
            get_prompt("segments-v1", "index")
        self.assertTrue(mismatch.exception.permanent)
        self.assertIn("'seconds'", str(mismatch.exception))


class PromptV3Tests(unittest.TestCase):
    def test_sentence_prompt_requires_content_summary(self) -> None:
        spec = get_prompt("segments-v3", "sentences")
        self.assertFalse(spec.cites_lines)
        self.assertIn("`[22.24-23.88] A complete sentence.`", spec.system)
        self.assertIn("nonempty `summary` describing what is actually said or promoted", spec.system)
        self.assertIn("complete audio duration as the outro's `endSeconds`", spec.system)
        item = spec.gemini_schema["properties"]["segments"]["items"]
        self.assertEqual(item["properties"]["summary"], {"type": "STRING"})
        self.assertIn("summary", item["required"])
        self.assertIn("summary", spec.claude_schema["properties"]["segments"]["items"]["required"])
        [parsed] = spec.parse('{"segments": [{"startSeconds": 22.24, "endSeconds": 23.88, '
                              '"kind": "ad", "summary": "  Acme mattress discount  "}]}')
        self.assertEqual(parsed.summary, "Acme mattress discount")

    def test_sentence_prompt_rejects_blank_or_missing_summary(self) -> None:
        spec = get_prompt("segments-v3", "sentences")
        for summary in ('"summary": ""', '"summary": "  "', ''):
            with self.subTest(summary=summary), self.assertRaises(SchemaViolation):
                fields = ', ' + summary if summary else ''
                spec.parse('{"segments": [{"startSeconds": 22.24, "endSeconds": 23.88, '
                           '"kind": "ad"' + fields + '}]}')


class SchemaTests(unittest.TestCase):
    def test_claude_schema_is_the_lowercase_strict_equivalent(self) -> None:
        self.assertEqual(
            json_schema_for_claude(RESPONSE_SCHEMA),
            {
                "type": "object",
                "properties": {
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "startSeconds": {"type": "number"},
                                "endSeconds": {"type": "number"},
                                "summary": {"type": "string"},
                                "kind": {"type": "string", "enum": ["ad", "intro", "outro"]},
                            },
                            "required": ["startSeconds", "endSeconds", "summary", "kind"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["segments"],
                "additionalProperties": False,
            },
        )

    def test_every_spec_parses_a_document_its_schema_allows(self) -> None:
        for (version, fmt), spec in PROMPTS.items():
            with self.subTest(version=version, fmt=fmt):
                items = spec.claude_schema["properties"]["segments"]["items"]
                row = {name: 12 if prop["type"] in ("integer", "number") else "intro"
                       for name, prop in items["properties"].items()}
                row["endSeconds"] = 30
                self.assertEqual(set(row), set(items["required"]))
                [parsed] = spec.parse(json.dumps({"segments": [row]}))
                self.assertEqual((parsed.start_seconds, parsed.end_seconds, parsed.kind), (12.0, 30.0, "intro"))
                self.assertEqual(parsed.start_line, 12 if spec.cites_lines else None)


class ParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = get_prompt("segments-v2", "index")

    def test_strip_json_fence(self) -> None:
        self.assertEqual(strip_json_fence('```json\n{"segments": []}\n```'), '{"segments": []}')
        self.assertEqual(strip_json_fence('```\n{"segments": []}```'), '{"segments": []}')
        self.assertEqual(strip_json_fence('  {"segments": []}  '), '{"segments": []}')

    def test_parses_fenced_answers_and_tolerates_missing_lines_and_unknown_kinds(self) -> None:
        text = '```json\n{"segments": [{"startSeconds": "1.5", "endSeconds": 9, "summary": "x", "kind": "sponsor"}]}\n```'
        [parsed] = self.spec.parse(text)
        self.assertEqual((parsed.start_seconds, parsed.end_seconds, parsed.kind), (1.5, 9.0, "sponsor"))
        self.assertIsNone(parsed.start_line)

    def test_schema_violations(self) -> None:
        for text in ("not json", "[]", '{"segs": []}', '{"segments": [{"startSeconds": 1}]}',
                     '{"segments": [{"startSeconds": "soon", "endSeconds": 2, "kind": "ad"}]}'):
            with self.subTest(text=text), self.assertRaises(SchemaViolation):
                self.spec.parse(text)


if __name__ == "__main__":
    unittest.main()
