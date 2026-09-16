"""Tests for jira_mini_mcp.jira: JiraClient core, search_issues, get_issue.

Fixtures under tests/fixtures/jira_*.json trace to live, read-only requests
against a Jira Cloud test site on 2026-09-16 (GET /rest/api/3/search/jql and
GET /rest/api/3/issue/{key}), with every tenant/account/content value
replaced by synthetic data. See each fixture's `_provenance` note.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from jira_mini_mcp import errors, jira
from jira_mini_mcp.auth import BasicTokenAuth
from jira_mini_mcp.jira import JiraClient
from jira_mini_mcp.models import IssueDetail, IssueSummary, SearchPage

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://synthetic-tenant.atlassian.net"

pytestmark = pytest.mark.anyio


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _json_response(status_code: int, body: Any) -> httpx2.Response:
    return httpx2.Response(status_code, json=body)


Handler = Callable[[httpx2.Request], httpx2.Response]


def _make_client(handler: Handler) -> JiraClient:
    transport = httpx2.MockTransport(handler)
    http_client = httpx2.AsyncClient(transport=transport)
    auth = BasicTokenAuth("agent@example.com", "super-secret-token")
    return JiraClient(http_client, auth, BASE_URL)


def _recording_handler(response: httpx2.Response) -> tuple[Handler, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return response

    return handler, seen


class TestPrivateRequestHelper:
    async def test_attaches_basic_auth_header(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN")

        auth_header = seen[0].headers["Authorization"]
        assert auth_header.startswith("Basic ")
        decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode()
        assert decoded == "agent@example.com:super-secret-token"

    async def test_network_failure_raises_network_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("connection refused")

        client = _make_client(handler)
        with pytest.raises(errors.JiraNetworkError) as exc_info:
            await client.search_issues("project = SYN")
        assert exc_info.value.operation == "search_issues"

    async def test_timeout_raises_timeout_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out")

        client = _make_client(handler)
        with pytest.raises(errors.JiraTimeoutError) as exc_info:
            await client.search_issues("project = SYN")
        assert exc_info.value.operation == "search_issues"

    async def test_non_json_success_body_raises_server_error(self) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, text="<html>not json</html>")

        client = _make_client(handler)
        with pytest.raises(errors.JiraServerError):
            await client.search_issues("project = SYN")


class TestSearchIssues:
    async def test_uses_enhanced_v3_search_path(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN")

        assert seen[0].method == "GET"
        assert seen[0].url.path == "/rest/api/3/search/jql"

    async def test_default_limit_and_fields(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN")

        params = seen[0].url.params
        assert params["maxResults"] == "20"
        assert params["fields"] == ",".join(jira.SEARCH_DEFAULT_FIELDS)
        assert "nextPageToken" not in params

    async def test_explicit_fields_replace_default(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", fields=["summary", "status"])

        assert seen[0].url.params["fields"] == "summary,status"

    async def test_empty_fields_list_requests_no_fields(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", fields=[])

        assert seen[0].url.params["fields"] == ""

    @pytest.mark.parametrize("limit", [1, 100])
    async def test_limit_boundaries_accepted(self, limit: int) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", limit=limit)

        assert seen[0].url.params["maxResults"] == str(limit)

    @pytest.mark.parametrize("limit", [0, -1, 101])
    async def test_invalid_limit_raises_actionable_error_without_http_call(
        self, limit: int
    ) -> None:
        def handler(request: httpx2.Request) -> httpx2.Response:
            raise AssertionError("no HTTP call should be made for an invalid limit")

        client = _make_client(handler)
        with pytest.raises(errors.JiraValidationError) as exc_info:
            await client.search_issues("project = SYN", limit=limit)
        message = str(exc_info.value)
        assert "1..100" in message
        assert "positive" in message

    async def test_page_token_passthrough_unchanged(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"issues": [], "isLast": True}))
        client = _make_client(handler)

        await client.search_issues("project = SYN", page_token="opaque-cursor-value")

        assert seen[0].url.params["nextPageToken"] == "opaque-cursor-value"

    async def test_sequential_pages_pass_returned_token_to_next_call(self) -> None:
        page1 = _load("jira_search_page1.json")
        page2 = _load("jira_search_page2.json")
        responses = [_json_response(200, page1), _json_response(200, page2)]
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return responses.pop(0)

        client = _make_client(handler)

        first = await client.search_issues("project = SYN")
        assert first.next_page_token == "synthetic-cursor-page-2"
        assert "nextPageToken" not in seen[0].url.params

        second = await client.search_issues("project = SYN", page_token=first.next_page_token)
        assert second.next_page_token is None
        assert seen[1].url.params["nextPageToken"] == "synthetic-cursor-page-2"

    async def test_first_page_items_shape(self) -> None:
        page1 = _load("jira_search_page1.json")
        handler, _ = _recording_handler(_json_response(200, page1))
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert isinstance(result, SearchPage)
        assert len(result.items) == 2
        first_item = result.items[0]
        assert isinstance(first_item, IssueSummary)
        assert first_item.key == "SYN-201"
        assert first_item.fields["summary"] == "Search index falls behind after bulk import"
        assert first_item.fields["assignee"].account_id == "syn-acc-101"
        assert first_item.fields["issuetype"] == {"id": "10001", "name": "Bug"}
        assert first_item.fields["status"] == {
            "id": "3",
            "name": "In Progress",
            "category": "indeterminate",
        }
        assert first_item.fields["priority"] == {"id": "2", "name": "High"}
        assert first_item.fields["project"] == {
            "id": "10000",
            "key": "SYN",
            "name": "Synthetic Project",
        }
        assert "self" not in first_item.fields
        second_item = result.items[1]
        assert "assignee" not in second_item.fields  # null assignee is omitted, not null

    async def test_last_page_has_no_next_token(self) -> None:
        page2 = _load("jira_search_page2.json")
        handler, _ = _recording_handler(_json_response(200, page2))
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert result.next_page_token is None

    async def test_empty_result(self) -> None:
        empty = _load("jira_search_empty.json")
        handler, _ = _recording_handler(_json_response(200, empty))
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert result.items == []
        assert result.next_page_token is None

    async def test_malformed_payload_raises_instead_of_empty_result(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"issues": "not-a-list"}))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.search_issues("project = SYN")

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {"issues": [], "nextPageToken": 123},
        ],
    )
    async def test_malformed_search_envelope_raises_server_error(self, body: Any) -> None:
        handler, _ = _recording_handler(_json_response(200, body))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.search_issues("project = SYN")

    @pytest.mark.parametrize(
        ("raw_issue", "problem"),
        [
            ("not-an-object", "$.issues[0]: expected an object"),
            (
                {"key": "SYN-1", "fields": "not-an-object"},
                "$.issues[0].fields: expected an object",
            ),
        ],
    )
    async def test_malformed_search_item_keeps_only_clean_partial_data(
        self, raw_issue: Any, problem: str
    ) -> None:
        handler, _ = _recording_handler(_json_response(200, {"issues": [raw_issue]}))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.search_issues("project = SYN")

        assert exc_info.value.problems == (problem,)

    async def test_null_search_fields_are_treated_as_absent(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"issues": [{"key": "SYN-1", "fields": None}]})
        )
        client = _make_client(handler)

        result = await client.search_issues("project = SYN")

        assert result.items == [IssueSummary(key="SYN-1", fields={})]

    async def test_malformed_resource_raises_with_sanitized_partial_search_page(self) -> None:
        raw = {
            "issues": [
                {"key": "SYN-1", "fields": {"summary": "Valid"}},
                {
                    "key": "SYN-2",
                    "fields": {
                        "summary": "Partially valid",
                        "priority": {
                            "id": 2,
                            "name": "High",
                            "self": "https://synthetic-tenant.atlassian.net/priority/2",
                        },
                    },
                },
            ],
            "nextPageToken": "synthetic-next",
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.search_issues("project = SYN")

        assert exc_info.value.problems == (
            "$.issues[1].fields.priority.id: expected a non-empty string",
        )
        assert exc_info.value.partial_result == {
            "items": [
                {"key": "SYN-1", "fields": {"summary": "Valid"}},
                {"key": "SYN-2", "fields": {"summary": "Partially valid"}},
            ],
            "next_page_token": "synthetic-next",
        }
        assert "synthetic-tenant" not in repr(exc_info.value.partial_result)

    async def test_missing_issue_key_is_reported_without_losing_valid_items(self) -> None:
        raw = {
            "issues": [
                {"key": "SYN-1", "fields": {"summary": "Valid"}},
                {"id": "10002", "fields": {"summary": "No key"}},
            ]
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.search_issues("project = SYN")

        assert exc_info.value.problems == ("$.issues[1].key: expected a non-empty string",)
        assert exc_info.value.partial_result == {
            "items": [{"key": "SYN-1", "fields": {"summary": "Valid"}}],
            "next_page_token": None,
        }

    async def test_response_has_only_items_and_next_page_token_fields(self) -> None:
        field_names = {f.name for f in dataclasses.fields(SearchPage)}
        assert field_names == {"items", "next_page_token"}

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
            (503, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.search_issues("project = SYN")


class TestGetIssue:
    async def test_uses_v3_issue_path(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1")

        assert seen[0].method == "GET"
        assert seen[0].url.path == "/rest/api/3/issue/SYN-1"

    async def test_default_fields(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1")

        assert seen[0].url.params["fields"] == ",".join(jira.ISSUE_DEFAULT_FIELDS)

    async def test_explicit_fields_replace_default(self) -> None:
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1", fields=["summary"])

        assert seen[0].url.params["fields"] == "summary"

    async def test_empty_fields_list_uses_sentinel_not_upstream_default(self) -> None:
        # Jira Cloud v3's single-issue GET treats fields="" and fields=-* as
        # unspecified and returns a tenant- and permission-dependent full set.
        # The non-field issue id is a stable sentinel for an empty fields object.
        handler, seen = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        await client.get_issue("SYN-1", fields=[])

        sent = seen[0].url.params["fields"]
        assert sent != ""
        assert sent not in jira.ISSUE_DEFAULT_FIELDS

    async def test_empty_fields_list_returns_empty_fields_object(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": {}}))
        client = _make_client(handler)

        result = await client.get_issue("SYN-1", fields=[])

        assert result == IssueDetail(key="SYN-1", fields={})

    async def test_full_response_shape_and_normalization(self) -> None:
        fixture = _load("jira_issue_detail.json")
        handler, _ = _recording_handler(_json_response(200, fixture["raw"]))
        client = _make_client(handler)

        result = await client.get_issue("SYN-301")

        assert result.key == "SYN-301"
        assert result.fields["summary"] == fixture["expected_normalized_fields"]["summary"]
        assert result.fields["updated"] == fixture["expected_normalized_fields"]["updated"]
        assert result.fields["assignee"].account_id == "syn-acc-201"
        assert result.fields["status"] == {
            "id": "3",
            "name": "In Progress",
            "category": "indeterminate",
        }
        assert "resolutiondate" not in result.fields  # null is omitted, not emitted as null

    async def test_malformed_resource_raises_with_sanitized_partial_issue(self) -> None:
        raw = {
            "key": "SYN-1",
            "fields": {
                "summary": "Useful",
                "project": {
                    "id": "10000",
                    "name": "Missing key",
                    "self": "https://synthetic-tenant.atlassian.net/project/10000",
                },
            },
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_issue("SYN-1")

        assert exc_info.value.problems == ("$.fields.project.key: expected a non-empty string",)
        assert exc_info.value.partial_result == {
            "key": "SYN-1",
            "fields": {"summary": "Useful"},
        }
        assert "synthetic-tenant" not in repr(exc_info.value.partial_result)

    async def test_omits_comments_attachments_changelog_even_if_present_in_raw_envelope(
        self,
    ) -> None:
        raw = {
            "key": "SYN-1",
            "fields": {"summary": "Has embedded history"},
            "changelog": {"histories": [{"id": "1"}]},
        }
        handler, _ = _recording_handler(_json_response(200, raw))
        client = _make_client(handler)

        result = await client.get_issue("SYN-1")

        assert result == IssueDetail(key="SYN-1", fields={"summary": "Has embedded history"})

    async def test_not_found_raises_with_issue_key(self) -> None:
        fixture = _load("jira_issue_not_found.json")
        handler, _ = _recording_handler(_json_response(404, fixture))
        client = _make_client(handler)

        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            await client.get_issue("SYN-404")

        assert exc_info.value.issue_key == "SYN-404"
        assert exc_info.value.status_code == 404

    async def test_malformed_payload_missing_key_raises_instead_of_crashing(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"fields": {"summary": "no key"}}))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_issue("SYN-1")

    async def test_non_object_issue_envelope_raises_server_error(self) -> None:
        handler, _ = _recording_handler(_json_response(200, []))
        client = _make_client(handler)

        with pytest.raises(errors.JiraServerError):
            await client.get_issue("SYN-1")

    async def test_null_issue_fields_are_treated_as_absent(self) -> None:
        handler, _ = _recording_handler(_json_response(200, {"key": "SYN-1", "fields": None}))
        client = _make_client(handler)

        assert await client.get_issue("SYN-1") == IssueDetail(key="SYN-1", fields={})

    async def test_non_object_issue_fields_raise_with_clean_partial_result(self) -> None:
        handler, _ = _recording_handler(
            _json_response(200, {"key": "SYN-1", "fields": "not-an-object"})
        )
        client = _make_client(handler)

        with pytest.raises(errors.JiraIncompleteResponseError) as exc_info:
            await client.get_issue("SYN-1")

        assert exc_info.value.problems == ("$.fields: expected an object",)
        assert exc_info.value.partial_result == {"key": "SYN-1", "fields": {}}

    @pytest.mark.parametrize(
        ("status", "exc_class"),
        [
            (401, errors.JiraAuthenticationError),
            (403, errors.JiraPermissionError),
            (404, errors.JiraNotFoundError),
            (429, errors.JiraRateLimitError),
            (500, errors.JiraServerError),
        ],
    )
    async def test_error_status_codes_map_to_expected_exception(
        self, status: int, exc_class: type[Exception]
    ) -> None:
        handler, _ = _recording_handler(
            _json_response(status, {"errorMessages": ["synthetic failure"], "errors": {}})
        )
        client = _make_client(handler)

        with pytest.raises(exc_class):
            await client.get_issue("SYN-1")
