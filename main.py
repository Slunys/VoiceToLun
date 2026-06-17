import sounddevice as sd
import soundfile as sf
import numpy as np
from pynput import keyboard
from pynput.keyboard import Controller
from faster_whisper import WhisperModel
import os
import sys
import queue
import threading
import time
import customtkinter as ctk
import json
import pystray
from PIL import Image, ImageDraw
import pyperclip
import winreg

# --- Настройки и Конфиг ---
CONFIG_FILE = "config.json"
MODEL_SIZE = "medium"
SAMPLE_RATE = 16000
MODEL_DIR = os.path.join(os.getcwd(), "whisper_models")
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            return data.get("hotkey", "Key.f20"), data.get("autostart", False)
    return "Key.f20", False


def save_config(hotkey_str, autostart_bool):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"hotkey": hotkey_str, "autostart": autostart_bool}, f)


current_hotkey_str, current_autostart = load_config()


# --- Логика Автозапуска (Windows Registry) ---
def set_autostart(enable):
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    app_name = "VoiceTypingApp"
    # Получаем абсолютный путь к запущенному файлу (скрипту или .exe)
    exe_path = os.path.abspath(sys.argv[0])

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
        if enable:
            winreg.SetValueEx(key, app_name, 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, app_name)
            except FileNotFoundError:
                pass  # Если ключа и так нет, игнорируем
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[❌] Ошибка реестра: {e}")


# Применяем автозапуск при старте, если он был включен
if current_autostart:
    set_autostart(True)

# --- Инициализация ИИ ---
print("[⚙️] Загрузка модели...")
model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8", download_root=MODEL_DIR)
keyboard_controller = Controller()

is_recording = False
continuous_mode = False
is_rebinding = False
audio_queue = queue.Queue()
double_tap_timer = None
press_start_time = 0
press_duration = 0

# --- Интерфейс (UI) ---
ctk.set_appearance_mode("dark")
app = ctk.CTk()
app.title("VoiceTyping")
app.geometry("350x270")  # Чуть увеличили окно под тумблер
app.resizable(False, False)

status_label = ctk.CTkLabel(app, text="🟢 Готово к работе", font=("Arial", 20, "bold"), text_color="#2ecc71")
status_label.pack(pady=(20, 10))

info_label = ctk.CTkLabel(app, text="Удерживайте кнопку для диктовки,\nили нажмите дважды для постоянной записи",
                          font=("Arial", 12))
info_label.pack(pady=(0, 15))

current_key_label = ctk.CTkLabel(app, text=f"Кнопка: {current_hotkey_str.replace('Key.', '').upper()}",
                                 font=("Arial", 14))
current_key_label.pack(pady=5)


def update_status(text, color):
    app.after(0, lambda: status_label.configure(text=text, text_color=color))


def start_rebind():
    global is_rebinding
    is_rebinding = True
    update_status("🟡 Нажмите любую клавишу...", "#f1c40f")
    btn_rebind.configure(state="disabled")


btn_rebind = ctk.CTkButton(app, text="Изменить кнопку", command=start_rebind)
btn_rebind.pack(pady=10)


def toggle_autostart():
    global current_autostart
    current_autostart = autostart_var.get()
    save_config(current_hotkey_str, current_autostart)
    set_autostart(current_autostart)


autostart_var = ctk.BooleanVar(value=current_autostart)
switch_autostart = ctk.CTkSwitch(app, text="Автозапуск с Windows", variable=autostart_var, command=toggle_autostart)
switch_autostart.pack(pady=10)


# --- Логика Системного Трея ---
def hide_window():
    app.withdraw()


app.protocol('WM_DELETE_WINDOW', hide_window)


def show_window(icon, item):
    app.after(0, app.deiconify)


def quit_app(icon, item):
    icon.stop()
    os._exit(0)


def create_icon_image():
    image = Image.new('RGB', (64, 64), color=(30, 30, 30))
    dc = ImageDraw.Draw(image)
    dc.ellipse((16, 16, 48, 48), fill=(220, 50, 50))
    return image


