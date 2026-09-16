"""Tests for jira_mini_mcp.models: dataclasses and value normalization.

Fixtures under tests/fixtures/ contain only synthetic data. Their provenance
notes distinguish live-derived Jira Cloud shapes from hand-authored cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jira_mini_mcp import models

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestUserAndCompactUser:
    def test_compact_user_keeps_only_account_id_and_display_name(self) -> None:
        raw = {
            "accountId": "syn-acc-001",
            "displayName": "Jordan Lee",
            "emailAddress": "jordan.lee@example.invalid",
            "avatarUrls": {"48x48": "https://synthetic-tenant.atlassian.net/avatar/1"},
            "self": "https://synthetic-tenant.atlassian.net/rest/api/3/user?accountId=syn-acc-001",
            "active": True,
        }
        user = models.compact_user(raw)
        assert user == models.User(account_id="syn-acc-001", display_name="Jordan Lee")
        assert not hasattr(user, "email")
        assert not hasattr(user, "self")
        assert not hasattr(user, "avatar_urls")

    def test_compact_user_never_leaks_email_or_self_in_repr(self) -> None:
        raw = {
            "accountId": "syn-acc-001",
            "displayName": "Jordan Lee",
            "emailAddress": "jordan.lee@example.invalid",
            "self": "https://synthetic-tenant.atlassian.net/rest/api/3/user?accountId=syn-acc-001",
        }
        user = models.compact_user(raw)
        text = repr(user)
        assert "example.invalid" not in text
        assert "synthetic-tenant" not in text


class TestNormalizeComment:
    def _raw(self, **overrides: object) -> dict:
        base = {
            "id": "92351",
            "author": {"accountId": "syn-acc-001", "displayName": "Jordan Lee"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [{"type": "paragraph", "content": []}],
            },
            "created": "2025-08-26T09:55:40.906-04:00",
        }
        base.update(overrides)
        return base

    def test_minimal_comment_omits_updated_and_updated_by(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(self._raw(), "$.comments[0]", problems)
        assert problems == []
        assert comment == models.Comment(
            id="92351",
            author=models.User(account_id="syn-acc-001", display_name="Jordan Lee"),
            body="",
            created="2025-08-26T13:55:40Z",
        )

    def test_updated_equal_to_created_is_omitted(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(
            self._raw(
                updated="2025-08-26T09:55:40.906-04:00",
                updateAuthor={"accountId": "syn-acc-001", "displayName": "Jordan Lee"},
            ),
            "$.comments[0]",
            problems,
        )
        assert problems == []
        assert comment is not None
        assert comment.updated is None
        assert comment.updated_by is None

    def test_updated_by_same_user_is_omitted(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(
            self._raw(
                updated="2025-08-27T09:55:40.906-04:00",
                updateAuthor={"accountId": "syn-acc-001", "displayName": "Jordan Lee"},
            ),
            "$.comments[0]",
            problems,
        )
        assert problems == []
        assert comment is not None
        assert comment.updated == "2025-08-27T13:55:40Z"
        assert comment.updated_by is None

    def test_updated_by_different_user_is_included(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(
            self._raw(
                updated="2025-08-27T09:55:40.906-04:00",
                updateAuthor={"accountId": "syn-acc-002", "displayName": "Alex Kim"},
            ),
            "$.comments[0]",
            problems,
        )
        assert problems == []
        assert comment is not None
        assert comment.updated == "2025-08-27T13:55:40Z"
        assert comment.updated_by == models.User(account_id="syn-acc-002", display_name="Alex Kim")

    def test_updated_differs_with_no_update_author_key(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(
            self._raw(updated="2025-08-27T09:55:40.906-04:00"),
            "$.comments[0]",
            problems,
        )
        assert problems == []
        assert comment is not None
        assert comment.updated == "2025-08-27T13:55:40Z"
        assert comment.updated_by is None

    def test_non_string_updated_records_problem_without_dropping_comment(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(self._raw(updated=12345), "$.comments[0]", problems)
        assert comment is not None
        assert comment.updated is None
        assert [str(p) for p in problems] == ["$.comments[0].updated: expected a string"]

    def test_no_self_or_visibility_leak(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(
            visibility={"type": "role", "value": "Administrators"},
            jsdPublic=True,
        )
        raw["self"] = "https://synthetic-tenant.atlassian.net/rest/api/3/issue/1/comment/92351"
        comment = models.normalize_comment(raw, "$.comments[0]", problems)
        assert not hasattr(comment, "self")
        assert not hasattr(comment, "visibility")
        assert not hasattr(comment, "jsdPublic")

    def test_missing_id_drops_comment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(self._raw(id=None), "$.comments[0]", problems)
        assert comment is None
        assert [str(p) for p in problems] == ["$.comments[0].id: expected a non-empty string"]

    def test_missing_author_drops_comment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(self._raw(author=None), "$.comments[0]", problems)
        assert comment is None
        assert any(p.path == "$.comments[0].author" for p in problems)

    def test_malformed_created_drops_comment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(
            self._raw(created="not-a-timestamp"), "$.comments[0]", problems
        )
        assert comment is None
        assert any(p.path == "$.comments[0].created" for p in problems)

    def test_non_object_body_drops_comment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(self._raw(body="plain text"), "$.comments[0]", problems)
        assert comment is None
        assert any(p.path == "$.comments[0].body" for p in problems)

    def test_malformed_updated_is_omitted_without_dropping_comment(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment(
            self._raw(updated="not-a-timestamp"), "$.comments[0]", problems
        )
        assert comment is not None
        assert comment.updated is None
        assert any(p.path == "$.comments[0].updated" for p in problems)

    def test_non_object_raw_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        comment = models.normalize_comment("not-an-object", "$.comments[0]", problems)
        assert comment is None
        assert [str(p) for p in problems] == ["$.comments[0]: expected an object"]


class TestNormalizeAttachment:
    def _raw(self, **overrides: object) -> dict:
        base = {
            "id": "80001",
            "filename": "diagnostics.log",
            "author": {"accountId": "syn-acc-001", "displayName": "Jordan Lee"},
            "created": "2025-08-26T09:55:40.906-04:00",
            "size": 4096,
            "mimeType": "text/plain",
        }
        base.update(overrides)
        return base

    def test_minimal_attachment_normalizes(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(self._raw(), "$.attachment[0]", problems)
        assert problems == []
        assert attachment == models.Attachment(
            id="80001",
            filename="diagnostics.log",
            mime_type="text/plain",
            size=4096,
            author=models.User(account_id="syn-acc-001", display_name="Jordan Lee"),
            created="2025-08-26T13:55:40Z",
        )

    def test_integer_id_is_accepted_and_stringified(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(self._raw(id=80001), "$.attachment[0]", problems)
        assert problems == []
        assert attachment is not None
        assert attachment.id == "80001"

    def test_no_self_or_thumbnail_leak(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(
            thumbnail="https://synthetic-tenant.atlassian.net/rest/api/3/attachment/thumbnail/80001"
        )
        raw["self"] = "https://synthetic-tenant.atlassian.net/rest/api/3/attachment/80001"
        raw["content"] = (
            "https://synthetic-tenant.atlassian.net/rest/api/3/attachment/content/80001"
        )
        attachment = models.normalize_attachment(raw, "$.attachment[0]", problems)
        assert not hasattr(attachment, "self")
        assert not hasattr(attachment, "thumbnail")
        assert not hasattr(attachment, "content")

    def test_missing_id_drops_attachment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(self._raw(id=None), "$.attachment[0]", problems)
        assert attachment is None
        assert any(p.path == "$.attachment[0].id" for p in problems)

    def test_boolean_id_is_rejected(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(self._raw(id=True), "$.attachment[0]", problems)
        assert attachment is None
        assert any(p.path == "$.attachment[0].id" for p in problems)

    def test_missing_filename_drops_attachment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(
            self._raw(filename=None), "$.attachment[0]", problems
        )
        assert attachment is None
        assert any(p.path == "$.attachment[0].filename" for p in problems)

    def test_missing_mime_type_drops_attachment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(
            self._raw(mimeType=None), "$.attachment[0]", problems
        )
        assert attachment is None
        assert any(p.path == "$.attachment[0].mimeType" for p in problems)

    @pytest.mark.parametrize("bad_size", [None, -1, "4096", True])
    def test_invalid_size_drops_attachment_and_records_problem(self, bad_size: object) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(
            self._raw(size=bad_size), "$.attachment[0]", problems
        )
        assert attachment is None
        assert any(p.path == "$.attachment[0].size" for p in problems)

    def test_missing_author_drops_attachment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(
            self._raw(author=None), "$.attachment[0]", problems
        )
        assert attachment is None
        assert any(p.path == "$.attachment[0].author" for p in problems)

    def test_malformed_created_drops_attachment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(
            self._raw(created="not-a-timestamp"), "$.attachment[0]", problems
        )
        assert attachment is None
        assert any(p.path == "$.attachment[0].created" for p in problems)

    def test_non_string_created_drops_attachment_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment(
            self._raw(created=12345), "$.attachment[0]", problems
        )
        assert attachment is None
        assert any(p.path == "$.attachment[0].created" for p in problems)

    def test_non_object_raw_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        attachment = models.normalize_attachment("not-an-object", "$.attachment[0]", problems)
        assert attachment is None
        assert [str(p) for p in problems] == ["$.attachment[0]: expected an object"]


class TestNormalizeChangelogChange:
    def _raw(self, **overrides: object) -> dict:
        base = {
            "field": "status",
            "fieldtype": "jira",
            "fieldId": "status",
            "from": "10000",
            "fromString": "To Do",
            "to": "10001",
            "toString": "In Progress",
        }
        base.update(overrides)
        return base

    def test_standard_field_change_normalizes_without_field_id(self) -> None:
        problems: list[models.NormalizationProblem] = []
        change = models.normalize_changelog_change(self._raw(), "$.items[0]", problems)
        assert problems == []
        assert change == models.ChangelogChange(
            field="status", from_="To Do", to="In Progress", field_id=None
        )

    def test_custom_field_change_keeps_field_id(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(
            field="Sprint",
            fieldtype="custom",
            fieldId="customfield_10020",
            **{"from": "", "fromString": "", "to": "213", "toString": "Sprint 12"},
        )
        change = models.normalize_changelog_change(raw, "$.items[0]", problems)
        assert problems == []
        assert change == models.ChangelogChange(
            field="Sprint", from_="", to="Sprint 12", field_id="customfield_10020"
        )

    def test_custom_field_without_field_id_key_omits_field_id(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(field="Some Custom Field", fieldtype="custom")
        del raw["fieldId"]
        change = models.normalize_changelog_change(raw, "$.items[0]", problems)
        assert problems == []
        assert change is not None
        assert change.field_id is None

    def test_missing_field_id_on_standard_change_is_tolerated(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(field="Link", to="SYN-9", toString="This issue relates to SYN-9")
        del raw["fieldId"]
        change = models.normalize_changelog_change(raw, "$.items[0]", problems)
        assert problems == []
        assert change is not None
        assert change.field_id is None

    def test_null_from_string_becomes_none(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(field="Attachment", fieldId="attachment")
        raw["from"] = None
        raw["fromString"] = None
        change = models.normalize_changelog_change(raw, "$.items[0]", problems)
        assert problems == []
        assert change is not None
        assert change.from_ is None

    def test_non_string_to_string_becomes_none_not_a_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(toString=12345)
        change = models.normalize_changelog_change(raw, "$.items[0]", problems)
        assert problems == []
        assert change is not None
        assert change.to is None

    def test_missing_field_drops_change_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        change = models.normalize_changelog_change(self._raw(field=None), "$.items[0]", problems)
        assert change is None
        assert any(p.path == "$.items[0].field" for p in problems)

    def test_non_object_raw_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        change = models.normalize_changelog_change("not-an-object", "$.items[0]", problems)
        assert change is None
        assert [str(p) for p in problems] == ["$.items[0]: expected an object"]


class TestNormalizeChangelogEntry:
    def _raw(self, **overrides: object) -> dict:
        base = {
            "id": "50101",
            "author": {"accountId": "syn-acc-301", "displayName": "Jordan Lee"},
            "created": "2025-08-26T09:55:40.906-04:00",
            "items": [
                {
                    "field": "status",
                    "fieldtype": "jira",
                    "fieldId": "status",
                    "from": "10000",
                    "fromString": "To Do",
                    "to": "10001",
                    "toString": "In Progress",
                }
            ],
        }
        base.update(overrides)
        return base

    def test_minimal_entry_normalizes(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry(self._raw(), "$.items[0]", problems)
        assert problems == []
        assert entry == models.ChangelogEntry(
            id="50101",
            author=models.User(account_id="syn-acc-301", display_name="Jordan Lee"),
            created="2025-08-26T13:55:40Z",
            changes=[
                models.ChangelogChange(
                    field="status", from_="To Do", to="In Progress", field_id=None
                )
            ],
        )

    def test_minimal_keys_entry_with_empty_items_normalizes(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry(self._raw(items=[]), "$.items[0]", problems)
        assert problems == []
        assert entry is not None
        assert entry.changes == []

    def test_missing_id_drops_entry_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry(self._raw(id=None), "$.items[0]", problems)
        assert entry is None
        assert any(p.path == "$.items[0].id" for p in problems)

    def test_missing_author_drops_entry_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry(self._raw(author=None), "$.items[0]", problems)
        assert entry is None
        assert any(p.path == "$.items[0].author" for p in problems)

    def test_malformed_created_drops_entry_and_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry(
            self._raw(created="not-a-timestamp"), "$.items[0]", problems
        )
        assert entry is None
        assert any(p.path == "$.items[0].created" for p in problems)

    def test_non_list_items_records_problem_but_keeps_entry(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry(
            self._raw(items="not-a-list"), "$.items[0]", problems
        )
        assert entry is not None
        assert entry.changes == []
        assert any(p.path == "$.items[0].items" for p in problems)

    def test_malformed_change_item_dropped_without_losing_entry_or_siblings(self) -> None:
        problems: list[models.NormalizationProblem] = []
        raw = self._raw(
            items=[
                {"field": "status", "toString": "In Progress"},
                "not-an-object",
                {"field": "priority", "toString": "Highest"},
            ]
        )
        entry = models.normalize_changelog_entry(raw, "$.items[0]", problems)
        assert entry is not None
        assert [c.field for c in entry.changes] == ["status", "priority"]
        assert any(p.path == "$.items[0].items[1]" for p in problems)

    def test_non_object_raw_records_problem(self) -> None:
        problems: list[models.NormalizationProblem] = []
        entry = models.normalize_changelog_entry("not-an-object", "$.items[0]", problems)
        assert entry is None
        assert [str(p) for p in problems] == ["$.items[0]: expected an object"]


class TestAdfToMarkdown:
    def test_full_node_coverage(self) -> None:
        fixture = _load("adf_node_coverage.json")
        assert models.adf_to_markdown(fixture["doc"]) == fixture["expected_markdown"]

    def test_unsupported_node_falls_back_to_extracted_text(self) -> None:
        fixture = _load("adf_unsupported_node.json")
        assert models.adf_to_markdown(fixture["doc"]) == fixture["expected_markdown"]

    def test_empty_doc_returns_empty_string(self) -> None:
        assert models.adf_to_markdown({"type": "doc", "version": 1, "content": []}) == ""

    def test_mention_without_leading_at_gets_one_added(self) -> None:
        node = {"type": "mention", "attrs": {"displayName": "Jordan Lee"}}
        assert models.adf_to_markdown(node) == "@Jordan Lee"

    def test_mention_without_any_name_falls_back(self) -> None:
        assert models.adf_to_markdown({"type": "mention", "attrs": {}}) == "@mention"

    def test_unsupported_leaf_node_without_content_uses_attrs_text(self) -> None:
        node = {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "\U0001f389"}}
        assert models.adf_to_markdown(node) == "\U0001f389"

    def test_stacked_marks_apply_in_order(self) -> None:
        node = {
            "type": "text",
            "text": "foo",
            "marks": [{"type": "strong"}, {"type": "em"}],
        }
        assert models.adf_to_markdown(node) == "***foo***"

    def test_link_mark_followed_by_another_mark(self) -> None:
        node = {
            "type": "text",
            "text": "docs",
            "marks": [
                {"type": "link", "attrs": {"href": "https://example.invalid/docs"}},
                {"type": "strong"},
            ],
        }
        assert models.adf_to_markdown(node) == "**[docs](https://example.invalid/docs)**"

    def test_unrecognized_mark_type_is_ignored(self) -> None:
        node = {
            "type": "text",
            "text": "plain",
            "marks": [{"type": "underline"}],
        }
        assert models.adf_to_markdown(node) == "plain"


class TestToUtcIso:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2024-01-15T10:30:00.000Z", "2024-01-15T10:30:00Z"),
            ("2024-01-15T10:30:00+00:00", "2024-01-15T10:30:00Z"),
            ("2024-01-15T10:30:00.000+0200", "2024-01-15T08:30:00Z"),
            ("2024-01-15T10:30:00-05:00", "2024-01-15T15:30:00Z"),
            ("2024-01-15T12:00:00+0530", "2024-01-15T06:30:00Z"),
        ],
    )
    def test_multiple_offsets_normalize_to_utc(self, raw: str, expected: str) -> None:
        assert models.to_utc_iso(raw) == expected

    def test_equal_instants_from_different_offsets_are_identical(self) -> None:
        a = models.to_utc_iso("2024-01-01T00:00:00Z")
        b = models.to_utc_iso("2024-01-01T02:00:00+02:00")
        assert a == b == "2024-01-01T00:00:00Z"

    def test_naive_timestamp_without_offset_raises(self) -> None:
        with pytest.raises(ValueError):
            models.to_utc_iso("2024-01-15T10:30:00")


class TestNormalizeIssueFields:
    def test_full_default_fields(self) -> None:
        fixture = _load("issue_full_fields.json")
        result = models.normalize_issue_fields(fixture["fields"])
        expected = fixture["expected_normalized_fields"]
        assert result.keys() == expected.keys()
        assert result["assignee"] == models.User(
            **{"account_id": "syn-acc-001", "display_name": "Jordan Lee"}
        )
        assert result["reporter"] == models.User(
            **{"account_id": "syn-acc-002", "display_name": "Riley Chen"}
        )
        for key in (
            "summary",
            "description",
            "issuetype",
            "status",
            "priority",
            "labels",
            "components",
            "created",
            "updated",
            "issuelinks",
            "project",
            "subtasks",
        ):
            assert result[key] == expected[key]

    def test_known_resources_have_exact_compact_shapes(self) -> None:
        fixture = _load("issue_full_fields.json")
        result = models.normalize_issue_fields(fixture["fields"])

        assert result["issuetype"] == {
            "id": "10001",
            "name": "Bug",
            "hierarchy_level": 0,
        }
        assert result["status"] == {
            "id": "3",
            "name": "In Progress",
            "category": "indeterminate",
        }
        assert result["priority"] == {"id": "2", "name": "High"}
        assert result["project"] == {
            "id": "10000",
            "key": "SYN",
            "name": "Synthetic Project",
        }
        assert result["components"] == [{"id": "10010", "name": "Backend"}]

    def test_parent_subtasks_and_links_share_compact_issue_reference(self) -> None:
        fixture = _load("issue_full_fields.json")
        result = models.normalize_issue_fields(fixture["fields"])
        expected = fixture["expected_normalized_fields"]

        assert result["parent"] == expected["parent"]
        assert result["subtasks"] == expected["subtasks"]
        assert result["issuelinks"] == expected["issuelinks"]
        serialized = json.dumps({key: result[key] for key in ("parent", "subtasks", "issuelinks")})
        for forbidden in ("self", "iconUrl", "description", "priority", "project"):
            assert f'"{forbidden}"' not in serialized

    def test_absent_optional_resource_values_are_omitted(self) -> None:
        result = models.normalize_issue_fields(
            {
                "issuetype": {"id": "1", "name": "Task"},
                "status": {"id": "2", "name": "Open"},
                "parent": {"key": "SYN-1"},
            }
        )

        assert result == {
            "issuetype": {"id": "1", "name": "Task"},
            "status": {"id": "2", "name": "Open"},
            "parent": {"key": "SYN-1"},
        }

    def test_null_optional_fields_are_omitted_not_null(self) -> None:
        result = models.normalize_issue_fields({"resolutiondate": None, "parent": None})
        assert "resolutiondate" not in result
        assert "parent" not in result

    def test_fields_empty_dict_returns_empty(self) -> None:
        assert models.normalize_issue_fields({}) == {}

    def test_missing_optional_field_value_is_omitted(self) -> None:
        fixture = _load("issue_missing_optional_fields.json")
        result = models.normalize_issue_fields(fixture["fields"])
        assert result == fixture["expected_normalized_fields"]
        assert "assignee" not in result

    def test_custom_fields_pass_through_with_recursive_normalization(self) -> None:
        fixture = _load("issue_custom_fields.json")
        result = models.normalize_issue_fields(fixture["fields"])
        assert result == fixture["expected_normalized_fields"]

    def test_known_field_not_in_special_sets_passes_through_unchanged(self) -> None:
        raw = {"labels": ["a", "b"]}
        assert models.normalize_issue_fields(raw) == {"labels": ["a", "b"]}

    def test_malformed_user_field_reports_path_and_clean_partial_value(self) -> None:
        with pytest.raises(models.IncompleteNormalizationError) as exc_info:
            models.normalize_issue_fields(
                {"summary": "Still useful", "assignee": "not-a-user-object"}
            )

        assert exc_info.value.partial_value == {"summary": "Still useful"}
        assert [problem.path for problem in exc_info.value.problems] == ["$.fields.assignee"]

    def test_malformed_date_field_reports_path_and_clean_partial_value(self) -> None:
        with pytest.raises(models.IncompleteNormalizationError) as exc_info:
            models.normalize_issue_fields({"summary": "Still useful", "created": 12345})

        assert exc_info.value.partial_value == {"summary": "Still useful"}
        assert [problem.path for problem in exc_info.value.problems] == ["$.fields.created"]

    @pytest.mark.parametrize(
        ("raw", "problem_path"),
        [
            ({"priority": "not-an-object"}, "$.fields.priority"),
            (
                {"issuetype": {"id": "1", "name": "Task", "hierarchyLevel": "zero"}},
                "$.fields.issuetype.hierarchyLevel",
            ),
            (
                {"status": {"id": "2", "name": "Open", "statusCategory": []}},
                "$.fields.status.statusCategory",
            ),
            ({"assignee": {"accountId": "a1"}}, "$.fields.assignee.displayName"),
            ({"parent": "not-an-object"}, "$.fields.parent"),
            (
                {"parent": {"key": "SYN-1", "fields": "not-an-object"}},
                "$.fields.parent.fields",
            ),
            (
                {"parent": {"key": "SYN-1", "fields": {"summary": 42}}},
                "$.fields.parent.fields.summary",
            ),
            ({"components": "not-an-array"}, "$.fields.components"),
            ({"issuelinks": ["not-an-object"]}, "$.fields.issuelinks[0]"),
            (
                {"issuelinks": [{"type": [], "inwardIssue": {"key": "SYN-1"}}]},
                "$.fields.issuelinks[0].type",
            ),
            ({"created": "2024-01-01T00:00:00"}, "$.fields.created"),
        ],
    )
    def test_malformed_known_value_reports_its_exact_path(
        self, raw: dict, problem_path: str
    ) -> None:
        with pytest.raises(models.IncompleteNormalizationError) as exc_info:
            models.normalize_issue_fields(raw)

        assert [problem.path for problem in exc_info.value.problems] == [problem_path]

    def test_invalid_nested_resources_leave_a_clean_issue_reference(self) -> None:
        raw = {
            "parent": {
                "key": "SYN-1",
                "fields": {
                    "summary": "Useful",
                    "status": {"id": 2, "name": "Open"},
                    "issuetype": "not-an-object",
                },
            }
        }

        with pytest.raises(models.IncompleteNormalizationError) as exc_info:
            models.normalize_issue_fields(raw)

        assert exc_info.value.partial_value == {"parent": {"key": "SYN-1", "summary": "Useful"}}
        assert [problem.path for problem in exc_info.value.problems] == [
            "$.fields.parent.fields.status.id",
            "$.fields.parent.fields.issuetype",
        ]

    def test_multiple_malformed_resources_are_aggregated_with_exact_paths(self) -> None:
        raw = {
            "summary": "Keep me",
            "issuetype": {"id": 10001, "name": "Bug", "self": "https://example.invalid"},
            "status": {
                "id": "3",
                "name": "In Progress",
                "statusCategory": {"key": 7, "self": "https://example.invalid"},
            },
            "components": [
                {"id": "10", "name": "API", "self": "https://example.invalid"},
                {"id": "11", "self": "https://example.invalid"},
            ],
        }

        with pytest.raises(models.IncompleteNormalizationError) as exc_info:
            models.normalize_issue_fields(raw)

        assert exc_info.value.partial_value == {
            "summary": "Keep me",
            "status": {"id": "3", "name": "In Progress"},
            "components": [{"id": "10", "name": "API"}],
        }
        assert [problem.path for problem in exc_info.value.problems] == [
            "$.fields.issuetype.id",
            "$.fields.status.statusCategory.key",
            "$.fields.components[1].name",
        ]
        assert "example.invalid" not in str(exc_info.value)

    @pytest.mark.parametrize(
        ("link", "problem_path"),
        [
            (
                {
                    "type": {"inward": "is blocked by", "outward": "blocks"},
                    "inwardIssue": {"key": "SYN-1"},
                    "outwardIssue": {"key": "SYN-2"},
                },
                "$.fields.issuelinks[0]",
            ),
            (
                {
                    "type": {"inward": "is blocked by"},
                    "outwardIssue": {"key": "SYN-2"},
                },
                "$.fields.issuelinks[0].type.outward",
            ),
            (
                {
                    "type": {"inward": "is blocked by"},
                    "inwardIssue": {"id": "10001"},
                },
                "$.fields.issuelinks[0].inwardIssue.key",
            ),
        ],
    )
    def test_malformed_issue_link_requires_one_direction_relationship_and_issue_key(
        self, link: dict, problem_path: str
    ) -> None:
        with pytest.raises(models.IncompleteNormalizationError) as exc_info:
            models.normalize_issue_fields({"summary": "Still useful", "issuelinks": [link]})

        assert exc_info.value.partial_value == {"summary": "Still useful", "issuelinks": []}
        assert [problem.path for problem in exc_info.value.problems] == [problem_path]

    def test_custom_field_list_is_recursively_normalized(self) -> None:
        raw = {"customfield_10050": ["2024-01-01T00:00:00Z", "plain"]}
        assert models.normalize_issue_fields(raw) == {
            "customfield_10050": ["2024-01-01T00:00:00Z", "plain"]
        }

    def test_custom_field_string_matching_timestamp_shape_but_invalid_is_left_as_is(self) -> None:
        raw = {"customfield_10060": "2024-13-01T00:00:00Z"}
        assert models.normalize_issue_fields(raw) == {"customfield_10060": "2024-13-01T00:00:00Z"}


class TestDataclassShapes:
    def test_page_holds_start_at_total_items(self) -> None:
        page = models.Page(start_at=0, total=2, items=["a", "b"])
        assert page.start_at == 0
        assert page.total == 2
        assert page.items == ["a", "b"]

    def test_search_page_holds_items_and_next_page_token(self) -> None:
        page = models.SearchPage(items=["a"], next_page_token=None)
        assert page.items == ["a"]
        assert page.next_page_token is None

    def test_issue_summary_and_detail_share_key_fields_shape(self) -> None:
        summary = models.IssueSummary(key="ABC-1", fields={"summary": "x"})
        detail = models.IssueDetail(key="ABC-1", fields={})
        assert summary.key == "ABC-1"
        assert detail.fields == {}

    def test_comment_defaults_omit_updated_and_updated_by(self) -> None:
        author = models.User(account_id="a1", display_name="Author")
        comment = models.Comment(
            id="10001", author=author, body="hi", created="2024-01-01T00:00:00Z"
        )
        assert comment.updated is None
        assert comment.updated_by is None

    def test_attachment_shape(self) -> None:
        author = models.User(account_id="a1", display_name="Author")
        attachment = models.Attachment(
            id="90001",
            filename="log.txt",
            mime_type="text/plain",
            size=128,
            author=author,
            created="2024-01-01T00:00:00Z",
        )
        assert attachment.filename == "log.txt"

    def test_download_result_shape(self) -> None:
        result = models.DownloadResult(
            attachment_id="90001",
            filename="log.txt",
            mime_type="text/plain",
            size=128,
            local_path="/cache/90001/log.txt",
        )
        assert result.local_path == "/cache/90001/log.txt"

    def test_changelog_change_uses_from_for_the_reserved_word(self) -> None:
        change = models.ChangelogChange(field="status", from_="To Do", to="In Progress")
        assert change.from_ == "To Do"
        assert change.field_id is None

    def test_changelog_entry_shape(self) -> None:
        author = models.User(account_id="a1", display_name="Author")
        change = models.ChangelogChange(field="status", from_="To Do", to="In Progress")
        entry = models.ChangelogEntry(
            id="20001", author=author, created="2024-01-01T00:00:00Z", changes=[change]
        )
        assert entry.changes == [change]


CANONICAL_MARKDOWN = """# Release notes

