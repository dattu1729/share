"""
Mock tool definitions and responses for the boards-work-item-summary skill.

Exports:
  DEFINITIONS  — list of tool schemas passed to Claude as available tools
  get_response — returns a mock response dict for a given tool call

To add or change a tool, update DEFINITIONS and add a matching branch in
get_response().
"""

# ---------------------------------------------------------------------------
# Tool schemas (sent to Claude as available tools)
# ---------------------------------------------------------------------------

DEFINITIONS = [
    {
        "name": "core_list_projects",
        "description": "Get the list of Azure DevOps projects in the organization.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "wit_my_work_items",
        "description": "Get work items assigned to the current user for a project.",
        "input_schema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "wit_get_work_items_batch_by_ids",
        "description": "Get work item details in batch by their IDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ids": {"type": "array", "items": {"type": "number"}},
                "fields": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["ids"],
        },
    },
    {
        "name": "wit_get_work_item",
        "description": "Get a single work item by ID with optional field expansion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "number"},
                "expand": {"type": "string"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "wit_list_work_item_comments",
        "description": "List all comments for a work item by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {"id": {"type": "number"}},
            "required": ["id"],
        },
    },
    {
        "name": "pipelines_get_build_definitions",
        "description": "Get pipeline/build definitions for a project, with optional name filter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "pipelines_get_builds",
        "description": (
            "Get recent builds for a project, optionally filtered by "
            "definition, branch, status, or result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "definitionId": {"type": "number"},
                "branchName": {"type": "string"},
                "statusFilter": {"type": "string"},
                "resultFilter": {"type": "string"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "pipelines_get_build_status",
        "description": "Get the status and result of a specific build by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "buildId": {"type": "number"},
            },
            "required": ["project", "buildId"],
        },
    },
    {
        "name": "pipelines_get_build_log",
        "description": "Get the list of log entries for a build.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "buildId": {"type": "number"},
            },
            "required": ["project", "buildId"],
        },
    },
    {
        "name": "pipelines_get_build_log_by_id",
        "description": "Get the raw content of a specific build log entry.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "buildId": {"type": "number"},
                "logId": {"type": "number"},
            },
            "required": ["project", "buildId", "logId"],
        },
    },
    {
        "name": "pipelines_get_build_changes",
        "description": "Get the commits/changesets associated with a specific build.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "buildId": {"type": "number"},
            },
            "required": ["project", "buildId"],
        },
    },
    {
        "name": "repo_list_repos_by_project",
        "description": "List repositories available in a project.",
        "input_schema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "advsec_get_alerts",
        "description": "Get Advanced Security alerts for a repository with optional filters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "repository": {"type": "string"},
                "severities": {"type": "array", "items": {"type": "string"}},
                "states": {"type": "array", "items": {"type": "string"}},
                "alertType": {"type": "string"},
                "confidenceLevels": {"type": "array", "items": {"type": "string"}},
                "onlyDefaultBranch": {"type": "boolean"},
            },
            "required": ["project", "repository"],
        },
    },
    {
        "name": "advsec_get_alert_details",
        "description": "Get full details for a specific security alert by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "repository": {"type": "string"},
                "alertId": {"type": "number"},
            },
            "required": ["project", "repository", "alertId"],
        },
    },
    {
        "name": "core_list_project_teams",
        "description": "Get the list of teams for a project.",
        "input_schema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "work_list_iterations",
        "description": "List all iterations defined at the project level.",
        "input_schema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
            "required": ["project"],
        },
    },
    {
        "name": "work_list_team_iterations",
        "description": "List iterations assigned to a specific team.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "team": {"type": "string"},
            },
            "required": ["project", "team"],
        },
    },
    {
        "name": "work_create_iterations",
        "description": "Create new iterations for a project.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "iterations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "startDate": {"type": "string"},
                            "finishDate": {"type": "string"},
                        },
                    },
                },
            },
            "required": ["project", "iterations"],
        },
    },
    {
        "name": "work_assign_iterations",
        "description": "Assign existing project iterations to a team.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "team": {"type": "string"},
                "iterationIds": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["project", "team", "iterationIds"],
        },
    },
]


# ---------------------------------------------------------------------------
# Mock responses
# ---------------------------------------------------------------------------

