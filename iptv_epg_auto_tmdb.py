import sys
import re
import gzip
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QTableWidget, QTableWidgetItem, QPushButton, QLineEdit,
    QLabel, QFileDialog, QMessageBox, QHeaderView, QSplitter, QTextEdit,
    QProgressBar, QInputDialog, QCheckBox
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QPixmap


def clean_movie_title(raw_title):
    year_match = re.search(r'\b(19\d{2}|20\d{2})\b', raw_title)
    year = year_match.group(1) if year_match else ""
    clean = re.sub(
        r'\[.*?\]|\(.*?\)|\b(FHD|UHD|HD|SD|4K|1080p|720p|ITA|ENG|SUB|MULTI|H264|H265|HEVC|WEB-DL|BLURAY|DVDRIP)\b',
        '', raw_title, flags=re.IGNORECASE
    )
    clean = re.sub(r'^(FILM|VOD|CINEMA)\s*[-:|]\s*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\s+', ' ', clean).strip(" -_.")
    return clean, year


class TMDBBatchWorker(QThread):
    row_updated = pyqtSignal(int, dict)
    progress_updated = pyqtSignal(int, int, str)
    finished_batch = pyqtSignal(int, int)

    def __init__(self, api_key, tasks):
        super().__init__()
        self.api_key = api_key
        self.tasks = tasks
        self._is_running = True

    def stop(self):
        self._is_running = False

    def run(self):
        updated = 0
        not_found = 0
        total = len(self.tasks)

        for idx, (row, raw_title) in enumerate(self.tasks, start=1):
            if not self._is_running:
                break

            clean_title, year = clean_movie_title(raw_title)
            self.progress_updated.emit(idx, total, f"Ricerca ({idx}/{total}): {clean_title}...")

            if not clean_title:
                not_found += 1
                continue

            try:
                movie = self._query_tmdb(clean_title, year)
                if not movie and year:
                    movie = self._query_tmdb(clean_title, "")

                if movie:
                    self.row_updated.emit(row, movie)
                    updated += 1
                else:
                    not_found += 1

                time.sleep(0.26)
            except Exception:
                not_found += 1

        self.finished_batch.emit(updated, not_found)

    def _query_tmdb(self, query, year):
        url = "https://api.themoviedb.org/3/search/movie"
        params = {
            "api_key": self.api_key,
            "query": query,
            "language": "it-IT",
            "include_adult": "false"
        }
        if year:
            params["year"] = year

        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 429:
            time.sleep(3)
            resp = requests.get(url, params=params, timeout=10)

        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None


class TMDBSingleSearchWorker(QThread):
    results_ready = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, api_key, query, year=""):
        super().__init__()
        self.api_key = api_key
        self.query = query
        self.year = year

    def run(self):
        try:
            url = "https://api.themoviedb.org/3/search/movie"
            params = {
                "api_key": self.api_key,
                "query": self.query,
                "language": "it-IT",
                "include_adult": "false"
            }
            if self.year.isdigit() and len(self.year) == 4:
                params["year"] = self.year

            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            self.results_ready.emit(resp.json().get("results", []))
        except Exception as e:
            self.error_occurred.emit(str(e))


