from html.parser import HTMLParser
from pathlib import Path
import re


WEB_ROOT = Path(__file__).resolve().parents[1] / "web"


class WorkbenchStructureParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.detail_parent_classes = set()
        self.detail_attributes = {}
        self.classes = set()

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set(attributes.get("class", "").split())
        self.classes.update(classes)
        if attributes.get("id") == "detail":
            self.detail_parent_classes = set(self.stack[-1][1])
            self.detail_attributes = attributes
        if tag not in self.VOID_TAGS:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class NewTaskFormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.dialog_attributes = {}
        self.form_ancestors = {}
        self.controls = {"url-form": [], "job-form": []}
        self.options = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id") == "new-task-dialog":
            self.dialog_attributes = attributes

        form = next(
            (node_attrs.get("id") for node_tag, node_attrs in reversed(self.stack) if node_tag == "form"),
            None,
        )
        if tag == "form" and attributes.get("id") in self.controls:
            form = attributes["id"]
            self.form_ancestors[form] = [node_attrs.get("id") for _, node_attrs in self.stack]
        if form in self.controls and tag in {"input", "select", "textarea"}:
            self.controls[form].append({"tag": tag, **attributes})
        if tag == "option" and form in self.controls:
            select = next(
                (node_attrs for node_tag, node_attrs in reversed(self.stack) if node_tag == "select"),
                None,
            )
            if select and select.get("id"):
                self.options.setdefault(select["id"], []).append(attributes.get("value"))

        if tag not in WorkbenchStructureParser.VOID_TAGS:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


class DetailTabsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.ids = []
        self.tablists = []
        self.tabs = []
        self.panels = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        inside_detail = any(node_attrs.get("id") == "detail" for _, node_attrs in self.stack)
        if inside_detail:
            role = attributes.get("role")
            if role == "tablist":
                self.tablists.append(attributes)
            elif role == "tab":
                self.tabs.append(attributes)
            elif role == "tabpanel":
                self.panels.append(attributes)
        if tag not in WorkbenchStructureParser.VOID_TAGS:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_workbench_has_master_detail_structure_and_preserves_detail_contract():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    parser = WorkbenchStructureParser()
    parser.feed(page)

    assert "task-detail-scroll" in parser.classes
    assert "task-detail-scroll" in parser.detail_parent_classes
    assert parser.detail_attributes["id"] == "detail"
    assert parser.detail_attributes["aria-live"] == "polite"
    assert "jobs-list" in parser.classes


def test_user_copy_describes_optional_source_subtitles_without_legacy_modes():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    api = (WEB_ROOT / "app.py").read_text(encoding="utf-8")

    for stale in (
        "日语自动多源模式还需要上传剪映吗？",
        "剪映输入必须",
        "Two source SRT files are required",
        "CapCut and Whisper inputs must be .srt files",
        "删除视频但保留字幕",
    ):
        assert stale not in page + app_script + api
    assert "补充源语言字幕" in page
    assert "主字幕和辅助字幕" in api
    assert "清理源媒体" in app_script


def test_pure_frontend_formatters_are_loaded_before_the_main_controller():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    formatters = (WEB_ROOT / "static" / "workbench-formatters.js").read_text(
        encoding="utf-8"
    )

    assert page.index("workbench-formatters.js") < page.index("app.js?v=")
    assert "window.PhoebeFormatters" in formatters
    for name in ("escapeHtml", "formatDate", "getVideoId", "formatDuration", "formatBytes"):
        assert name in formatters
        assert f"function {name}" not in app_script
    assert "window.PhoebeFormatters" in app_script


def test_detail_has_five_aria_linked_tabs_with_overview_selected_by_default():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    parser = DetailTabsParser()
    parser.feed(page)

    expected = ["overview", "pipeline", "log", "files", "risk"]
    assert len(parser.tablists) == 1
    assert parser.tablists[0]["aria-label"] == "任务详情"
    assert [tab["data-detail-tab"] for tab in parser.tabs] == expected
    assert len(parser.panels) == 5

    panels = {panel["id"]: panel for panel in parser.panels}
    for index, (name, tab) in enumerate(zip(expected, parser.tabs)):
        panel = panels[tab["aria-controls"]]
        assert panel["aria-labelledby"] == tab["id"]
        assert panel["tabindex"] == "0"
        if index == 0:
            assert tab["aria-selected"] == "true"
            assert tab["tabindex"] == "0"
            assert "hidden" not in panel
        else:
            assert tab["aria-selected"] == "false"
            assert tab["tabindex"] == "-1"
            assert "hidden" in panel

    assert len(parser.ids) == len(set(parser.ids))
    for review_id in ("risk-summary", "risk-list", "risk-pagination"):
        assert parser.ids.count(review_id) == 1


