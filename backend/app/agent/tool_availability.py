"""Offer only tools whose required local workspace exists for this run.

This is a capability filter, not request classification or an authorization
grant. Tool implementations still revalidate scope, policy and approvals.
"""

PROJECT_BOUND_TOOLS = frozenset({
    "git_project_write", "read_project_command_output", "run_project_command",
    "search_project_files", "git_project_status", "git_project_diff",
    "list_project_worktrees", "create_project_worktree",
    "review_project_worktree_removal", "remove_project_worktree",
    "review_project_worktree_archive", "archive_project_worktree",
    "restore_project_worktree", "reconcile_project_worktree",
    "edit_project_file", "list_project_files", "read_project_file",
})


def available_workspace_tools(specs, workspace):
    """A conversation artifact/source does not imply a linked project folder."""
    bound = (isinstance(workspace, dict) and isinstance(workspace.get("project_id"), str)
             and bool(workspace["project_id"]))
    return [spec for spec in specs if bound or spec.get("name") not in PROJECT_BOUND_TOOLS]