Fixed the retry loop in `_request` and added a regression test.

**Note:** this changes *nothing* for callers. See [the PR](https://example.invalid/pr/7).

- first item
- second item

1. one
2. two

> Reviewed by the platform team.

```python
retry(attempts=3)
```"""


class TestMarkdownToAdf:
    """The inverse of adf_to_markdown, over the same node set.

    Round-tripping the canonical corpus is the strongest assertion here: it
    pins both functions to one shared vocabulary instead of letting them
    drift into two dialects.
    """

    def test_round_trips_the_canonical_corpus(self) -> None:
        document = models.markdown_to_adf(CANONICAL_MARKDOWN)
        assert models.adf_to_markdown(document) == CANONICAL_MARKDOWN

    def test_result_is_always_a_versioned_doc_node(self) -> None:
        document = models.markdown_to_adf("hello")
        assert document["type"] == "doc"
        assert document["version"] == 1
        assert isinstance(document["content"], list)

    def test_plain_paragraph(self) -> None:
        assert models.markdown_to_adf("hello world")["content"] == [
            {"type": "paragraph", "content": [{"type": "text", "text": "hello world"}]}
        ]

    def test_blank_lines_separate_paragraphs(self) -> None:
        document = models.markdown_to_adf("first\n\n\n\nsecond")
        assert [block["type"] for block in document["content"]] == ["paragraph", "paragraph"]

    def test_consecutive_lines_become_one_paragraph_with_hard_breaks(self) -> None:
        document = models.markdown_to_adf("line one\nline two")
        assert document["content"] == [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "hardBreak"},
                    {"type": "text", "text": "line two"},
                ],
            }
        ]

    @pytest.mark.parametrize("level", [1, 2, 3, 4, 5, 6])
    def test_atx_headings(self, level: int) -> None:
        document = models.markdown_to_adf(f"{'#' * level} Title")
        assert document["content"] == [
            {
                "type": "heading",
                "attrs": {"level": level},
                "content": [{"type": "text", "text": "Title"}],
            }
        ]

    def test_seventh_hash_level_is_not_a_heading(self) -> None:
        document = models.markdown_to_adf("####### too deep")
        assert document["content"][0]["type"] == "paragraph"

    def test_bullet_list(self) -> None:
        document = models.markdown_to_adf("- a\n- b")
        assert document["content"] == [
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "a"}]}
                        ],
                    },
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "b"}]}
                        ],
                    },
                ],
            }
        ]

    def test_asterisk_bullets_are_a_bullet_list_too(self) -> None:
        assert models.markdown_to_adf("* a\n* b")["content"][0]["type"] == "bulletList"

    def test_ordered_list_renumbers_from_one_on_the_way_back(self) -> None:
        document = models.markdown_to_adf("7. seven\n9. nine")
        assert document["content"][0]["type"] == "orderedList"
        assert models.adf_to_markdown(document) == "1. seven\n2. nine"

    def test_blockquote(self) -> None:
        document = models.markdown_to_adf("> quoted line")
        assert document["content"] == [
            {
                "type": "blockquote",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "quoted line"}]}
                ],
            }
        ]

    def test_fenced_code_block_keeps_its_language_and_raw_text(self) -> None:
        document = models.markdown_to_adf("```sql\nSELECT *\nFROM t -- **not bold**\n```")
        assert document["content"] == [
            {
                "type": "codeBlock",
                "attrs": {"language": "sql"},
                "content": [{"type": "text", "text": "SELECT *\nFROM t -- **not bold**"}],
            }
        ]

    def test_fence_without_a_language_omits_the_attribute(self) -> None:
        document = models.markdown_to_adf("```\nplain\n```")
        assert "attrs" not in document["content"][0]

    def test_unclosed_fence_consumes_the_rest_as_code(self) -> None:
        document = models.markdown_to_adf("```\nstill code\n\n# not a heading")
        assert [block["type"] for block in document["content"]] == ["codeBlock"]
        assert document["content"][0]["content"][0]["text"] == "still code\n\n# not a heading"

    @pytest.mark.parametrize(
        ("source", "mark"),
        [
            ("**bold**", {"type": "strong"}),
            ("*slanted*", {"type": "em"}),
            ("_slanted_", {"type": "em"}),
            ("`literal`", {"type": "code"}),
        ],
    )
    def test_inline_marks(self, source: str, mark: dict) -> None:
        nodes = models.markdown_to_adf(source)["content"][0]["content"]
        assert nodes[0]["marks"] == [mark]

    def test_link(self) -> None:
        paragraph = models.markdown_to_adf("see [docs](https://example.invalid/d)")["content"][0]
        assert paragraph["content"][1] == {
            "type": "text",
            "text": "docs",
            "marks": [{"type": "link", "attrs": {"href": "https://example.invalid/d"}}],
        }

    def test_code_span_suppresses_the_marks_inside_it(self) -> None:
        nodes = models.markdown_to_adf("`a **b** c`")["content"][0]["content"]
        assert nodes == [{"type": "text", "text": "a **b** c", "marks": [{"type": "code"}]}]

    @pytest.mark.parametrize(
        "source",
        [
            "next_page_token and start_at",
            "snake_case_identifier",
            "2 * 3 * 4",
            "a * b",
        ],
    )
    def test_intraword_and_spaced_delimiters_stay_literal(self, source: str) -> None:
        nodes = models.markdown_to_adf(source)["content"][0]["content"]
        assert nodes == [{"type": "text", "text": source}]

    @pytest.mark.parametrize(
        "source",
        [
            "an *unclosed emphasis",
            "an **unclosed strong",
            "an `unclosed code span",
            "an [unclosed](link",
            "a lone *",
            "| a | table |",
            "<b>html</b>",
            "![image](https://example.invalid/i.png)",
            "[ref][1]",
        ],
    )
    def test_unsupported_or_malformed_syntax_survives_as_text(self, source: str) -> None:
        assert _adf_text(models.markdown_to_adf(source)) == source

    def test_crlf_and_cr_are_normalized(self) -> None:
        assert models.markdown_to_adf("a\r\n\r\nb") == models.markdown_to_adf("a\n\nb")
        assert models.markdown_to_adf("a\rb") == models.markdown_to_adf("a\nb")

    @pytest.mark.parametrize("source", ["", "   ", "\n\n", "\t \r\n "])
    def test_empty_input_is_a_value_error_not_an_empty_document(self, source: str) -> None:
        with pytest.raises(ValueError) as exc_info:
            models.markdown_to_adf(source)
        assert "empty" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        "source",
        [
            CANONICAL_MARKDOWN,
            "a lone *",
            "```unclosed",
            "- \n- \n",
            "> \n>\n",
            "#",
            "#no space",
            "1.",
            "***",
            "[]()",
            "`",
            "   leading spaces",
        ],
    )
    def test_every_document_produced_is_structurally_valid_adf(self, source: str) -> None:
        _assert_valid_adf(models.markdown_to_adf(source))


def _adf_text(node: dict) -> str:
    """Every text fragment in a document, concatenated, marks ignored."""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_adf_text(child) for child in node.get("content", []))


_ADF_BLOCK_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "bulletList",
        "orderedList",
        "listItem",
        "blockquote",
        "codeBlock",
    }
)


def _assert_valid_adf(node: dict) -> None:
    """Structural check on a produced document.

    Jira answers a malformed ADF document with a 400 an agent cannot act
    on, so no input may produce one: every node is typed, every text node
    carries non-empty text, and no unknown node type appears.
    """
    assert isinstance(node, dict)
    node_type = node.get("type")
    assert isinstance(node_type, str) and node_type

    if node_type == "text":
        assert isinstance(node.get("text"), str)
        assert node["text"] != ""
        for mark in node.get("marks", []):
            assert isinstance(mark.get("type"), str)
            if mark["type"] == "link":
                assert isinstance(mark["attrs"]["href"], str)
        return

    if node_type == "hardBreak":
        assert "content" not in node
        return

    if node_type == "heading":
        assert node["attrs"]["level"] in range(1, 7)

    if node_type == "doc" or node_type in _ADF_BLOCK_TYPES:
        content = node.get("content", [])
        assert isinstance(content, list)
        for child in content:
            _assert_valid_adf(child)
        return

    raise AssertionError(f"unexpected node type {node_type!r}")
