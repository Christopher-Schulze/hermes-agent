"""Unit tests: CDP supervisor attaches a dedicated page per task_id (#69727)."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest


def _make_supervisor() -> Any:
    import threading
    from tools.browser_supervisor import CDPSupervisor

    sup = object.__new__(CDPSupervisor)
    sup.task_id = "session-A"
    sup.cdp_url = "ws://example.test/cdp"
    sup._state_lock = threading.Lock()
    sup._active = False
    sup._page_session_id = None
    sup._page_target_id = None
    sup._owns_page_target = False
    sup._child_sessions = {}
    sup._loop = None
    return sup


@pytest.mark.asyncio
async def test_attach_creates_dedicated_page_even_when_pages_exist():
    """Existing page targets must not be adopted by a second Hermes session."""
    sup = _make_supervisor()
    calls: List[Dict[str, Any]] = []

    async def fake_cdp(
        method: str,
        params: Optional[dict] = None,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> dict:
        calls.append({"method": method, "params": params, "session_id": session_id})
        if method == "Target.createTarget":
            return {"result": {"targetId": "NEW-TAB-A"}}
        if method == "Target.attachToTarget":
            return {"result": {"sessionId": "SID-A"}}
        if method in {
            "Page.enable",
            "Runtime.enable",
            "Target.setAutoAttach",
            "Page.addScriptToEvaluateOnNewDocument",
            "Fetch.enable",
        }:
            return {"result": {}}
        if method == "Target.getTargets":
            # Would previously steal EXISTING-SHARED — must not happen.
            return {
                "result": {
                    "targetInfos": [
                        {"targetId": "EXISTING-SHARED", "type": "page", "url": "https://baidu.com"},
                        {"targetId": "OTHER", "type": "page", "url": "https://sina.com"},
                    ]
                }
            }
        return {"result": {}}

    # Bypass dialog bridge install (Fetch/addScript) complexity for this unit.
    async def _noop_bridge(_session_id: str) -> None:
        return None

    sup._cdp = fake_cdp
    sup._install_dialog_bridge = _noop_bridge

    await sup._attach_initial_page()

    create_calls = [c for c in calls if c["method"] == "Target.createTarget"]
    assert len(create_calls) == 1
    assert create_calls[0]["params"] == {"url": "about:blank"}

    attach_calls = [c for c in calls if c["method"] == "Target.attachToTarget"]
    assert len(attach_calls) == 1
    assert attach_calls[0]["params"] == {"targetId": "NEW-TAB-A", "flatten": True}

    # Must not have preferred the existing shared page.
    assert all(
        c["params"].get("targetId") != "EXISTING-SHARED"
        for c in attach_calls
        if c.get("params")
    )
    assert sup._page_target_id == "NEW-TAB-A"
    assert sup._owns_page_target is True
    assert sup._page_session_id == "SID-A"


@pytest.mark.asyncio
async def test_missing_created_target_id_never_adopts_a_shared_page():
    """An invalid create response must fail instead of restoring the original shared-tab bug."""
    sup = _make_supervisor()
    methods = []

    async def cdp(method, params=None, session_id=None, timeout=10.0):
        methods.append(method)
        if method == "Target.createTarget":
            return {"result": {}}
        return {"result": {"targetInfos": [{"targetId": "SHARED", "type": "page"}]}}

    sup._cdp = cdp
    with pytest.raises(RuntimeError, match="Target.createTarget returned no targetId"):
        await sup._resolve_dedicated_page_target()
    assert methods == ["Target.createTarget"]
    assert sup._page_target_id is None


@pytest.mark.asyncio
async def test_two_supervisors_get_distinct_page_targets():
    """Simulates two Hermes sessions against one shared CDP browser."""
    created_ids = ["TAB-1", "TAB-2"]
    create_index = {"i": 0}

    async def make_attach(sup_name: str):
        sup = _make_supervisor()
        sup.task_id = sup_name
        attached: List[str] = []

        async def fake_cdp(
            method: str,
            params: Optional[dict] = None,
            session_id: Optional[str] = None,
            timeout: float = 10.0,
        ) -> dict:
            if method == "Target.createTarget":
                tid = created_ids[create_index["i"]]
                create_index["i"] += 1
                return {"result": {"targetId": tid}}
            if method == "Target.attachToTarget":
                assert params is not None
                attached.append(params["targetId"])
                return {"result": {"sessionId": f"SID-{params['targetId']}"}}
            if method == "Target.getTargets":
                return {
                    "result": {
                        "targetInfos": [
                            {"targetId": "SHARED", "type": "page", "url": "about:blank"},
                        ]
                    }
                }
            return {"result": {}}

        async def _noop_bridge(_session_id: str) -> None:
            return None

        sup._cdp = fake_cdp
        sup._install_dialog_bridge = _noop_bridge
        await sup._attach_initial_page()
        return sup, attached

    a, attached_a = await make_attach("session-A")
    b, attached_b = await make_attach("session-B")

    assert a._page_target_id == "TAB-1"
    assert b._page_target_id == "TAB-2"
    assert a._page_target_id != b._page_target_id
    assert attached_a == ["TAB-1"]
    assert attached_b == ["TAB-2"]
    assert "SHARED" not in attached_a + attached_b


@pytest.mark.asyncio
async def test_reconnect_reuses_owned_page_target():
    """After reconnect, re-attach the same dedicated tab when it still exists."""
    sup = _make_supervisor()
    sup._page_target_id = "OWNED-TAB"
    sup._owns_page_target = True
    methods: List[str] = []

    async def fake_cdp(
        method: str,
        params: Optional[dict] = None,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> dict:
        methods.append(method)
        if method == "Target.getTargets":
            return {
                "result": {
                    "targetInfos": [
                        {"targetId": "OWNED-TAB", "type": "page", "url": "about:blank"},
                        {"targetId": "OTHER", "type": "page", "url": "https://example.com"},
                    ]
                }
            }
        if method == "Target.createTarget":
            raise AssertionError("must not create a new tab when owned page still exists")
        if method == "Target.attachToTarget":
            assert params is not None
            assert params["targetId"] == "OWNED-TAB"
            return {"result": {"sessionId": "SID-REUSE"}}
        return {"result": {}}

    async def _noop_bridge(_session_id: str) -> None:
        return None

    sup._cdp = fake_cdp
    sup._install_dialog_bridge = _noop_bridge

    await sup._attach_initial_page()

    assert "Target.createTarget" not in methods
    assert methods.count("Target.getTargets") == 1
    assert sup._page_target_id == "OWNED-TAB"
    assert sup._page_session_id == "SID-REUSE"


def test_navigate_page_uses_owned_session_and_activates_target(monkeypatch):
    """Public navigation must hit the dedicated page session (#69727 review)."""
    sup = _make_supervisor()
    sup._active = True
    sup._page_session_id = "SID-NAV"
    sup._page_target_id = "TAB-NAV"
    sup._owns_page_target = True
    methods: List[tuple] = []

    async def fake_cdp(
        method: str,
        params: Optional[dict] = None,
        session_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> dict:
        methods.append((method, params, session_id))
        if method == "Page.navigate":
            return {"result": {"frameId": "frame-1", "loaderId": "load-1"}}
        return {"result": {}}

    class _Loop:
        def is_running(self) -> bool:
            return True

    class _Fut:
        def __init__(self, value):
            self._value = value

        def result(self, timeout=None):
            return self._value

    def schedule(coro, loop):
        # Run the coroutine on a private loop to completion.
        loop_local = asyncio.new_event_loop()
        try:
            return _Fut(loop_local.run_until_complete(coro))
        finally:
            loop_local.close()

    monkeypatch.setattr(
        "agent.async_utils.safe_schedule_threadsafe", schedule
    )
    sup._loop = _Loop()
    sup._cdp = fake_cdp

    result = sup.navigate_page("https://example.com/search")

    assert result["ok"] is True
    assert result["target_id"] == "TAB-NAV"
    assert ("Target.activateTarget", {"targetId": "TAB-NAV"}, None) in methods
    assert any(
        m[0] == "Page.navigate"
        and m[1] == {"url": "https://example.com/search"}
        and m[2] == "SID-NAV"
        for m in methods
    )


@pytest.mark.parametrize("command,args,payload", [
    ("click", ["@e1"], {"clicked": "@e1"}),
    ("snapshot", ["-c"], {"snapshot": "- button Click", "refs": {"e1": {"role": "button"}}}),
])
@pytest.mark.parametrize("previous,close_succeeds", [(None, True), ("owned", True), ("old", True), ("old", False)])
def test_cdp_command_uses_owned_endpoint_and_replaces_stale_daemon(
    monkeypatch, command, args, payload, previous, close_succeeds,
):
    """Keep valid refs on the same endpoint; never reuse a daemon bound to a retired target."""
    from tools import browser_supervisor, browser_tool_session

    supervisor = MagicMock()
    supervisor.page_target_id.return_value = "target"
    supervisor.page_command_endpoint.return_value = "ws://127.0.0.1/owned"
    monkeypatch.setattr(browser_supervisor.SUPERVISOR_REGISTRY, "get", lambda task_id: supervisor)
    session = {"session_name": "task", "cdp_url": "wss://browser.test/remote"}
    if previous is not None:
        session["_page_command_endpoint"] = f"ws://127.0.0.1/{previous}"
    calls = []

    def collect(task_id, info, argv, operation, engine, timeout):
        calls.append((argv, operation))
        if operation == "close":
            return {"success": close_succeeds, "error": "close failed"}
        return {"success": True, "data": payload}

    monkeypatch.setattr(browser_tool_session, "_spawn_and_collect", collect)
    result = browser_tool_session._run_cdp_page_command(
        "task", session, ["agent-browser", "--cdp", session["cdp_url"]], command, args, "auto", 10,
    )
    if previous == "old" and not close_succeeds:
        assert result == {"success": False, "error": "close failed"}
        assert session["_page_command_endpoint"] == "ws://127.0.0.1/old"
        assert [operation for _, operation in calls] == ["close"]
    else:
        assert result == {"success": True, "data": payload}
        assert session["_page_command_endpoint"] == "ws://127.0.0.1/owned"
        assert [operation for _, operation in calls] == (["close", command] if previous == "old" else [command])
        assert calls[-1][0][-len(args)-1:] == [command, *args]
    assert all(argv[:4] == ["agent-browser", "--cdp", "ws://127.0.0.1/owned", "--json"] for argv, _ in calls)
    supervisor.page_command_endpoint.assert_called_once_with(timeout=10)


def test_browser_navigate_cdp_uses_supervisor_page(monkeypatch):
    """browser_navigate on a CDP session must not fall through to unbound CLI."""
    import json
    import tools.browser_tool as bt
    import tools.browser_tool_session as _session
    import tools.browser_tool_cdp as _cdp
    import tools.browser_tool_cloud as _cloud
    import tools.browser_supervisor as bsup

    session = {
        "session_name": "cdp_test",
        "cdp_url": "ws://127.0.0.1:9222/devtools/browser/x",
        "page_target_id": "TASK-TAB",
        "_first_nav": True,
    }

    class _Sup:
        def page_target_id(self):
            return "TASK-TAB"

        def navigate_page(self, url, timeout=30.0):
            return {
                "ok": True,
                "target_id": "TASK-TAB",
                "frame_id": "f1",
                "loader_id": "l1",
            }

    class _Reg:
        def get(self, task_id):
            return _Sup()

    monkeypatch.setattr(_session, "_get_session_info", lambda key: session)
    monkeypatch.setattr(_cdp, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(_session, "_bind_session_page_target", lambda tid, info: None)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(_cloud, "_is_local_backend", lambda: False)
    monkeypatch.setattr(_cloud, "_allow_private_urls", lambda: True)
    monkeypatch.setattr(bt, "_is_always_blocked_url", lambda url: False)
    monkeypatch.setattr(bt, "check_website_access", lambda url: None)
    monkeypatch.setattr(_cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(bt, "_maybe_start_recording", lambda key: None)
    monkeypatch.setattr(bt, "_sensitive_query_param_name", lambda url: None)
    monkeypatch.setattr(bt, "_normalize_url_for_request", lambda url: url)
    monkeypatch.setattr(bsup, "SUPERVISOR_REGISTRY", _Reg())

    def _fail_cli(*a, **k):
        raise AssertionError("must not call agent-browser CLI for CDP navigate")

    monkeypatch.setattr(_session, "_run_browser_command", _fail_cli)

    out = json.loads(bt.browser_navigate("https://www.baidu.com", task_id="sess-A"))
    assert out["success"] is True
    assert out["page_target_id"] == "TASK-TAB"
    assert out["via"] == "cdp_supervisor"


def test_two_task_navigate_paths_keep_distinct_targets(monkeypatch):
    """Two task_ids must route navigate to different page targets."""
    import json
    import tools.browser_tool as bt
    import tools.browser_tool_session as _session
    import tools.browser_tool_cdp as _cdp
    import tools.browser_tool_cloud as _cloud
    import tools.browser_supervisor as bsup

    sessions = {
        "sess-A": {
            "session_name": "cdp_a",
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/x",
            "_first_nav": True,
        },
        "sess-B": {
            "session_name": "cdp_b",
            "cdp_url": "ws://127.0.0.1:9222/devtools/browser/x",
            "_first_nav": True,
        },
    }
    seen: Dict[str, str] = {}

    class _Sup:
        def __init__(self, tid: str, tab: str):
            self.tid = tid
            self.tab = tab

        def page_target_id(self):
            return self.tab

        def navigate_page(self, url, timeout=30.0):
            seen[self.tid] = self.tab
            return {"ok": True, "target_id": self.tab, "frame_id": "f"}

    class _Reg:
        def get(self, task_id):
            tab = "TAB-A" if task_id == "sess-A" else "TAB-B"
            return _Sup(task_id, tab)

    monkeypatch.setattr(_session, "_get_session_info", lambda key: sessions[key])
    monkeypatch.setattr(_cdp, "_ensure_cdp_supervisor", lambda task_id: None)
    monkeypatch.setattr(_session, "_bind_session_page_target", lambda tid, info: None)
    monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
    monkeypatch.setattr(_cloud, "_is_local_backend", lambda: False)
    monkeypatch.setattr(_cloud, "_allow_private_urls", lambda: True)
    monkeypatch.setattr(bt, "_is_always_blocked_url", lambda url: False)
    monkeypatch.setattr(bt, "check_website_access", lambda url: None)
    monkeypatch.setattr(_cloud, "_get_cloud_provider", lambda: None)
    monkeypatch.setattr(bt, "_maybe_start_recording", lambda key: None)
    monkeypatch.setattr(bt, "_sensitive_query_param_name", lambda url: None)
    monkeypatch.setattr(bt, "_normalize_url_for_request", lambda url: url)
    monkeypatch.setattr(bsup, "SUPERVISOR_REGISTRY", _Reg())
    monkeypatch.setattr(
        _session,
        "_run_browser_command",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no CLI")),
    )

    a = json.loads(bt.browser_navigate("https://www.baidu.com", task_id="sess-A"))
    b = json.loads(bt.browser_navigate("https://www.sina.com.cn", task_id="sess-B"))
    assert a["page_target_id"] == "TAB-A"
    assert b["page_target_id"] == "TAB-B"
    assert seen == {"sess-A": "TAB-A", "sess-B": "TAB-B"}