def test_detail_tab_switching_keeps_aria_visibility_and_keyboard_state_in_sync():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    restore_start = app_script.index("function restoreDetailTabState")
    restore_end = app_script.index("async function changeActiveDetailTab", restore_start)
    restore = app_script[restore_start:restore_end]
    assert "setAttribute('aria-selected', String(selected))" in restore
    assert "tab.tabIndex = selected ? 0 : -1" in restore
    assert "panel.hidden = !selected" in restore

    bind_start = app_script.index("function bindDetailTabNavigation")
    bind_end = app_script.index("bindDetailTabNavigation();", bind_start)
    binding = app_script[bind_start:bind_end]
    assert "addEventListener('click'" in binding
    for key in ("ArrowLeft", "ArrowRight", "Home", "End"):
        assert key in binding
    assert "event.preventDefault()" in binding
    assert "{ focus: true }" in binding
    assert "$('#detail').innerHTML" not in app_script


def test_detail_tab_renderers_rebind_every_existing_dynamic_control():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    renderer_bindings = {
        "renderOverviewTab": "bindOverviewTabInteractions",
        "renderPipelineTab": "bindPipelineTabInteractions",
        "renderLogTab": "bindLogTabInteractions",
        "renderFilesTab": "bindFilesTabInteractions",
        "renderDetailRiskTab": "bindDetailRiskTabInteractions",
    }
    for renderer, binder in renderer_bindings.items():
        start = app_script.index(f"function {renderer}")
        match = re.search(r"\nfunction\s+\w+", app_script[start + 1:])
        end = start + 1 + match.start() if match else len(app_script)
        body = app_script[start:end]
        assert body.index("panel.innerHTML") < body.index(f"{binder}(panel)")

    for token in (
        'class="pause-job"', 'class="cancel-job"', 'class="danger delete-job"',
        'class="danger force-reset-job"', 'class="resume-job"', 'class="retry-job"',
        'class="restart-job"', 'class="run-button start-translation"',
        'class="open-capcut-app"', 'class="capcut-form"', 'class="optional-capcut-toggle"',
        'class="edit-translation-config"', 'class="run-button continue-automatic-source"',
        'class="open-youtube-recovery"', 'class="youtube-recovery-form"',
        'class="rename-job"', 'class="archive-job"', 'class="open-downloads"',
        'class="organize-technical"', 'class="final-download', 'class="risk-actions"',
        'data-action="resolved"', 'data-action="verified"', 'data-use-round2="1"',
        'data-generate-round2="1"',
    ):
        assert token in app_script

    overview_binding = app_script[
        app_script.index("function bindOverviewTabInteractions"):
        app_script.index("function bindPipelineTabInteractions")
    ]
    for handler in (
        "uploadCapcut", "openCapcutApp", "startTranslation", "cancelJob", "pauseJob",
            "resumeJob", "deleteJob", "retryJob", "retryDownloadWithAuth", "forceResetJob",
            "renameSelectedJob", "archiveSelectedJob", "openYoutubeRecovery", "syncRetryYoutubeAuth",
            "pasteYoutubeCookies", "activateView('review')", "continueWithAutomaticSource",
            ".optional-capcut-toggle", ".edit-translation-config",
    ):
        assert handler in overview_binding

    files_binding = app_script[
        app_script.index("function bindFilesTabInteractions"):
        app_script.index("function renderOverviewTab")
    ]
    assert "openDownloadsFolder" in files_binding
    assert "organizeSelectedJob" in files_binding

    risk_binding = app_script[
        app_script.index("function bindRiskCards"):
        app_script.index("function renderRiskPage")
    ]
    for handler in ("setActiveRisk", "loadRisks", "saveRiskCard", "dataset.generateRound2"):
        assert handler in risk_binding

    overview_renderer = app_script[
        app_script.index("function renderOverviewTab"):
        app_script.index("function renderPipelineTab")
    ]
    for content in ("context.workflow", "context.statusHint", "context.action", "context.jobActions", "context.primaryDownload"):
        assert content in overview_renderer
    pipeline_renderer = app_script[
        app_script.index("function renderPipelineTab"):
        app_script.index("function renderLogTab")
    ]
    for content in ("context.liveStatus", "context.configLine", "context.metricsView", "context.deliveryView"):
        assert content in pipeline_renderer
    files_renderer = app_script[
        app_script.index("function renderFilesTab"):
        app_script.index("function detailRiskItems")
    ]
    assert "context.deliveryArtifacts" in files_renderer
    assert "context.technicalArtifacts" in files_renderer
    detail_risk_binding = app_script[
        app_script.index("function bindDetailRiskTabInteractions"):
        app_script.index("function renderDetailRiskTab")
    ]
    assert "bindRiskCards(list)" in detail_risk_binding
    assert "saveRiskCard" in detail_risk_binding


