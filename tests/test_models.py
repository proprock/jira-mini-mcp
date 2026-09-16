"""Tests for jira_mini_mcp.models: dataclasses and value normalization.

Fixtures under tests/fixtures/ are hand-authored synthetic JSON, not live
Jira captures -- Phase 3 is pure data-shape/normalization logic and does
not require live-Jira-derived evidence (that starts at Phase 4's HTTP
interactions).
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

    def test_null_optional_fields_are_omitted_not_null(self) -> None:
        fixture = _load("issue_full_fields.json")
        result = models.normalize_issue_fields(fixture["fields"])
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

    def test_malformed_user_field_is_skipped_not_crashed(self) -> None:
        assert models.normalize_issue_fields({"assignee": "not-a-user-object"}) == {}

    def test_malformed_date_field_is_skipped_not_crashed(self) -> None:
        assert models.normalize_issue_fields({"created": 12345}) == {}

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
