from pathlib import Path
import json
import subprocess


BUNDLE = Path(__file__).parents[1] / "plugin" / "dashboard" / "dist" / "index.js"


def test_dashboard_loads_settings_even_when_assignment_log_fails() -> None:
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'api("/settings").then(setSettings)' in source
    assert 'api("/assignments").then((result) => setAssignments(result.assignments || []))' in source
    assert "Promise.all([api(\"/settings\"), api(\"/assignments\")])" not in source


def test_grant_folder_form_is_collapsed_by_default() -> None:
    source = BUNDLE.read_text(encoding="utf-8")
    assert 'h("details", { className: "rounded border p-3" }' in source
    assert 'h("summary", { className: "cursor-pointer font-medium" }, "Grant a folder")' in source


def test_assignment_wording_is_not_misrouted_as_hangup() -> None:
    routing = Path(__file__).parents[1] / "rex_voice_v1" / "routing.ts"
    script = (
        "import { requestedAction, routeTurn } from "
        f"'{routing.as_uri()}'; "
        "const values = ["
        "requestedAction('Please hang up'),"
        "requestedAction('I am giving you an assignment to write a story whenever I hang up'),"
        "routeTurn('I am giving you an assignment to write a story whenever I hang up', false)"
        "]; console.log(JSON.stringify(values));"
    )
    result = subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == [
        "phone_hangup",
        "assignment_capture",
        {"activeTools": ["assignment_capture"], "requested": "assignment_capture"},
    ]