def test_log_tab_has_its_own_bounded_contained_scroll_region():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'id="detail-panel-log"' in page
    assert 'class="logs log-scroll"' in app_script
    rule = re.search(r"\.log-scroll\s*\{(?P<body>[^}]*)\}", styles, re.DOTALL)
    assert rule
    body = rule.group("body")
    assert "max-height:" in body
    assert "overflow-y: auto" in body
    assert "overscroll-behavior: contain" in body
    assert "logs.scrollTop = logs.scrollHeight" in app_script


def test_active_detail_tab_resets_only_for_a_new_job_and_restores_after_redraws():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert app_script.count("activeDetailTab = 'overview';") == 2
    select_start = app_script.index("async function selectJob")
    select_end = app_script.index("/* ---- 详情面板渲染 ---- */", select_start)
    select_body = app_script[select_start:select_end]
    guard = select_body.index("if (previousJobId !== job.id)")
    reset = select_body.index("activeDetailTab = 'overview';")
    assert guard < reset
    guarded_reset = select_body[guard:select_body.index("const existingIndex", guard)]
    assert "allRiskItems = []" in guarded_reset
    assert "riskItems = []" in guarded_reset
    assert "risksLoadedJobId = ''" in guarded_reset

    render_start = app_script.index("function renderDetail()")
    render_end = app_script.index("function metricNumber", render_start)
    render_body = app_script[render_start:render_end]
    for renderer in ("renderOverviewTab", "renderPipelineTab", "renderLogTab", "renderFilesTab", "renderDetailRiskTab"):
        assert renderer in render_body
    assert "restoreDetailTabState();" in render_body
    assert "activeDetailTab = 'overview'" not in render_body
    assert "risksLoadedJobId !== selectedJob.id" in app_script
    assert "renderRiskCardMarkup(item, 'detail-risk')" in app_script
    assert "renderRiskCardMarkup(item, 'risk')" in app_script
    assert "const editorId = `${idPrefix}-editor-${riskId}`" in app_script
    assert "const titleId = `${idPrefix}-title-${riskId}`" in app_script

    load_start = app_script.index("async function loadRisks")
    catch_start = app_script.index("} catch (error) {", load_start)
    catch_end = app_script.index("} finally {", catch_start)
    catch_body = app_script[catch_start:catch_end]
    assert "allRiskItems = []" in catch_body
    assert "riskItems = []" in catch_body
    assert "risksLoadedJobId = ''" in catch_body
    assert "risksLoadedJobId = requestedJobId" not in catch_body


def test_detail_artifact_inventory_deduplicates_the_link_actually_shown_in_overview():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    context_start = app_script.index("function buildDetailContext")
    context_end = app_script.index("function bindOverviewTabInteractions", context_start)
    context = app_script[context_start:context_end]

    assert "const overviewLinkedArtifact = deliveryBlocked ? automaticArtifact : preferredArtifact" in context
    assert "name !== overviewLinkedArtifact" in context
    assert "overviewLinkedArtifact," in context