def get_response(tool_name: str, tool_input: dict) -> dict:
    """Return a realistic mock response for the given Azure DevOps MCP tool call."""

    if tool_name == "core_list_projects":
        return {"value": [{"name": "Contoso"}, {"name": "Fabrikam"}, {"name": "Alpha"}]}

    if tool_name == "wit_my_work_items":
        return {"workItems": [{"id": 101}, {"id": 102}, {"id": 103}]}

    if tool_name == "wit_get_work_items_batch_by_ids":
        ids = tool_input.get("ids", [101, 102, 103])
        types = ["Task", "User Story", "Bug"]
        return {
            "value": [
                {
                    "id": id_,
                    "fields": {
                        "System.Id": id_,
                        "System.Title": f"Work Item {id_}",
                        "System.State": "Active",
                        "System.AssignedTo": {"displayName": "Test User"},
                        "System.WorkItemType": types[id_ % 3],
                        "System.CreatedDate": "2024-01-15T10:00:00Z",
                        "System.ChangedDate": "2024-03-20T14:30:00Z",
                        "System.Tags": "",
                        "Microsoft.VSTS.Common.Priority": 2,
                    },
                }
                for id_ in ids
            ]
        }

    if tool_name == "wit_get_work_item":
        id_ = tool_input.get("id", 0)
        return {
            "id": id_,
            "fields": {
                "System.Id": id_,
                "System.Title": f"Sample Work Item {id_}",
                "System.State": "Active",
                "System.AssignedTo": {"displayName": "Jane Doe"},
                "System.WorkItemType": "User Story",
                "System.CreatedDate": "2024-02-01T09:00:00Z",
                "System.ChangedDate": "2024-03-15T11:00:00Z",
                "System.Tags": "backend; api",
                "Microsoft.VSTS.Common.Priority": 1,
                "System.Description": "Implement the new authentication flow for the API.",
                "System.Parent": 50,
            },
            "relations": [
                {
                    "rel": "System.LinkTypes.Hierarchy-Reverse",
                    "url": "https://dev.azure.com/myorg/Contoso/_apis/wit/workItems/50",
                    "attributes": {"name": "Parent"},
                },
                {
                    "rel": "System.LinkTypes.Hierarchy-Forward",
                    "url": "https://dev.azure.com/myorg/Contoso/_apis/wit/workItems/201",
                    "attributes": {"name": "Child"},
                },
            ],
        }

    if tool_name == "wit_list_work_item_comments":
        return {
            "comments": [
                {
                    "id": 1,
                    "text": "Started implementation. Using JWT for token management.",
                    "createdBy": {"displayName": "Jane Doe"},
                    "createdDate": "2024-03-10T10:00:00Z",
                },
                {
                    "id": 2,
                    "text": "PR created, ready for review.",
                    "createdBy": {"displayName": "John Smith"},
                    "createdDate": "2024-03-14T15:30:00Z",
                },
            ],
            "totalCount": 2,
        }

    if tool_name == "pipelines_get_build_definitions":
        return {
            "value": [
                {"id": 1, "name": "CI-Main"},
                {"id": 2, "name": "CI-PR"},
                {"id": 3, "name": "CD-Production"},
            ]
        }

    if tool_name == "pipelines_get_builds":
        return {
            "value": [
                {
                    "id": 12345,
                    "buildNumber": "20240315.1",
                    "status": "completed",
                    "result": "failed",
                    "sourceBranch": "refs/heads/main",
                    "startTime": "2024-03-15T10:00:00Z",
                    "finishTime": "2024-03-15T10:12:00Z",
                    "requestedBy": {"displayName": "Jane Doe"},
                    "definition": {"name": "CI-Main"},
                },
                {
                    "id": 12344,
                    "buildNumber": "20240314.3",
                    "status": "completed",
                    "result": "succeeded",
                    "sourceBranch": "refs/heads/main",
                    "startTime": "2024-03-14T16:00:00Z",
                    "finishTime": "2024-03-14T16:09:00Z",
                    "requestedBy": {"displayName": "John Smith"},
                    "definition": {"name": "CI-Main"},
                },
            ]
        }

    if tool_name == "pipelines_get_build_status":
        return {
            "id": tool_input.get("buildId"),
            "buildNumber": "20240315.1",
            "status": "completed",
            "result": "failed",
            "sourceBranch": "refs/heads/main",
            "startTime": "2024-03-15T10:00:00Z",
            "finishTime": "2024-03-15T10:12:00Z",
            "requestedBy": {"displayName": "Jane Doe"},
            "definition": {"name": "CI-Main"},
        }

    if tool_name == "pipelines_get_build_log":
        return {
            "value": [
                {"id": 1, "type": "Container", "lineCount": 10, "name": "Initialize Agent"},
                {"id": 2, "type": "Task", "lineCount": 50, "name": "npm install"},
                {
                    "id": 3, "type": "Task", "lineCount": 200,
                    "name": "Run Tests", "result": "failed",
                },
            ]
        }

    if tool_name == "pipelines_get_build_log_by_id":
        return {
            "value": [
                "##[section]Starting: Run Tests",
                "npm test",
                "FAIL src/auth.test.js",
                "  \u25cf AuthService \u203a should validate token",
                "    Expected: 200",
                "    Received: 401",
                "##[error]Process completed with exit code 1.",
                "##[section]Finishing: Run Tests",
            ]
        }

    if tool_name == "pipelines_get_build_changes":
        return {
            "value": [
                {
                    "id": "a1b2c3d4",
                    "author": {"displayName": "Jane Doe"},
                    "message": "fix: correct token validation logic",
                    "timestamp": "2024-03-15T09:45:00Z",
                },
                {
                    "id": "f0e1d2c3",
                    "author": {"displayName": "John Smith"},
                    "message": "chore: update dependencies",
                    "timestamp": "2024-03-14T16:00:00Z",
                },
            ]
        }

    if tool_name == "repo_list_repos_by_project":
        return {
            "value": [
                {"name": "MyApp"}, {"name": "Backend"},
                {"name": "Frontend"}, {"name": "API"},
            ]
        }

    if tool_name == "advsec_get_alerts":
        return {
            "value": [
                {
                    "alertId": 42,
                    "title": "Prototype Pollution in lodash",
                    "severity": "Critical",
                    "state": "Active",
                    "alertType": "Dependency",
                    "rule": {"id": "GHSA-4xc9-xhrj-v574", "name": "GHSA-4xc9-xhrj-v574"},
                    "firstSeenDate": "2024-01-10T00:00:00Z",
                },
                {
                    "alertId": 43,
                    "title": "Exposed API key in config.js",
                    "severity": "High",
                    "state": "Active",
                    "alertType": "Secret",
                    "rule": {"id": "secret/api-key", "name": "Exposed API Key"},
                    "firstSeenDate": "2024-02-05T00:00:00Z",
                },
                {
                    "alertId": 44,
                    "title": "SQL injection risk in query builder",
                    "severity": "Medium",
                    "state": "Active",
                    "alertType": "Code",
                    "rule": {"id": "js/sql-injection", "name": "SQL Injection"},
                    "firstSeenDate": "2024-03-01T00:00:00Z",
                },
            ],
            "continuationToken": None,
        }

    if tool_name == "advsec_get_alert_details":
        return {
            "alertId": tool_input.get("alertId"),
            "title": "Prototype Pollution in lodash",
            "severity": "Critical",
            "state": "Active",
            "alertType": "Dependency",
            "description": (
                "Versions of lodash prior to 4.17.21 are vulnerable to Prototype Pollution "
                "via the defaultsDeep function."
            ),
            "physicalLocations": [{"filePath": "package.json", "region": {"startLine": 12}}],
            "remediation": "Update lodash to version 4.17.21 or later.",
            "confidence": "High",
            "validity": "Active",
            "toolName": "Dependency Scanning",
            "rule": {"id": "GHSA-4xc9-xhrj-v574"},
        }

    if tool_name == "core_list_project_teams":
        return {"value": [{"name": "Contoso Team"}, {"name": "Alpha Team"}, {"name": "Beta Team"}]}

    if tool_name == "work_list_iterations":
        return {
            "value": [
                {
                    "id": "iter-1",
                    "name": "Iteration 1",
                    "path": "Contoso\\Iteration 1",
                    "attributes": {"startDate": "2024-01-01", "finishDate": "2024-01-14"},
                },
                {
                    "id": "iter-2",
                    "name": "Iteration 2",
                    "path": "Contoso\\Iteration 2",
                    "attributes": {"startDate": "2024-01-15", "finishDate": "2024-01-28"},
                },
                {
                    "id": "iter-3",
                    "name": "Iteration 3",
                    "path": "Contoso\\Iteration 3",
                    "attributes": {"startDate": "2024-01-29", "finishDate": "2024-02-11"},
                },
            ]
        }

    if tool_name == "work_list_team_iterations":
        return {
            "value": [
                {
                    "id": "iter-1",
                    "name": "Iteration 1",
                    "attributes": {"startDate": "2024-01-01", "finishDate": "2024-01-14"},
                },
                {
                    "id": "iter-2",
                    "name": "Iteration 2",
                    "attributes": {"startDate": "2024-01-15", "finishDate": "2024-01-28"},
                },
            ]
        }

    if tool_name == "work_create_iterations":
        iterations = tool_input.get("iterations", [])
        return {
            "created": [
                {
                    "id": f"iter-new-{i + 1}",
                    "name": it.get("name", f"New Iteration {i + 1}"),
                    "attributes": {
                        "startDate": it.get("startDate", ""),
                        "finishDate": it.get("finishDate", ""),
                    },
                }
                for i, it in enumerate(iterations)
            ]
        }

    if tool_name == "work_assign_iterations":
        return {"assigned": tool_input.get("iterationIds", [])}

    return {"error": f"Unknown tool: {tool_name}", "toolName": tool_name}
