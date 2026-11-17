"""Step 2: Capture — "Render in DAW" and "Use plugin directly"."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..backends.daw import (
    DawRoundtripBackend,
    SessionIndex,
    find_session,
    markdown_to_text,
    render_instructions,
)
from ..core.analysis import AnalysisOptions
from ..core.audio_io import READABLE_EXTENSIONS
from ..core.session import Session, create_session, load_session
from .runner import Job, file_job
from .widgets import run_in_background


class DropArea(QFrame):
    files_dropped = Signal(list)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(70)
        self.setMaximumHeight(110)
        lab = QLabel(text)
        lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lab.setWordWrap(True)
        QVBoxLayout(self).addWidget(lab)
        self.setStyleSheet("DropArea { border: 2px dashed palette(mid); border-radius: 8px; }")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        paths = [p for p in paths if p.suffix.lower() in READABLE_EXTENSIONS]
        if paths:
            self.files_dropped.emit(paths)


class InstructionsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.tabs = QTabWidget()
        self.browsers: dict[str, QTextBrowser] = {}
        self.md: dict[str, str] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tabs)
        copy = QPushButton("Copy instructions")
        copy.clicked.connect(self._copy)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(copy)
        lay.addLayout(row)

    def show_for(self, session: Session) -> None:
        self.md = render_instructions(session)
        self.tabs.clear()
        for daw, md in self.md.items():
            b = QTextBrowser()
            b.setOpenExternalLinks(True)
            b.setMarkdown(md)
            self.tabs.addTab(b, daw)
            self.browsers[daw] = b

    def _copy(self) -> None:
        daw = self.tabs.tabText(self.tabs.currentIndex())
        if daw in self.md:
            QGuiApplication.clipboard().setText(markdown_to_text(self.md[daw]))


class DawTab(QWidget):
    analyze_requested = Signal(list)  # list[Job]
    status = Signal(str)

    def __init__(self, page: CapturePage):
        super().__init__()
        self.page = page
        self.index = SessionIndex()
        self.backend = DawRoundtripBackend(self.index)
        self.session: Session | None = None

        create = QPushButton("Create test file…")
        create.setDefault(True)
        create.clicked.connect(self.create_test_file)
        self.info = QLabel("Create a test file, render it through the plugin in your DAW, then import the result.")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.open_folder = QPushButton("Open folder")
        self.open_folder.setEnabled(False)
        self.open_folder.clicked.connect(self._open_folder)
        self.instructions = InstructionsPanel()
        placeholder = QLabel(
            "<h3>How the DAW route works</h3><ol>"
            "<li>Choose the settings on the Setup page.</li>"
            "<li>Click <b>Create test file…</b>. Step-by-step instructions for Cubase and Logic Pro appear here.</li>"
            "<li>Render the test file through the plugin in your DAW.</li>"
            "<li>Import the rendered file(s) below. irene-capture checks them, shows a summary and creates the IR."
            "</li></ol>")
        placeholder.setWordWrap(True)
        placeholder.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.middle = QStackedWidget()
        self.middle.addWidget(placeholder)
        self.middle.addWidget(self.instructions)

        imp = QPushButton("Import rendered file(s)…")
        imp.clicked.connect(self.import_dialog)
        self.drop = DropArea("…or drop rendered WAV/AIFF files here (several files of the same session are "
                             "analyzed as a batch)")
        self.drop.files_dropped.connect(self.import_files)

        top = QHBoxLayout()
        top.addWidget(create)
        top.addWidget(self.open_folder)
        top.addStretch()
        bottom = QHBoxLayout()
        bottom.addWidget(imp)
        bottom.addWidget(self.drop, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.info)
        lay.addWidget(self.middle, 1)
        lay.addLayout(bottom)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e):
        self.drop.dragEnterEvent(e)

    def dropEvent(self, e):
        self.drop.dropEvent(e)

    def create_test_file(self) -> None:
        setup = self.page.setup
        if setup.has_errors():
            QMessageBox.warning(self, "Settings", "Please fix the settings marked in red on the Setup page first.")
            return
        session = create_session(setup.settings())
        folder = setup.output_folder()
        folder.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(self, "Create test file", str(folder / session.test_file),
                                              "WAV files (*.wav)")
        if not path:
            return
        try:
            wav, js, txt = self.backend.create_test_file(session, path)
        except OSError as e:
            QMessageBox.critical(self, "Can't write the test file", str(e))
            return
        self.session = session
        self.page.qs.setValue("paths/last_test_dir", str(wav.parent))
        self.info.setText(
            f"<b>Test file:</b> {wav.name} ({session.layout.total_length / session.sample_rate:.1f} s, "
            f"{session.sample_rate / 1000:g} kHz, {'mono' if session.layout.channels == 1 else 'stereo'})<br>"
            f"<b>Keep the session file <code>{js.name}</code> next to the test file</b> — irene-capture needs it "
            f"to analyze the render. The instructions are also saved as <code>{txt.name}</code>.")
        self.instructions.show_for(session)
        self.middle.setCurrentIndex(1)
        self.open_folder.setEnabled(True)
        self.status.emit(f"Created {wav}")

    def _open_folder(self) -> None:
        if self.session and self.session.path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.session.path.parent)))

    def import_dialog(self) -> None:
        start = self.page.qs.value("paths/last_import", "") or str(self.page.setup.output_folder())
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import rendered file(s)", start,
            "Audio files (" + " ".join("*" + e for e in READABLE_EXTENSIONS) + ")")
        if files:
            self.import_files([Path(f) for f in files])

    def import_files(self, paths: list[Path]) -> None:
        if not paths:
            return
        self.page.qs.setValue("paths/last_import", str(paths[0].parent))
        last_test = self.page.qs.value("paths/last_test_dir", "")
        extra = (Path(last_test),) if last_test else ()
        jobs: list[Job] = []
        chosen: Session | None = None
        for p in paths:
            session, how = find_session(p, self.index, extra)
            if session is None:
                if chosen is None:
                    js, _ = QFileDialog.getOpenFileName(self, f"Session for {p.name}", str(p.parent),
                                                        "irene-capture sessions (*.capture.json)")
                    if not js:
                        continue
                    try:
                        chosen = load_session(js)
                    except ValueError as e:
                        QMessageBox.critical(self, "Session file", str(e))
                        continue
                session, how = chosen, f"Session chosen by you ({Path(js).name})."
            self.status.emit(f"{p.name}: {how}")
            jobs.append(file_job(p, session, how))
        if jobs:
            self.analyze_requested.emit(jobs)


class ParameterDialog(QDialog):
    """Generic controls (sliders from the plugin's parameters), the fallback when no editor can be shown."""

    def __init__(self, host, params: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Plugin parameters")
        self.host = host
        self.resize(520, 600)
        inner = QWidget()
        form = QFormLayout(inner)
        for p in params:
            s = QSlider(Qt.Orientation.Horizontal)
            s.setRange(0, 1000)
            s.setValue(int(round(p["raw_value"] * 1000)))
            label = QLabel(f"{p['text']} {p['label']}".strip())
            label.setMinimumWidth(90)
            row = QHBoxLayout()
            row.addWidget(s, 1)
            row.addWidget(label)
            s.valueChanged.connect(lambda v, key=p["key"], lab=label: self._set(key, v / 1000, lab))
            form.addRow(p["name"], row)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(scroll)
        lay.addWidget(buttons)

    def _set(self, key, raw, label) -> None:
        try:
            label.setText(self.host.set_parameter(key, raw))
        except Exception as e:  # keep the dialog usable
            label.setText(f"error: {e}")


class PluginTab(QWidget):
    analyze_requested = Signal(list)
    status = Signal(str)

    def __init__(self, page: CapturePage):
        super().__init__()
        self.page = page
        self.host = None
        self.editor_open = False
        self.pending_capture = False

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter plugins…")
        self.filter.textChanged.connect(self._apply_filter)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _: self.load_selected())
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.rescan)
        add = QPushButton("Add folder…")
        add.clicked.connect(self.add_folder)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse)
        load = QPushButton("Load")
        load.clicked.connect(self.load_selected)
        left = QVBoxLayout()
        left.addWidget(self.filter)
        left.addWidget(self.list, 1)
        brow = QHBoxLayout()
        for b in (rescan, add, browse, load):
            brow.addWidget(b)
        left.addLayout(brow)
        lw = QWidget()
        lw.setLayout(left)

        self.loaded = QLabel("No plugin loaded.")
        self.loaded.setWordWrap(True)
        self.editor_btn = QPushButton("Open editor")
        self.editor_btn.clicked.connect(self.toggle_editor)
        self.params_btn = QPushButton("Generic controls…")
        self.params_btn.clicked.connect(self.generic_controls)
        self.name = QLineEdit()
        self.name.setPlaceholderText("e.g. 4x12 SM57 edge")
        self.save_rendered = QCheckBox("Also save the rendered audio")
        self.capture_btn = QPushButton("Capture")
        self.capture_btn.setDefault(True)
        self.capture_btn.clicked.connect(self.capture)
        for b in (self.editor_btn, self.params_btn, self.capture_btn):
            b.setEnabled(False)
        hint = QLabel(
            "1. Choose a plugin and load it.  2. Open its editor and dial in the sound.  3. Name the capture and "
            "press Capture (the editor closes).  Change the sound and capture again as often as you like.<br>"
            "The plugin runs in a separate process, so a crashing plugin can't take the app down.")
        hint.setWordWrap(True)
        right = QVBoxLayout()
        right.addWidget(self.loaded)
        er = QHBoxLayout()
        er.addWidget(self.editor_btn)
        er.addWidget(self.params_btn)
        right.addLayout(er)
        f = QFormLayout()
        f.addRow("Capture name:", self.name)
        right.addLayout(f)
        right.addWidget(self.save_rendered)
        right.addWidget(self.capture_btn)
        right.addWidget(hint)
        right.addStretch()
        rw = QWidget()
        rw.setLayout(right)

        split = QSplitter()
        split.addWidget(lw)
        split.addWidget(rw)
        split.setSizes([380, 420])
        QVBoxLayout(self).addWidget(split)
        self._scanned = False

    def showEvent(self, e):
        super().showEvent(e)
        if not self._scanned:
            self._scanned = True
            self.rescan()

    # --- plugin list ---------------------------------------------------------------------
    def folders(self) -> list[Path]:
        return [Path(p) for p in (self.page.qs.value("paths/plugin_folders", []) or []) if p]

    def rescan(self) -> None:
        from ..backends.plugin_host import scan_plugins, standard_folders

        self.list.clear()
        plugins = scan_plugins(self.folders())
        for p in plugins:
            it = QListWidgetItem(f"{p.stem}  ({'AU' if p.suffix == '.component' else 'VST3'})")
            it.setData(Qt.ItemDataRole.UserRole, str(p))
            it.setToolTip(str(p))
            self.list.addItem(it)
        if not plugins:
            it = QListWidgetItem("No plugins found in " + ", ".join(str(f) for f in standard_folders())
                                 + ". Use Add folder… or Browse….")
            it.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(it)
        self._apply_filter(self.filter.text())

    def _apply_filter(self, text: str) -> None:
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(text) and text.lower() not in it.text().lower())

    def add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Add plugin folder")
        if d:
            folders = [str(f) for f in self.folders()]
            if d not in folders:
                folders.append(d)
            self.page.qs.setValue("paths/plugin_folders", folders)
            self.rescan()

    def browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a plugin", "",
                                              "Plugins (*.vst3 *.component);;All files (*)")
        if path:
            self.load(path)

    def load_selected(self) -> None:
        it = self.list.currentItem()
        if it and it.data(Qt.ItemDataRole.UserRole):
            self.load(it.data(Qt.ItemDataRole.UserRole))

    # --- host ------------------------------------------------------------------------------
    def ensure_host(self):
        from ..backends.plugin_host import PluginHostBackend

        if self.host is None:
            self.host = PluginHostBackend()
        return self.host

    def load(self, path: str) -> None:
        host = self.ensure_host()
        self.loaded.setText(f"Loading {Path(path).name}…")
        self._busy(True)

        def work():
            names = []
            try:
                names = host.plugin_names(path)
            except Exception:
                names = []
            return names

        def got_names(names):
            name = None
            if len(names) > 1:
                name, ok = QInputDialog.getItem(self, "Choose a plugin", f"{Path(path).name} contains several "
                                                "plugins:", names, 0, False)
                if not ok:
                    self._busy(False)
                    self.loaded.setText("No plugin loaded.")
                    return
            run_in_background(host.load, path, name, on_done=self._loaded, on_error=self._failed)

        run_in_background(work, on_done=got_names, on_error=self._failed)

    def _loaded(self, info: dict) -> None:
        self._busy(False)
        lat = f", latency {info['latency']} samples" if info.get("latency") else ""
        self.loaded.setText(f"<b>{info['name']}</b> — {info.get('manufacturer') or info['format']}{lat}<br>"
                            f"<small>{info['path']}</small>")
        self.editor_btn.setEnabled(bool(info.get("has_editor")))
        self.params_btn.setEnabled(True)
        self.capture_btn.setEnabled(True)
        if not self.name.text():
            self.name.setText(self.page.setup.capture_type().value)

    def _failed(self, exc, tb) -> None:
        self._busy(False)
        self.editor_open = False
        self.editor_btn.setText("Open editor")
        self.loaded.setText(f"<span style='color:#d0342c'>{exc}</span>")
        self.status.emit(str(exc))

    def _busy(self, on: bool) -> None:
        for b in (self.capture_btn, self.params_btn):
            b.setEnabled(not on and bool(self.host and self.host.info))
        if not self.editor_open:
            self.editor_btn.setEnabled(not on and bool(self.host and self.host.info.get("has_editor")))

    def toggle_editor(self) -> None:
        if self.editor_open:
            self.host.close_editor()
            return
        self.editor_open = True
        self.editor_btn.setText("Close editor")
        self.status.emit("Plugin editor open — dial in the sound, then press Capture or close the editor.")

        def closed(_):
            self.editor_open = False
            self.editor_btn.setText("Open editor")
            self.status.emit("Plugin editor closed.")
            if self.pending_capture:
                self.pending_capture = False
                self.capture()

        def failed(exc, tb):
            self.editor_open = False
            self.editor_btn.setText("Open editor")
            QMessageBox.warning(self, "Plugin editor",
                                f"The plugin editor could not be shown:\n{exc}\n\nUse Generic controls… instead.")

        run_in_background(self.host.show_editor, on_done=closed, on_error=failed)

    def generic_controls(self) -> None:
        try:
            params = self.host.parameters()
        except Exception as e:
            QMessageBox.warning(self, "Parameters", str(e))
            return
        if not params:
            QMessageBox.information(self, "Parameters", "This plugin exposes no parameters.")
            return
        ParameterDialog(self.host, params, self).exec()

    def capture(self) -> None:
        setup = self.page.setup
        if setup.has_errors():
            QMessageBox.warning(self, "Settings", "Please fix the settings marked in red on the Setup page first.")
            return
        if self.editor_open:
            # Close the editor first; the capture starts when it is closed. Some plugins ignore the
            # request to close (seen on macOS), then the user closes the window.
            self.pending_capture = True
            self.host.close_editor()
            self.status.emit("Closing the plugin editor… If its window stays open, close it to start the capture.")
            return
        settings = setup.settings()
        settings.capture_name = self.name.text().strip()
        session = create_session(settings, source="plugin")
        host = self.host
        save_rendered = self.save_rendered.isChecked()

        def produce(opts: AnalysisOptions):
            result = host.capture_and_analyze(session, opts)
            result.attachments = host.state_attachments()
            if save_rendered:
                result.rendered_to_save = host.last_rendered
            return result

        label = f"{host.info.get('name', 'plugin')} — {settings.capture_name or 'capture'}"
        self.analyze_requested.emit([Job(label, session, produce)])


class CapturePage(QWidget):
    analyze_requested = Signal(list)
    status = Signal(str)

    def __init__(self, setup, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setup = setup
        self.qs = settings
        self.tabs = QTabWidget()
        self.daw = DawTab(self)
        self.plugin = PluginTab(self)
        self.tabs.addTab(self.daw, "Render in DAW")
        self.tabs.addTab(self.plugin, "Use plugin directly")
        for t in (self.daw, self.plugin):
            t.analyze_requested.connect(self.analyze_requested)
            t.status.connect(self.status)
        QVBoxLayout(self).addWidget(self.tabs)

    def shutdown(self) -> None:
        if self.plugin.host is not None:
            self.plugin.host.stop()