def test_workbench_exposes_compact_and_empty_state_hooks():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'class="page-header workbench-header"' in page
    assert 'class="flow-overview empty-state-only"' in page
    assert "classList.toggle('has-jobs', jobs.length > 0)" in app_script
    assert "classList.toggle('empty-state', jobs.length === 0)" in app_script
    assert "classList.toggle('workbench-active', view === 'workbench')" in app_script
    assert 'id="new-task-dialog"' in page
    assert page.count("data-open-new-task") >= 2
    assert ".header-actions" in styles
    assert "body.workbench-active.has-jobs .empty-state-only" in styles
    assert "body.workbench-active.has-jobs .jobs-list" in styles


def test_new_task_dialog_preserves_every_form_field_contract():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    parser = NewTaskFormParser()
    parser.feed(page)

    assert parser.dialog_attributes == {
        "id": "new-task-dialog",
        "class": "new-task-dialog",
        "aria-labelledby": "new-job-title",
        "aria-modal": "true",
    }
    assert "new-task-dialog" in parser.form_ancestors["url-form"]
    assert "new-task-dialog" in parser.form_ancestors["job-form"]

    by_id = {
        form: {control["id"]: control for control in controls if control.get("id")}
        for form, controls in parser.controls.items()
    }
    url = by_id["url-form"]
    assert (url["source-language"]["tag"], url["source-language"]["name"]) == ("select", "source_language")
    assert parser.options["source-language"] == ["en", "ja", "ko"]
    assert (url["target-language"]["tag"], url["target-language"]["name"]) == ("select", "target_language")
    assert parser.options["target-language"] == ["zh-CN"]
    assert (url["video-url"]["name"], url["video-url"]["type"], "required" in url["video-url"]) == ("url", "url", True)
    assert (url["url-base-url"]["name"], url["url-base-url"]["type"]) == ("base_url", "url")
    assert (url["url-api-key"]["name"], url["url-api-key"]["type"]) == ("api_key", "password")
    assert (url["url-model"]["name"], url["url-model"].get("type", "text")) == ("model", "text")
    assert url["url-model"].get("value", "") == ""
    assert "list" not in url["url-model"]
    assert "placeholder" not in url["url-model"]
    assert parser.options["reference-strategy"] == ["adaptive", "youtube", "whisper"]
    assert parser.options["video-quality"] == ["best", "2160", "1440", "1080", "720", "480"]
    assert (url["download-proxy"]["name"], url["download-proxy"].get("type", "text")) == ("proxy", "text")
    assert url["download-proxy"].get("value", "") == "http://127.0.0.1:7890"
    assert url["url-round2-model"]["name"] == "round2_model"
    assert (url["url-batch-size"]["name"], url["url-batch-size"]["type"]) == ("batch_size", "number")
    assert url["url-local-whisper"]["name"] == "local_whisper"
    assert parser.options["youtube-auth"] == ["none", "chrome", "file"]
    assert (url["youtube-cookies-text"]["tag"], url["youtube-cookies-text"]["name"]) == ("textarea", "youtube_cookies_text")

    url_named = {(control.get("name"), control.get("type"), control.get("value")): control for control in parser.controls["url-form"]}
    assert not [control for control in parser.controls["url-form"] if control.get("name") == "workflow_mode"]
    assert "checked" in url_named[("download_video", "checkbox", None)]
    assert url_named[("download_subtitles", "hidden", "true")]["tag"] == "input"
    assert "checked" in url_named[("download_thumbnail", "checkbox", None)]
    assert any(control.get("type") == "checkbox" and "disabled" in control and "checked" in control for control in parser.controls["url-form"])

    upload = by_id["job-form"]
    assert (upload["direct-source-language"]["tag"], upload["direct-source-language"]["name"]) == ("select", "source_language")
    assert parser.options["direct-source-language"] == ["en", "ja", "ko"]
    assert (upload["direct-target-language"]["tag"], upload["direct-target-language"]["name"]) == ("select", "target_language")
    assert parser.options["direct-target-language"] == ["zh-CN"]
    assert (upload["capcut-upload"]["name"], upload["capcut-upload"]["type"], upload["capcut-upload"]["accept"], "required" in upload["capcut-upload"]) == ("primary_srt", "file", ".srt", True)
    assert (upload["whisper-upload"]["name"], upload["whisper-upload"]["type"], upload["whisper-upload"]["accept"], "required" in upload["whisper-upload"]) == ("secondary_srt", "file", ".srt", True)
    assert (upload["video-upload"]["name"], upload["video-upload"]["type"], upload["video-upload"]["accept"], "required" in upload["video-upload"]) == ("video", "file", "video/*,audio/*", False)