def start_tray():
    menu = pystray.Menu(
        pystray.MenuItem('Показать окно', show_window, default=True),
        pystray.MenuItem('Выход', quit_app)
    )
    tray_icon = pystray.Icon("VoiceTyping", create_icon_image(), "Voice Typing", menu=menu)
    tray_icon.run()


# --- Логика Аудио и Вставка ---
def audio_callback(indata, frames, time, status):
    if is_recording:
        audio_queue.put(indata.copy())


def start_recording():
    global is_recording
    if not is_recording:
        update_status("🔴 Идет запись...", "#e74c3c")
        is_recording = True
        while not audio_queue.empty():
            audio_queue.get()


def process_and_type_logic(is_continuous_stop=False):
    global is_recording, continuous_mode, press_duration
    is_recording = False
    continuous_mode = False

    if not is_continuous_stop and press_duration < 0.4:
        update_status("🟢 Готово к работе", "#2ecc71")
        while not audio_queue.empty():
            audio_queue.get()
        return

    update_status("⏳ Обработка...", "#3498db")

    audio_data = []
    while not audio_queue.empty():
        audio_data.append(audio_queue.get())

    if not audio_data:
        update_status("🟢 Готово к работе", "#2ecc71")
        return

    audio_np = np.concatenate(audio_data, axis=0)
    temp_path = "temp_dictation.wav"
    sf.write(temp_path, audio_np, SAMPLE_RATE)

    try:
        segments, _ = model.transcribe(
            temp_path,
            beam_size=5,
            language="ru",
            vad_filter=True,
            initial_prompt="Обычный текст на русском языке, including some English words."
        )
        text = "".join([segment.text for segment in segments]).strip()
    except Exception as e:
        text = ""

    if os.path.exists(temp_path):
        os.remove(temp_path)

    if text:
        # МОМЕНТАЛЬНАЯ ВСТАВКА ЧЕРЕЗ БУФЕР ОБМЕНА
        old_clipboard = pyperclip.paste()  # Сохраняем старый буфер
        pyperclip.copy(text + " ")  # Кидаем новый текст

        # Эмулируем Ctrl+V
        keyboard_controller.press(keyboard.Key.ctrl)
        keyboard_controller.press('v')
        keyboard_controller.release('v')
        keyboard_controller.release(keyboard.Key.ctrl)

        time.sleep(0.6)

        pyperclip.copy(old_clipboard)  # Возвращаем старый буфер

    update_status("🟢 Готово к работе", "#2ecc71")


def process_and_type(is_continuous_stop=False):
    threading.Thread(target=process_and_type_logic, args=(is_continuous_stop,)).start()


# --- Перехват Клавиатуры ---
def on_press(key):
    global continuous_mode, double_tap_timer, press_start_time, is_recording, is_rebinding, current_hotkey_str
    key_str = str(key)

    if is_rebinding:
        current_hotkey_str = key_str
        save_config(current_hotkey_str, current_autostart)
        app.after(0, lambda: current_key_label.configure(text=f"Кнопка: {key_str.replace('Key.', '').upper()}"))
        update_status("🟢 Готово к работе", "#2ecc71")
        app.after(0, lambda: btn_rebind.configure(state="normal"))
        is_rebinding = False
        return

    if key_str == current_hotkey_str:
        if continuous_mode:
            process_and_type(is_continuous_stop=True)
        else:
            if double_tap_timer is not None and double_tap_timer.is_alive():
                double_tap_timer.cancel()
                continuous_mode = True
                update_status("🔒 Постоянная запись", "#e67e22")
            elif not is_recording:
                press_start_time = time.time()
                start_recording()


def on_release(key):
    global double_tap_timer, press_duration
    key_str = str(key)
    if key_str == current_hotkey_str:
        if not continuous_mode and not is_rebinding:
            press_duration = time.time() - press_start_time
            double_tap_timer = threading.Timer(0.3, process_and_type, args=[False])
            double_tap_timer.start()


# --- Запуск всех потоков ---
threading.Thread(target=start_tray, daemon=True).start()

keyboard_listener = keyboard.Listener(on_press=on_press, on_release=on_release)
keyboard_listener.start()

audio_stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, callback=audio_callback)
audio_stream.start()

app.mainloop()

os._exit(0)