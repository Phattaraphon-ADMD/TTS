import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import threading
from io import BytesIO
from pathlib import Path

from gtts import gTTS
from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from server import VOICES, generate_audio

WINDOW_TITLE = "Discord TTS Microphone"
MAX_MESSAGE_LENGTH = 2000
SPEED_SCALE = 10
MIN_SPEED = 0.5
MAX_SPEED = 2.0
MIN_SPEED_TICKS = int(MIN_SPEED * SPEED_SCALE)
MAX_SPEED_TICKS = int(MAX_SPEED * SPEED_SCALE)
WORKER_SHUTDOWN_TIMEOUT_MS = 3000
VOICE_LABELS = {
    "th-TH-NiwatNeural": "Thai - Male",
    "th-TH-PremwadeeNeural": "Thai - Female",
    "en-US-GuyNeural": "English - Male",
    "en-US-AriaNeural": "English - Female",
}
EDGE_PROVIDER = "edge"
WINDOWS_PROVIDER = "windows"
GTTS_PROVIDER = "gtts"
WINDOWS_VOICE_LIST_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "Add-Type -AssemblyName System.Speech; "
    "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
    "try { $voices = @($synth.GetInstalledVoices() | "
    "Where-Object { $_.Enabled } | ForEach-Object { "
    "[PSCustomObject]@{ name = $_.VoiceInfo.Name; "
    "culture = $_.VoiceInfo.Culture.Name } }); "
    "ConvertTo-Json -InputObject $voices -Compress } "
    "finally { $synth.Dispose() }"
)
WINDOWS_SPEECH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$payloadJson = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String([Console]::In.ReadToEnd())
)
$payload = $payloadJson | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $synth.SelectVoice([string]$payload.voice)
    $synth.SetOutputToWaveFile([string]$payload.path)
    $synth.Speak([string]$payload.text)
}
finally {
    $synth.Dispose()
}
"""


def get_windows_voices():
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_VOICE_LIST_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    voices = json.loads(completed.stdout.strip() or "[]")
    return [voices] if isinstance(voices, dict) else voices


class MessageInput(QPlainTextEdit):
    speak_requested = Signal()

    def keyPressEvent(self, event):
        enter_keys = (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        shift_pressed = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if event.key() in enter_keys and not shift_pressed:
            event.accept()
            self.speak_requested.emit()
            return
        super().keyPressEvent(event)


class TTSWorker(QThread):
    audio_ready = Signal(bytes)
    failed = Signal(str)

    def __init__(self, text, voice, provider):
        super().__init__()
        self.text = text
        self.voice = voice
        self.provider = provider
        self._loop = None
        self._task = None
        self._process = None
        self._cancel_requested = False
        self._state_lock = threading.Lock()

    def cancel(self):
        with self._state_lock:
            self._cancel_requested = True
            loop = self._loop
            task = self._task
            process = self._process
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)
        if process is not None and process.poll() is None:
            process.terminate()

    def run(self):
        try:
            if self.provider == WINDOWS_PROVIDER:
                audio_data = self._generate_windows_audio()
            elif self.provider == GTTS_PROVIDER:
                audio_data = self._generate_gtts_audio()
            else:
                audio_data = self._generate_edge_audio()
            if self._is_cancelled():
                return
            if not audio_data:
                self.failed.emit("TTS ไม่ส่งข้อมูลเสียงกลับมา")
            else:
                self.audio_ready.emit(audio_data)
        except Exception as error:
            if not self._is_cancelled():
                self.failed.emit(str(error))

    def _is_cancelled(self):
        with self._state_lock:
            return self._cancel_requested

    def _generate_edge_audio(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task = loop.create_task(generate_audio(self.text, self.voice))
        with self._state_lock:
            self._loop = loop
            self._task = task
            cancel_requested = self._cancel_requested
        if cancel_requested:
            loop.call_soon(task.cancel)
        try:
            return loop.run_until_complete(task)
        finally:
            loop.close()
            asyncio.set_event_loop(None)
            with self._state_lock:
                self._loop = None
                self._task = None

    def _generate_gtts_audio(self):
        audio_buffer = BytesIO()
        speech = gTTS(
            self.text,
            lang=self.voice,
            lang_check=False,
            timeout=(5, 30),
        )
        speech.write_to_fp(audio_buffer)
        return audio_buffer.getvalue()

    def _generate_windows_audio(self):
        temp_file_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                temp_file_path = Path(temp_file.name)
            payload = base64.b64encode(
                json.dumps(
                    {"text": self.text, "voice": self.voice, "path": str(temp_file_path)},
                    ensure_ascii=True,
                ).encode("utf-8")
            ).decode("ascii")
            process = subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_SPEECH_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="ascii",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._state_lock:
                self._process = process
                cancel_requested = self._cancel_requested
            if cancel_requested:
                process.terminate()
            try:
                _, error_output = process.communicate(input=payload, timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise TimeoutError("Windows Speech ใช้เวลาสร้างเสียงนานเกินไป")
            finally:
                with self._state_lock:
                    self._process = None
            if process.returncode != 0:
                raise RuntimeError(error_output.strip() or "Windows Speech สร้างเสียงไม่สำเร็จ")
            return temp_file_path.read_bytes()
        finally:
            if temp_file_path is not None:
                temp_file_path.unlink(missing_ok=True)


class TTSWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(560, 620)

        self.media_devices = QMediaDevices(self)
        self.windows_voice_error = None
        try:
            self.windows_voices = get_windows_voices()
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            self.windows_voices = []
            self.windows_voice_error = str(error)

        self.audio_outputs = [QAudioOutput(self), QAudioOutput(self)]
        self.players = [QMediaPlayer(self), QMediaPlayer(self)]
        self.audio_paths = [None, None]
        for index, player in enumerate(self.players):
            player.setAudioOutput(self.audio_outputs[index])
            player.errorOccurred.connect(
                lambda error, message, player_index=index:
                self._on_player_error(player_index, error, message)
            )
            player.mediaStatusChanged.connect(
                lambda status, player_index=index:
                self._on_media_status_changed(player_index, status)
            )

        self.worker = None
        self.request_id = 0
        self.output_devices = []
        self.output_combos = []
        self.selected_output_devices = [None, None]
        self.active_player_indexes = set()
        self.ended_player_indexes = set()

        self._build_ui()
        self.media_devices.audioOutputsChanged.connect(self._refresh_outputs)
        self._refresh_outputs()
        if not self.windows_voices:
            if self.windows_voice_error:
                self._set_status("โหลดเสียง Windows ไม่สำเร็จ; ใช้ Edge TTS ได้ตามปกติ")
            else:
                self._set_status("ไม่พบเสียง Windows Speech; ใช้ Edge TTS ได้ตามปกติ")

    def _build_ui(self):
        content = QWidget(self)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(12)

        heading = QLabel(WINDOW_TITLE)
        heading.setStyleSheet("font-size: 22px; font-weight: 700;")
        layout.addWidget(heading)
        layout.addWidget(QLabel("ส่งเสียง TTS ไปยังอุปกรณ์ที่เลือก"))

        for index, label in enumerate(("Output Device 1", "Output Device 2 (optional)")):
            layout.addWidget(QLabel(label))
            combo = QComboBox()
            combo.currentIndexChanged.connect(
                lambda item_index, output_index=index:
                self._select_output(output_index, item_index)
            )
            self.output_combos.append(combo)
            layout.addWidget(combo)

        layout.addWidget(QLabel("Voice Source"))
        self.voice_source_combo = QComboBox()
        self.voice_source_combo.addItem("Edge TTS (ออนไลน์)", EDGE_PROVIDER)
        self.voice_source_combo.addItem("Google TTS ภาษาไทย (ออนไลน์)", GTTS_PROVIDER)
        if self.windows_voices:
            self.voice_source_combo.addItem("Windows Speech (ออฟไลน์)", WINDOWS_PROVIDER)
        self.voice_source_combo.currentIndexChanged.connect(self._refresh_voice_choices)
        layout.addWidget(self.voice_source_combo)

        layout.addWidget(QLabel("Voice"))
        self.voice_combo = QComboBox()
        layout.addWidget(self.voice_combo)
        self._refresh_voice_choices()

        speed_row = QHBoxLayout()
        speed_row.addWidget(QLabel("Speed"))
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(MIN_SPEED_TICKS, MAX_SPEED_TICKS)
        self.speed_slider.setValue(SPEED_SCALE)
        self.speed_slider.valueChanged.connect(self._update_speed_label)
        speed_row.addWidget(self.speed_slider, 1)
        self.speed_label = QLabel("1.0x")
        speed_row.addWidget(self.speed_label)
        layout.addLayout(speed_row)

        layout.addWidget(QLabel("Message"))
        self.message_input = MessageInput()
        self.message_input.setPlaceholderText("พิมพ์ข้อความแล้วกด Enter เพื่อพูด")
        self.message_input.setMinimumHeight(150)
        self.message_input.speak_requested.connect(self.speak)
        layout.addWidget(self.message_input, 1)

        button_row = QHBoxLayout()
        self.speak_button = QPushButton("พูดทันที")
        self.speak_button.clicked.connect(self.speak)
        self.stop_button = QPushButton("หยุดพูด")
        self.stop_button.clicked.connect(self.stop)
        button_row.addWidget(self.speak_button)
        button_row.addWidget(self.stop_button)
        layout.addLayout(button_row)

        self.status_label = QLabel("พร้อมใช้งาน")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.setCentralWidget(content)

    def _refresh_voice_choices(self):
        provider = self.voice_source_combo.currentData()
        self.voice_combo.clear()
        if provider == WINDOWS_PROVIDER:
            for voice in self.windows_voices:
                label = f"{voice['name']} ({voice['culture']})"
                self.voice_combo.addItem(label, voice)
            return
        if provider == GTTS_PROVIDER:
            self.voice_combo.addItem("Thai (Google Translate TTS)", "th")
            return
        for voice in sorted(VOICES):
            self.voice_combo.addItem(VOICE_LABELS[voice], voice)

    def _refresh_outputs(self):
        selected_ids = [combo.currentData() for combo in self.output_combos]
        self.output_devices = self.media_devices.audioOutputs()
        default_id = self.media_devices.defaultAudioOutput().id()
        for output_index, combo in enumerate(self.output_combos):
            combo.blockSignals(True)
            combo.clear()
            if output_index == 1:
                combo.addItem("ไม่ใช้งาน", None)
            for device in self.output_devices:
                combo.addItem(device.description(), device.id())

            selected_id = selected_ids[output_index]
            available_ids = [device.id() for device in self.output_devices]
            if selected_id not in available_ids:
                selected_id = default_id if output_index == 0 else None
            combo.setCurrentIndex(combo.findData(selected_id))
            combo.blockSignals(False)

        for output_index, combo in enumerate(self.output_combos):
            self._select_output(output_index, combo.currentIndex())

    def _select_output(self, output_index, item_index):
        device_id = self.output_combos[output_index].itemData(item_index)
        device = next(
            (item for item in self.output_devices if item.id() == device_id),
            None,
        )
        if output_index == 1 and device is not None:
            primary = self.selected_output_devices[0]
            if primary is not None and primary.id() == device.id():
                combo = self.output_combos[1]
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
                device = None
                self._set_status("เลือก Output คนละอุปกรณ์กัน")
        if output_index == 0 and device is not None:
            secondary = self.selected_output_devices[1]
            if secondary is not None and secondary.id() == device.id():
                combo = self.output_combos[1]
                combo.blockSignals(True)
                combo.setCurrentIndex(0)
                combo.blockSignals(False)
                self.selected_output_devices[1] = None

        self.selected_output_devices[output_index] = device
        if device is not None:
            self.audio_outputs[output_index].setDevice(device)
            self._set_status(f"Output {output_index + 1}: {device.description()}")

    def _update_speed_label(self, value):
        speed = value / SPEED_SCALE
        self.speed_label.setText(f"{speed:.1f}x")
        for player in self.players:
            player.setPlaybackRate(speed)

    def speak(self):
        text = self.message_input.toPlainText().strip()
        if not text:
            self._set_status("กรุณาพิมพ์ข้อความก่อน")
            return
        if len(text) > MAX_MESSAGE_LENGTH:
            self._set_status(f"ข้อความยาวได้ไม่เกิน {MAX_MESSAGE_LENGTH} ตัวอักษร")
            return
        if self.selected_output_devices[0] is None:
            self._set_status("ไม่พบอุปกรณ์เสียง Output")
            return
        provider = self.voice_source_combo.currentData()
        voice = self.voice_combo.currentData()
        if provider == WINDOWS_PROVIDER:
            contains_thai = any("\u0e00" <= character <= "\u0e7f" for character in text)
            if contains_thai and not voice["culture"].lower().startswith("th"):
                self._set_status(
                    "เสียงออฟไลน์ที่เลือกเป็นภาษาอังกฤษและไม่รองรับข้อความไทย; "
                    "เลือก Edge TTS หรือเสียง Windows ภาษาไทย (th-TH)"
                )
                return
            voice = voice["name"]
        if self.worker is not None and self.worker.isRunning():
            return

        self.stop()
        self.request_id += 1
        request_id = self.request_id
        self.worker = TTSWorker(
            text,
            voice,
            provider,
        )
        audio_suffix = ".wav" if provider == WINDOWS_PROVIDER else ".mp3"
        self.worker.audio_ready.connect(
            lambda audio_data: self._play_audio(request_id, audio_data, audio_suffix)
        )
        self.worker.failed.connect(
            lambda message: self._on_generation_error(request_id, message)
        )
        worker = self.worker
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        self.speak_button.setEnabled(False)
        self._set_status("กำลังสร้างเสียง...")
        self.worker.start()

    def stop(self):
        self.request_id += 1
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
        for player in self.players:
            player.stop()
        self._clear_audio_files()
        self.active_player_indexes.clear()
        self.ended_player_indexes.clear()
        self.speak_button.setEnabled(
            self.worker is None or not self.worker.isRunning()
        )
        self._set_status("หยุดแล้ว")

    def _play_audio(self, request_id, audio_data, audio_suffix):
        if request_id != self.request_id:
            return
        self.active_player_indexes = {
            index
            for index, device in enumerate(self.selected_output_devices)
            if device is not None
        }
        self.ended_player_indexes.clear()
        for index in self.active_player_indexes:
            with tempfile.NamedTemporaryFile(suffix=audio_suffix, delete=False) as audio_file:
                audio_file.write(audio_data)
                self.audio_paths[index] = Path(audio_file.name)
            player = self.players[index]
            player.setSource(QUrl.fromLocalFile(str(self.audio_paths[index])))
            player.setPlaybackRate(self.speed_slider.value() / SPEED_SCALE)
            player.play()
        self._set_status("กำลังพูดออก " + str(len(self.active_player_indexes)) + " อุปกรณ์...")

    def _on_generation_error(self, request_id, message):
        if request_id == self.request_id:
            self._set_status(f"สร้างเสียงไม่สำเร็จ: {message}")

    def _on_worker_finished(self, worker):
        if self.worker is worker:
            self.worker = None
            self.speak_button.setEnabled(True)

    def _on_player_error(self, output_index, _error, error_message):
        self._set_status(f"เล่นเสียง Output {output_index + 1} ไม่สำเร็จ: {error_message}")

    def _on_media_status_changed(self, output_index, media_status):
        if media_status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.ended_player_indexes.add(output_index)
            if self.ended_player_indexes >= self.active_player_indexes:
                self._set_status("พูดเสร็จแล้ว")
                self._clear_audio_files()

    def _clear_audio_files(self):
        for index, audio_path in enumerate(self.audio_paths):
            if audio_path is not None:
                self.players[index].setSource(QUrl())
                audio_path.unlink(missing_ok=True)
                self.audio_paths[index] = None

    def _set_status(self, text):
        self.status_label.setText(text)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            if not self.worker.wait(WORKER_SHUTDOWN_TIMEOUT_MS):
                self._set_status("กำลังหยุดงาน TTS กรุณาปิดหน้าต่างอีกครั้ง")
                event.ignore()
                return
        for player in self.players:
            player.stop()
        self._clear_audio_files()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(WINDOW_TITLE)
    window = TTSWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())