def test_new_task_dialog_mode_close_help_and_success_hooks():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert 'name="new_task_mode" value="url"' in page
    assert 'name="new_task_mode" value="upload"' in page
    assert 'data-new-task-panel="url"' in page
    assert 'data-new-task-panel="upload" hidden' in page
    assert "newTaskDialog.showModal()" in app_script
    assert "panel.hidden = panel.dataset.newTaskPanel !== nextMode" in app_script
    assert "event.target === newTaskDialog" in app_script
    assert "newTaskOpener.focus()" in app_script
    assert "openNewTaskDialog('url', document.activeElement)" in app_script
    assert ".new-task-mode-panel[hidden]" in styles
    assert "width: min(720px, 90vw)" in styles

    for endpoint in ("/api/jobs/from-url", "/api/jobs"):
        candidates = [
            app_script.find(f"const job = await api('{endpoint}'"),
            app_script.find(f'const job = await api("{endpoint}"'),
        ]
        start = next(index for index in candidates if index >= 0)
        success = app_script[start:app_script.index("} catch (error)", start)]
        assert success.index("closeNewTaskDialog();") < success.index("await loadJobs();") < success.index("await selectJob(job.id);")


def test_batch_selection_has_checkboxes_select_all_bar_and_hooks():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    # Per-job checkbox rendered in JS card template
    assert "job-check-input" in app_script
    assert "data-check-id" in app_script
    assert "selectedJobIds.has(job.id)" in app_script
    # Select-all + batch bar in static HTML
    assert 'id="job-select-all"' in page
    assert 'id="batch-bar"' in page
    assert 'id="batch-archive"' in page
    assert 'id="batch-delete"' in page
    assert 'id="batch-clear"' in page
    # Batch toolbar is persistent; buttons disabled when no selection; checkbox click must not select detail
    assert "archiveBtn.disabled = selected === 0" in app_script
    assert "deleteBtn.disabled = selected === 0" in app_script
    assert "clearBtn.disabled = selected === 0" in app_script
    assert "stopPropagation" in app_script
    assert "selectedJobIds.delete(id)" in app_script
    # Escape clears selection
    assert "event.key !== 'Escape'" in app_script
    # Styling present (visual refinement: compact embedded checkbox + design-system toolbar)
    assert ".job-wrap" in styles
    assert ".batch-toolbar" in styles
    assert "width: 18px" in styles
    assert "height: 18px" in styles
    assert ".job.selected" in styles


def test_realtime_updates_preserve_scroll_tab_and_selection():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    # Log auto-follow: only scrolls to bottom when user hasn't scrolled up
    assert "logAutoFollow" in app_script
    assert "if (logAutoFollow) logsEl.scrollTop = logsEl.scrollHeight" in app_script
    assert "trackLogScroll" in app_script
    assert "scrollTop + scrollEl.clientHeight >= scrollEl.scrollHeight - 40" in app_script
    # Jobs-list scroll preserved across refresh/redraw
    assert "previousScrollTop = jobsListEl?.scrollTop || 0" in app_script
    assert "applyJobsListHtml(listHtml, previousScrollTop)" in app_script
    assert "list.scrollTop = scrollTop || 0" in app_script
    # Tab state preserved (UX-C) and selection preserved (UX-D)
    assert "restoreDetailTabState" in app_script
    assert "selectedJobIds.has(job.id)" in app_script