class IPTVEpgAutoTMDB(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("IPTV M3U + EPG XMLTV + Auto-TMDB Studio")
        self.resize(1380, 840)

        self.channels = []
        self.epg_channels = {}
        self.current_tmdb_results = []
        self.tmdb_target_row = None
        self.batch_worker = None

        self.init_ui()

    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)

        top_bar = QHBoxLayout()
        top_bar.addWidget(QLabel("<b>TMDB API Key (v3):</b>"))
        self.tmdb_key_input = QLineEdit()
        self.tmdb_key_input.setPlaceholderText("Inserisci la tua API Key TMDB v3...")
        self.tmdb_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        top_bar.addWidget(self.tmdb_key_input)

        self.chk_auto_epg = QCheckBox("Genera anche evento EPG durante la scansione automatica")
        self.chk_auto_epg.setChecked(True)
        top_bar.addWidget(self.chk_auto_epg)

        self.chk_overwrite_logo = QCheckBox("Sovrascrivi loghi esistenti")
        self.chk_overwrite_logo.setChecked(True)
        top_bar.addWidget(self.chk_overwrite_logo)

        main_layout.addLayout(top_bar)

        progress_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)

        self.lbl_status = QLabel("Pronto.")
        self.btn_stop_batch = QPushButton("Ferma")
        self.btn_stop_batch.setVisible(False)
        self.btn_stop_batch.clicked.connect(self.stop_batch_process)

        progress_layout.addWidget(self.lbl_status)
        progress_layout.addWidget(self.progress_bar)
        progress_layout.addWidget(self.btn_stop_batch)
        main_layout.addLayout(progress_layout)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)

        self.tab_m3u = QWidget()
        self.tab_epg = QWidget()
        self.tab_tmdb = QWidget()

        self.tabs.addTab(self.tab_m3u, "Playlist IPTV (M3U)")
        self.tabs.addTab(self.tab_epg, "Editor Guida EPG (XMLTV)")
        self.tabs.addTab(self.tab_tmdb, "Ricerca Manuale Film TMDB")

        self.setup_m3u_tab()
        self.setup_epg_tab()
        self.setup_tmdb_tab()

    def setup_m3u_tab(self):
        layout = QVBoxLayout(self.tab_m3u)

        file_bar = QHBoxLayout()
        btn_load_file = QPushButton("Carica M3U da File")
        btn_load_file.clicked.connect(self.load_m3u_file)

        btn_load_url = QPushButton("Carica M3U da URL")
        btn_load_url.clicked.connect(self.load_m3u_url)

        btn_save_m3u = QPushButton("Esporta M3U")
        btn_save_m3u.clicked.connect(self.save_m3u_file)

        btn_add_row = QPushButton("Aggiungi Voce")
        btn_add_row.clicked.connect(self.add_m3u_row)

        btn_del_row = QPushButton("Elimina Selezionati")
        btn_del_row.clicked.connect(self.delete_m3u_rows)

        file_bar.addWidget(btn_load_file)
        file_bar.addWidget(btn_load_url)
        file_bar.addWidget(btn_save_m3u)
        file_bar.addWidget(btn_add_row)
        file_bar.addWidget(btn_del_row)
        layout.addLayout(file_bar)

        auto_bar = QHBoxLayout()

        btn_auto_selected = QPushButton("Auto-TMDB Selezionati")
        btn_auto_selected.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 6px;")
        btn_auto_selected.clicked.connect(lambda: self.start_batch_tmdb(only_selected=True))

        btn_auto_visible = QPushButton("Auto-TMDB Tutti i Visibili (Filtrati)")
        btn_auto_visible.setStyleSheet("background-color: #01b4e4; color: white; font-weight: bold; padding: 6px;")
        btn_auto_visible.clicked.connect(lambda: self.start_batch_tmdb(only_selected=False))

        btn_manual_tmdb = QPushButton("Cerca Manualmente Selezionato")
        btn_manual_tmdb.clicked.connect(self.send_selected_to_tmdb)

        self.m3u_filter_input = QLineEdit()
        self.m3u_filter_input.setPlaceholderText("Filtra per titolo o gruppo (es. 'Film', 'Cinema', 'Action')...")
        self.m3u_filter_input.textChanged.connect(self.filter_m3u_table)

        auto_bar.addWidget(btn_auto_selected)
        auto_bar.addWidget(btn_auto_visible)
        auto_bar.addWidget(btn_manual_tmdb)
        auto_bar.addWidget(self.m3u_filter_input)
        layout.addLayout(auto_bar)

        self.m3u_table = QTableWidget(0, 5)
        self.m3u_table.setHorizontalHeaderLabels([
            "Titolo / Nome Canale", "Gruppo (group-title)", "TVG-ID (EPG)", "Locandina / Logo (tvg-logo)", "Stream URL"
        ])
        self.m3u_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.m3u_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.m3u_table)

    def load_m3u_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Apri Playlist M3U", "", "Playlist (*.m3u *.m3u8);;Tutti i file (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            self._parse_m3u_content(content)
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Impossibile leggere il file:\n{e}")

    def load_m3u_url(self):
        url, ok = QInputDialog.getText(self, "Carica M3U da URL", "Inserisci l'URL completo della playlist M3U:")
        if not ok or not url.strip():
            return
        try:
            self.lbl_status.setText("Scaricamento playlist remota in corso...")
            QApplication.processEvents()
            resp = requests.get(url.strip(), timeout=20)
            resp.raise_for_status()
            self._parse_m3u_content(resp.text)
            self.lbl_status.setText("Playlist remota caricata.")
        except Exception as e:
            self.lbl_status.setText("Errore caricamento URL.")
            QMessageBox.critical(self, "Errore URL", f"Impossibile scaricare la playlist:\n{e}")

    def _parse_m3u_content(self, content):
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        self.channels.clear()
        current_info = {}

        for line in lines:
            if line.startswith("#EXTINF:"):
                tvg_id = self._extract_attr(line, "tvg-id")
                tvg_logo = self._extract_attr(line, "tvg-logo")
                group = self._extract_attr(line, "group-title")
                name = line.split(",")[-1].strip() if "," in line else "Senza Nome"
                current_info = {
                    "name": name,
                    "group": group,
                    "tvg_id": tvg_id,
                    "logo": tvg_logo,
                    "url": ""
                }
            elif not line.startswith("#") and current_info:
                current_info["url"] = line
                self.channels.append(current_info)
                current_info = {}

        self.populate_m3u_table()
        QMessageBox.information(self, "Playlist Caricata", f"Caricate {len(self.channels)} voci.")

    def _extract_attr(self, line, attr):
        match = re.search(rf'{attr}="([^"]*)"', line, re.IGNORECASE)
        return match.group(1) if match else ""

    def populate_m3u_table(self):
        self.m3u_table.setRowCount(0)
        for row_idx, ch in enumerate(self.channels):
            self.m3u_table.insertRow(row_idx)
            self.m3u_table.setItem(row_idx, 0, QTableWidgetItem(ch["name"]))
            self.m3u_table.setItem(row_idx, 1, QTableWidgetItem(ch["group"]))
            self.m3u_table.setItem(row_idx, 2, QTableWidgetItem(ch["tvg_id"]))
            self.m3u_table.setItem(row_idx, 3, QTableWidgetItem(ch["logo"]))
            self.m3u_table.setItem(row_idx, 4, QTableWidgetItem(ch["url"]))

    def save_m3u_file(self):
        path, _ = QFileDialog.getSaveFileName(self, "Esporta Playlist M3U", "playlist_tmdb.m3u", "M3U (*.m3u)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
                for r in range(self.m3u_table.rowCount()):
                    name = self.m3u_table.item(r, 0).text() if self.m3u_table.item(r, 0) else ""
                    group = self.m3u_table.item(r, 1).text() if self.m3u_table.item(r, 1) else ""
                    tvg_id = self.m3u_table.item(r, 2).text() if self.m3u_table.item(r, 2) else ""
                    logo = self.m3u_table.item(r, 3).text() if self.m3u_table.item(r, 3) else ""
                    url = self.m3u_table.item(r, 4).text() if self.m3u_table.item(r, 4) else ""

                    f.write(f'#EXTINF:-1 tvg-id="{tvg_id}" tvg-logo="{logo}" group-title="{group}",{name}\n')
                    f.write(f'{url}\n')
            QMessageBox.information(self, "Esportato", "Playlist M3U salvata con successo.")
        except Exception as e:
            QMessageBox.critical(self, "Errore", f"Errore durante il salvataggio:\n{e}")

    def add_m3u_row(self):
        row = self.m3u_table.rowCount()
        self.m3u_table.insertRow(row)
        for col in range(5):
            self.m3u_table.setItem(row, col, QTableWidgetItem(""))

    def delete_m3u_rows(self):
        rows = sorted(set(index.row() for index in self.m3u_table.selectedIndexes()), reverse=True)
        for r in rows:
            self.m3u_table.removeRow(r)

    def filter_m3u_table(self, text):
        text = text.lower()
        for r in range(self.m3u_table.rowCount()):
            name = self.m3u_table.item(r, 0).text().lower() if self.m3u_table.item(r, 0) else ""
            group = self.m3u_table.item(r, 1).text().lower() if self.m3u_table.item(r, 1) else ""
            self.m3u_table.setRowHidden(r, text not in name and text not in group)

    def start_batch_tmdb(self, only_selected=True):
        api_key = self.tmdb_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "API Key Mancante", "Inserisci prima la tua chiave API TMDB nella barra in alto.")
            return

        if self.batch_worker and self.batch_worker.isRunning():
            QMessageBox.warning(self, "Operazione in corso", "Attendi il completamento della scansione attuale.")
            return

        tasks = []
        if only_selected:
            selected_rows = sorted(set(idx.row() for idx in self.m3u_table.selectedIndexes()))
            for r in selected_rows:
                title = self.m3u_table.item(r, 0).text() if self.m3u_table.item(r, 0) else ""
                tasks.append((r, title))
        else:
            for r in range(self.m3u_table.rowCount()):
                if not self.m3u_table.isRowHidden(r):
                    title = self.m3u_table.item(r, 0).text() if self.m3u_table.item(r, 0) else ""
                    tasks.append((r, title))

        if not tasks:
            QMessageBox.information(self, "Nessun elemento", "Nessuna riga selezionata o visibile da elaborare.")
            return

        reply = QMessageBox.question(
            self, "Conferma Auto-TMDB",
            f"Avviare la ricerca automatica su TMDB per {len(tasks)} elementi?\n"
            f"Verranno aggiornati titoli, locandine e (se attivo) gli eventi EPG.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        self.progress_bar.setMaximum(len(tasks))
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.btn_stop_batch.setVisible(True)

        self.batch_worker = TMDBBatchWorker(api_key, tasks)
        self.batch_worker.row_updated.connect(self.on_batch_row_updated)
        self.batch_worker.progress_updated.connect(self.on_batch_progress)
        self.batch_worker.finished_batch.connect(self.on_batch_finished)
        self.batch_worker.start()

    def stop_batch_process(self):
        if self.batch_worker and self.batch_worker.isRunning():
            self.batch_worker.stop()
            self.lbl_status.setText("Arresto in corso...")

    def on_batch_progress(self, current, total, text):
        self.progress_bar.setValue(current)
        self.lbl_status.setText(text)

    def on_batch_row_updated(self, row, movie):
        title = movie.get("title", "")
        year = (movie.get("release_date") or "")[:4]
        formatted_title = f"{title} ({year})" if year else title
        poster_path = movie.get("poster_path")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""

        self.m3u_table.setItem(row, 0, QTableWidgetItem(formatted_title))

        current_logo = self.m3u_table.item(row, 3).text().strip() if self.m3u_table.item(row, 3) else ""
        if poster_url and (self.chk_overwrite_logo.isChecked() or not current_logo):
            self.m3u_table.setItem(row, 3, QTableWidgetItem(poster_url))

        if self.chk_auto_epg.isChecked():
            tvg_id = self.m3u_table.item(row, 2).text().strip() if self.m3u_table.item(row, 2) else ""
            if not tvg_id:
                tvg_id = re.sub(r'[^a-zA-Z0-9]', '', title.lower()) + ".vod"
                self.m3u_table.setItem(row, 2, QTableWidgetItem(tvg_id))

            overview = movie.get("overview") or "Nessuna trama disponibile."
            self._append_epg_entry(tvg_id, formatted_title, overview)

    def on_batch_finished(self, updated, not_found):
        self.progress_bar.setVisible(False)
        self.btn_stop_batch.setVisible(False)
        self.lbl_status.setText(f"Completato: {updated} aggiornati, {not_found} non trovati.")
        QMessageBox.information(
            self, "Scansione TMDB Completata",
            f"Elaborazione terminata!\n\nAggiornati: {updated}\nNon trovati: {not_found}"
        )

    def send_selected_to_tmdb(self):
        selected = self.m3u_table.selectedItems()
        if not selected:
            QMessageBox.warning(self, "Attenzione", "Seleziona prima una riga nella tabella M3U.")
            return
        row = selected[0].row()
        raw_title = self.m3u_table.item(row, 0).text()
        clean_title, year = clean_movie_title(raw_title)

        self.tmdb_search_input.setText(clean_title)
        self.tmdb_year_input.setText(year)
        self.tmdb_target_row = row
        self.tabs.setCurrentWidget(self.tab_tmdb)
        self.search_tmdb()

    def setup_epg_tab(self):
        layout = QVBoxLayout(self.tab_epg)

        btn_layout = QHBoxLayout()
        btn_load_xml = QPushButton("Carica XMLTV (.xml / .xml.gz)")
        btn_load_xml.clicked.connect(self.load_epg_xml)

        btn_save_xml = QPushButton("Esporta XMLTV (.xml)")
        btn_save_xml.clicked.connect(lambda: self.save_epg_xml(compressed=False))

        btn_save_gz = QPushButton("Esporta XMLTV Compresso (.xml.gz)")
        btn_save_gz.clicked.connect(lambda: self.save_epg_xml(compressed=True))

        btn_add_prog = QPushButton("Nuovo Programma EPG")
        btn_add_prog.clicked.connect(self.add_epg_programme)

        btn_del_prog = QPushButton("Elimina Programma")
        btn_del_prog.clicked.connect(self.delete_epg_programme)

        btn_layout.addWidget(btn_load_xml)
        btn_layout.addWidget(btn_save_xml)
        btn_layout.addWidget(btn_save_gz)
        btn_layout.addWidget(btn_add_prog)
        btn_layout.addWidget(btn_del_prog)
        layout.addLayout(btn_layout)

        self.epg_table = QTableWidget(0, 5)
        self.epg_table.setHorizontalHeaderLabels([
            "Channel ID (tvg-id)", "Inizio (YYYYMMDDHHMMSS +0100)", "Fine (YYYYMMDDHHMMSS +0100)", "Titolo Programma", "Descrizione / Trama"
        ])
        self.epg_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.epg_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.epg_table)

    def _append_epg_entry(self, tvg_id, title, desc):
        row = self.epg_table.rowCount()
        self.epg_table.insertRow(row)
        now = datetime.now()
        end = now + timedelta(hours=2)
        self.epg_table.setItem(row, 0, QTableWidgetItem(tvg_id))
        self.epg_table.setItem(row, 1, QTableWidgetItem(now.strftime("%Y%m%d%H%M00 +0100")))
        self.epg_table.setItem(row, 2, QTableWidgetItem(end.strftime("%Y%m%d%H%M00 +0100")))
        self.epg_table.setItem(row, 3, QTableWidgetItem(title))
        self.epg_table.setItem(row, 4, QTableWidgetItem(desc))

    def load_epg_xml(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Apri Guida EPG XMLTV", "", "XMLTV (*.xml *.xml.gz *.gz);;Tutti i file (*)"
        )
        if not path:
            return

        try:
            if path.endswith(".gz"):
                with gzip.open(path, "rb") as f:
                    tree = ET.parse(f)
            else:
                tree = ET.parse(path)

            root = tree.getroot()
            self.epg_channels.clear()
            for ch in root.findall("channel"):
                cid = ch.attrib.get("id", "")
                dname = ch.findtext("display-name", default=cid)
                self.epg_channels[cid] = dname

            self.epg_table.setRowCount(0)
            for idx, prog in enumerate(root.findall("programme")):
                self.epg_table.insertRow(idx)
                self.epg_table.setItem(idx, 0, QTableWidgetItem(prog.attrib.get("channel", "")))
                self.epg_table.setItem(idx, 1, QTableWidgetItem(prog.attrib.get("start", "")))
                self.epg_table.setItem(idx, 2, QTableWidgetItem(prog.attrib.get("stop", "")))
                self.epg_table.setItem(idx, 3, QTableWidgetItem(prog.findtext("title", default="")))
                self.epg_table.setItem(idx, 4, QTableWidgetItem(prog.findtext("desc", default="")))

            QMessageBox.information(self, "EPG Caricata", f"Caricati {self.epg_table.rowCount()} programmi EPG.")
        except Exception as e:
            QMessageBox.critical(self, "Errore XMLTV", f"Impossibile leggere il file EPG:\n{e}")

    def add_epg_programme(self):
        self._append_epg_entry("canale.it", "Nuovo Programma", "Descrizione programma")

    def delete_epg_programme(self):
        rows = sorted(set(index.row() for index in self.epg_table.selectedIndexes()), reverse=True)
        for r in rows:
            self.epg_table.removeRow(r)

    def save_epg_xml(self, compressed=False):
        default_name = "epg_tmdb.xml.gz" if compressed else "epg_tmdb.xml"
        file_filter = "GZIP XML (*.xml.gz)" if compressed else "XML (*.xml)"
        path, _ = QFileDialog.getSaveFileName(self, "Esporta XMLTV", default_name, file_filter)
        if not path:
            return

        try:
            tv = ET.Element("tv", {"generator-info-name": "IPTV-EPG-Auto-TMDB"})
            seen_channels = set()

            for r in range(self.epg_table.rowCount()):
                cid = self.epg_table.item(r, 0).text().strip()
                if cid and cid not in seen_channels:
                    seen_channels.add(cid)
                    ch_el = ET.SubElement(tv, "channel", {"id": cid})
                    dn = ET.SubElement(ch_el, "display-name", {"lang": "it"})
                    dn.text = self.epg_channels.get(cid, cid)

            for r in range(self.epg_table.rowCount()):
                cid = self.epg_table.item(r, 0).text().strip()
                start = self.epg_table.item(r, 1).text().strip()
                stop = self.epg_table.item(r, 2).text().strip()
                title = self.epg_table.item(r, 3).text().strip()
                desc = self.epg_table.item(r, 4).text().strip()

                prog_el = ET.SubElement(tv, "programme", {"start": start, "stop": stop, "channel": cid})
                t_el = ET.SubElement(prog_el, "title", {"lang": "it"})
                t_el.text = title
                if desc:
                    d_el = ET.SubElement(prog_el, "desc", {"lang": "it"})
                    d_el.text = desc

            tree = ET.ElementTree(tv)
            ET.indent(tree, space="  ", level=0)

            if compressed:
                xml_bytes = ET.tostring(tv, encoding="utf-8", xml_declaration=True)
                with gzip.open(path, "wb") as f:
                    f.write(xml_bytes)
            else:
                tree.write(path, encoding="utf-8", xml_declaration=True)

            QMessageBox.information(self, "Esportato", "Guida EPG esportata con successo.")
        except Exception as e:
            QMessageBox.critical(self, "Errore Salvataggio", f"Errore durante il salvataggio:\n{e}")

    def setup_tmdb_tab(self):
        layout = QVBoxLayout(self.tab_tmdb)

        search_layout = QHBoxLayout()
        self.tmdb_search_input = QLineEdit()
        self.tmdb_search_input.setPlaceholderText("Titolo del film da cercare...")
        self.tmdb_search_input.returnPressed.connect(self.search_tmdb)

        self.tmdb_year_input = QLineEdit()
        self.tmdb_year_input.setPlaceholderText("Anno")
        self.tmdb_year_input.setFixedWidth(100)

        btn_search = QPushButton("Cerca")
        btn_search.clicked.connect(self.search_tmdb)

        search_layout.addWidget(self.tmdb_search_input)
        search_layout.addWidget(self.tmdb_year_input)
        search_layout.addWidget(btn_search)
        layout.addLayout(search_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        self.tmdb_table = QTableWidget(0, 4)
        self.tmdb_table.setHorizontalHeaderLabels(["ID TMDB", "Titolo", "Data Uscita", "Voto"])
        self.tmdb_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tmdb_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tmdb_table.itemSelectionChanged.connect(self.display_tmdb_details)
        splitter.addWidget(self.tmdb_table)

        detail_widget = QWidget()
        detail_layout = QVBoxLayout(detail_widget)

        self.poster_label = QLabel("Nessuna locandina")
        self.poster_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.poster_label.setFixedHeight(290)
        self.poster_label.setStyleSheet("border: 1px solid #555; background: #1e1e1e; color: #aaa;")
        detail_layout.addWidget(self.poster_label)

        self.tmdb_overview = QTextEdit()
        self.tmdb_overview.setReadOnly(True)
        detail_layout.addWidget(self.tmdb_overview)

        btn_apply_m3u = QPushButton("Applica a riga M3U")
        btn_apply_m3u.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold; padding: 8px;")
        btn_apply_m3u.clicked.connect(self.apply_tmdb_to_m3u)
        detail_layout.addWidget(btn_apply_m3u)

        splitter.addWidget(detail_widget)
        splitter.setSizes([650, 550])
        layout.addWidget(splitter)

    def search_tmdb(self):
        api_key = self.tmdb_key_input.text().strip()
        if not api_key:
            QMessageBox.warning(self, "API Key Mancante", "Inserisci la tua chiave API TMDB.")
            return
        query = self.tmdb_search_input.text().strip()
        if not query:
            return
        self.single_worker = TMDBSingleSearchWorker(api_key, query, self.tmdb_year_input.text().strip())
        self.single_worker.results_ready.connect(self.on_tmdb_results)
        self.single_worker.error_occurred.connect(lambda err: QMessageBox.critical(self, "Errore TMDB", err))
        self.single_worker.start()

    def on_tmdb_results(self, results):
        self.current_tmdb_results = results
        self.tmdb_table.setRowCount(0)
        for idx, movie in enumerate(results):
            self.tmdb_table.insertRow(idx)
            self.tmdb_table.setItem(idx, 0, QTableWidgetItem(str(movie.get("id", ""))))
            self.tmdb_table.setItem(idx, 1, QTableWidgetItem(movie.get("title", "")))
            self.tmdb_table.setItem(idx, 2, QTableWidgetItem(movie.get("release_date", "")))
            self.tmdb_table.setItem(idx, 3, QTableWidgetItem(str(movie.get("vote_average", ""))))

    def display_tmdb_details(self):
        selected = self.tmdb_table.selectedItems()
        if not selected:
            return
        movie = self.current_tmdb_results[selected[0].row()]
        self.tmdb_overview.setText(movie.get("overview") or "Nessuna trama disponibile.")
        poster_path = movie.get("poster_path")
        if poster_path:
            try:
                img_data = requests.get(f"https://image.tmdb.org/t/p/w300{poster_path}", timeout=8).content
                pixmap = QPixmap()
                pixmap.loadFromData(img_data)
                self.poster_label.setPixmap(pixmap.scaled(200, 280, Qt.AspectRatioMode.KeepAspectRatio))
            except Exception:
                self.poster_label.setText("Errore caricamento locandina")
        else:
            self.poster_label.setText("Locandina non disponibile")

    def apply_tmdb_to_m3u(self):
        selected = self.tmdb_table.selectedItems()
        if not selected:
            return
        movie = self.current_tmdb_results[selected[0].row()]
        target = self.tmdb_target_row
        if target is None:
            m3u_sel = self.m3u_table.selectedItems()
            if not m3u_sel:
                return
            target = m3u_sel[0].row()

        self.on_batch_row_updated(target, movie)
        self.tabs.setCurrentWidget(self.tab_m3u)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = IPTVEpgAutoTMDB()
    window.show()
    sys.exit(app.exec())