def test_jobs_list_rebases_deferred_redraw_and_preserves_the_live_card_node():
    """延迟刷新不能回放旧 HTML，也不能替换正在选中/悬停的卡片节点。"""
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    # 交互判定覆盖鼠标按下、输入焦点、以及被重绘区域上的 hover
    assert "if (pointerIsDown) return true;" in app_script
    assert "['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)" in app_script
    assert "document.querySelector('#jobs-list :hover')" in app_script
    # 延迟队列只记录刷新意图；空闲时必须基于最新 allJobs / selectedJob 重建。
    assert "pendingJobsListRender = true;" in app_script
    assert "writeJobsListHtml(renderCurrentJobsListHtml(), pendingJobsListScrollTop);" in app_script
    assert "pendingJobsListHtml" not in app_script
    assert "const JOBS_LIST_APPLY_PROBE_MS = 300;" in app_script
    assert "}, JOBS_LIST_APPLY_PROBE_MS);" in app_script
    assert "scheduleJobsListApply();" in app_script
    # 实时更新复用现有 wrapper/button，避免 selected 边框在每次拉取时重新入场。
    replace_start = app_script.index("function replaceJobCard(job)")
    replace_end = app_script.index("async function refreshJobCard", replace_start)
    replace_body = app_script[replace_start:replace_end]
    assert "patchJobWrap(existing, fragment);" in replace_body
    assert "existing.replaceWith(fragment);" not in replace_body
    assert "if (isUserInteractingWithPage())" in replace_body
    # 顺序未变化时不得把每个现有节点重新 append 一遍。
    assert "const atIndex = list.children[index];" in app_script
    assert "if (atIndex !== node) list.insertBefore(node, atIndex || null);" in app_script
    assert "list.append(currentNode);" not in app_script
    # 鼠标状态用捕获阶段监听，避免被列表内部的 stopPropagation 吞掉
    assert "document.addEventListener('pointerdown', () => { pointerIsDown = true; }, true);" in app_script


def test_job_selection_and_hover_never_animate_geometry():
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    assert "transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease;" not in styles
    assert "/* Stable job-card geometry during live refresh and selection. */" in styles
    stable_start = styles.index("/* Stable job-card geometry during live refresh and selection. */")
    stable_rule = styles[stable_start:stable_start + 420]
    assert "transform: none;" in stable_rule
    assert "box-shadow: none;" in stable_rule
    assert "transition: background-color .12s ease, border-color .12s ease;" in stable_rule


def test_narrow_workbench_hides_floating_pet_outside_the_content_layer():
    styles = (WEB_ROOT / "static" / "app.css").read_text(encoding="utf-8")

    marker = "/* Narrow workbench: floating decoration must not cover task controls. */"
    assert marker in styles
    compact_rule = styles[styles.index(marker):]
    compact_rule = compact_rule[:compact_rule.index("}", compact_rule.index(".web-pet-summon")) + 1]
    assert "@media (max-width: 920px)" in compact_rule
    assert "body.workbench-active .web-pet" in compact_rule
    assert "body.workbench-active .web-pet-summon" in compact_rule
    assert "display: none !important;" in compact_rule


def test_job_summary_chips_expose_operational_backlog_filters():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="job-status-summary"' in page
    assert 'data-job-summary-filter="active"' in page
    assert 'data-job-summary-filter="review"' in page
    assert 'data-job-summary-filter="failed"' in page
    assert 'data-job-summary-filter="today"' in page
    assert "function jobSummaryCounts(jobs)" in app_script
    assert "unresolvedReviewRequiredCount(job) > 0" in app_script
    assert "renderJobSummary(jobs);" in app_script
    assert "summaryFilter === 'review'" in app_script
    assert "summaryFilter === 'today'" in app_script


def test_sse_outage_falls_back_to_adaptive_polling():
    """SSE 连不上时任务列表不能永远停在旧状态。"""
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "const JOBS_POLL_ACTIVE_MS = 2000;" in app_script
    assert "const JOBS_POLL_IDLE_MS = 8000;" in app_script
    assert "const JOBS_POLL_EMPTY_MS = 15000;" in app_script
    assert "const JOBS_POLL_FAILURE_THRESHOLD = 3;" in app_script
    # 连续失败才降级；一旦连上就复位并停掉轮询
    assert "jobsPollFailures >= JOBS_POLL_FAILURE_THRESHOLD" in app_script
    assert "es.onopen = () => noteStreamSuccess();" in app_script
    assert "noteStreamFailure();" in app_script
    # 标签页不可见时暂停，回来后补一次刷新再继续
    assert "document.addEventListener('visibilitychange'" in app_script
    assert "if (jobsPollTimer || document.hidden) return;" in app_script
    # 兜底轮询失败时不能每两秒弹一次 toast
    assert "await loadJobs({ silent: true });" in app_script
    assert "if (!silent) showToast(" in app_script


def test_global_job_stream_refreshes_only_the_changed_card():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    server_script = (WEB_ROOT / "app.py").read_text(encoding="utf-8")

    assert '@app.get("/api/job-events/stream")' in server_script
    assert "job_change_snapshot()" in server_script
    assert '"job_id": job_id' in server_script
    assert "new EventSource('/api/job-events/stream')" in app_script
    assert "async function refreshJobCard(jobId, generation)" in app_script
    assert "function replaceJobCard(job)" in app_script
    assert "patchJobWrap(existing, fragment);" in app_script
    assert "existing.replaceWith(fragment);" not in app_script
    assert "if (Number(job.generation || 0) < Number(generation || 0)) return;" in app_script


def test_broken_thumbnail_falls_back_to_the_text_placeholder():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    # error 事件不冒泡，必须用捕获阶段委托
    assert "$('#jobs-list')?.addEventListener('error'" in app_script
    assert "image.classList.contains('job-thumbnail')" in app_script
    assert "placeholder.className = 'job-thumbnail job-thumbnail-placeholder';" in app_script
    assert "image.replaceWith(placeholder);" in app_script


def test_asr_garbage_risk_reasons_have_chinese_display_labels():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "credit_line: '疑似字幕署名或来源标记'" in app_script
    assert "noise_command: '疑似非语音噪声指令'" in app_script
    assert "repeated_clause: '疑似 ASR 整句复读'" in app_script
    assert "low_info_density: '长时段语音信息密度过低'" in app_script


def test_batch_actions_send_json_content_type():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    # Both batch endpoints must send application/json (FastAPI dict body requirement).
    archive_call = app_script[app_script.index("'/api/jobs/batch-archive'"):]
    archive_call = archive_call[:archive_call.index("});") + 3]
    delete_call = app_script[app_script.index("'/api/jobs/batch-delete'"):]
    delete_call = delete_call[:delete_call.index("});") + 3]
    assert "Content-Type" in archive_call and "application/json" in archive_call
    assert "Content-Type" in delete_call and "application/json" in delete_call


def test_batch_toolbar_exposes_safe_retry_for_selected_jobs():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'id="batch-retry"' in page
    assert "function batchRetryEligibility(ids)" in app_script
    assert "'/api/jobs/batch-retry'" in app_script
    assert "body: JSON.stringify({ ids, restart: false })" in app_script


def test_health_diagnostics_surface_only_actionable_anomalies():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "function healthDiagnosticSummary(health)" in app_script
    assert "health.stuck_jobs" in app_script
    assert "health.recent_failures" in app_script
    assert "health.disk?.percent" in app_script
    assert "诊断：${diagnostics.join(' · ')}" in app_script


def test_translation_ready_state_is_summary_first_without_repeated_credentials():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")
    start = app_script.index("function renderTranslationSettings")
    end = app_script.index("function renderTranslationConfigRepair", start)
    form = app_script[start:end]
    assert "translation-model-summary" in form
    assert "当前模型" in form
    assert 'name="api_key"' not in form
    assert 'name="base_url"' not in form
    assert 'name="proxy"' not in form


def test_ui_has_no_workflow_mode_or_model_recommendations():
    page = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert 'name="workflow_mode"' not in page
    assert "workflow_mode" not in app_script
    assert "settings-model-suggestions" not in page
    assert "translation-model-suggestions" not in app_script
    assert "deepseek-v4-flash…" not in app_script
    assert "const MEMORY_KEYS = ['model'" not in app_script
    assert "memory.model" not in app_script


def test_ready_translation_uses_optional_capcut_toggle_in_same_region():
    app_script = (WEB_ROOT / "static" / "app.js").read_text(encoding="utf-8")

    assert "function renderOptionalCapcutSource(job)" in app_script
    assert "补充剪映源语言字幕（可选）" in app_script
    assert 'class="optional-capcut-toggle"' in app_script
    assert "openCapcutApp" in app_script
    assert "uploadCapcut" in app_script
    waiting = app_script[app_script.index("if (job.status === 'waiting_capcut')"):]
    waiting = waiting[:waiting.index("} else if (job.status === 'ready_for_translation')")]
    assert "renderTranslationSettings(job)" in waiting
    assert "continue-automatic-source" in waiting
