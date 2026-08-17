
import os
import re
import json
import time
import threading
import random
from collections import deque
import queue
import webbrowser
from urllib.parse import quote_plus
import subprocess
import platform
import importlib.metadata
from pathlib import Path
import psutil
import tkinter as tk
from tkinter import simpledialog
from PIL import Image, ImageTk, ImageEnhance, ImageFilter
from tkinter import scrolledtext
from datetime import datetime, timedelta

import speech_recognition as sr
import pyttsx3

# Google Gemini current SDK.
# Google currently recommends the Interactions API for new projects.
try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None


# ============================================================
# THOR ADVANCED CONFIG
# ============================================================

# ============================================================
# THOR SINGLE-INSTANCE / OLD-PROCESS GUARD
# ============================================================
def ensure_single_thor_instance():
    """
    Prevent multiple Thor Python processes from listening to the same mic.
    Old Thor instances are cleaned up by command-line name so one spoken
    command cannot be executed 3-4 times by multiple listeners.
    """
    import ctypes

    current_pid = os.getpid()

    # Close older Thor Python processes from this project family.
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            pid = proc.info.get("pid")
            if not pid or pid == current_pid:
                continue

            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()

            if "python" not in name:
                continue

            if "thor_" in cmdline or "thor-classic" in cmdline or "thorclassic" in cmdline:
                try:
                    proc.terminate()
                except Exception:
                    pass
    except Exception as exc:
        print("Old Thor cleanup warning:", type(exc).__name__, str(exc))

    # Named Windows mutex shared by all Thor versions.
    kernel32 = ctypes.windll.kernel32
    mutex = kernel32.CreateMutexW(None, False, "Global\\SahilThorSingleInstance")
    ERROR_ALREADY_EXISTS = 183

    if not mutex:
        print("[WARN] Could not create Thor single-instance mutex.")
        return True, None

    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        print("[INFO] Another Thor instance is already running.")
        return False, mutex

    return True, mutex



MODEL = "gemini-3.6-flash"
FALLBACK_MODEL = "gemini-3.5-flash-lite"
WAKE_PHRASES = ("hello thor", "hey thor", "hi thor", "hello thore", "hey thore")

MEMORY_FILE = "thor_memory.json"
REMINDERS_FILE = "thor_reminders.json"

# Never give Gemini arbitrary shell/PowerShell access.
# Only these explicit safe tools can be called.
TOOLS = [
    {
        "type": "function",
        "name": "open_website",
        "description": "Open a website requested by the user. Use natural names such as Google, YouTube, Instagram, GitHub, Gmail, WhatsApp, Claude, or a full https URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "description": "Website name or full URL."
                }
            },
            "required": ["site"]
        }
    },

    {
        "type": "function",
        "name": "open_app",
        "description": "Open a safe desktop application. Supported app names include chrome, calculator, notepad, file explorer, vscode, settings, and whatsapp.",
        "parameters": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "description": "Application name."
                }
            },
            "required": ["app"]
        }
    },

    {
        "type": "function",
        "name": "get_pc_status",
        "description": "Get safe read-only PC information such as CPU, RAM, disk, battery, OS, and running process count.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },

    {
        "type": "function",
        "name": "media_control",
        "description": "Control currently active media. Actions: play_pause, next, previous, stop, volume_up, volume_down, mute.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "play_pause",
                        "next",
                        "previous",
                        "stop",
                        "volume_up",
                        "volume_down",
                        "mute"
                    ]
                }
            },
            "required": ["action"]
        }
    },

    {
        "type": "function",
        "name": "search_youtube",
        "description": "Search YouTube for a song, video, topic, or channel and open the results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "YouTube search query."
                }
            },
            "required": ["query"]
        }
    },

    {
        "type": "function",
        "name": "set_reminder",
        "description": "Set a local reminder. Use minutes_from_now for relative reminders or an exact 24-hour time for today/tomorrow when appropriate.",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "What the user wants to be reminded about."
                },
                "minutes_from_now": {
                    "type": "integer",
                    "description": "Minutes from now. Use this for 'in 10 minutes', 'after 1 hour', etc."
                },
                "clock_time": {
                    "type": "string",
                    "description": "Optional 24-hour HH:MM clock time."
                }
            },
            "required": ["message"]
        }
    },

    {
        "type": "function",
        "name": "take_screenshot",
        "description": "Take a screenshot of the current desktop and save it locally.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    },

    {
        "type": "function",
        "name": "close_active_window",
        "description": "Close the currently active window using Alt+F4. Do not use this for deleting files or shutting down the PC.",
        "parameters": {
            "type": "object",
            "properties": {}
        }
    }
]


# ============================================================
# VOICE
# ============================================================

engine = pyttsx3.init()
engine.setProperty("rate", 170)
engine.setProperty("volume", 1.0)

speaking_lock = threading.Lock()
speaking_event = threading.Event()


def speak(text):
    """
    Reliable Windows TTS.

    IMPORTANT:
    speaking_event MUST be cleared on every exit path. The previous V29
    implementation returned after successful SAPI speech before reaching
    the fallback finally block. That left speaking_event=True forever,
    so the microphone loop kept sleeping and THOR stopped accepting commands.
    """
    text = str(text).strip()
    if not text:
        return

    print("Thor:", text)

    def worker():
        speaking_event.set()
        try:
            with speaking_lock:
                if platform.system().lower() == "windows":
                    ps_script = r"""
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.Rate = 0
$speaker.Volume = 100
$text = [Console]::In.ReadToEnd()
$speaker.Speak($text)
$speaker.Dispose()
"""

                    try:
                        result = subprocess.run(
                            [
                                "powershell",
                                "-NoProfile",
                                "-ExecutionPolicy", "Bypass",
                                "-Command", ps_script
                            ],
                            input=text,
                            text=True,
                            capture_output=True,
                            timeout=30
                        )

                        if result.returncode == 0:
                            return

                        print(
                            "[TTS] Windows SAPI returned:",
                            result.returncode
                        )

                    except Exception as exc:
                        print(
                            "[TTS] Windows SAPI error:",
                            type(exc).__name__,
                            str(exc)
                        )

                try:
                    local_engine = pyttsx3.init("sapi5")
                    local_engine.setProperty("rate", 145)
                    local_engine.setProperty("volume", 1.0)

                    voices = local_engine.getProperty("voices")
                    if voices:
                        local_engine.setProperty("voice", voices[0].id)

                    local_engine.say(text)
                    local_engine.runAndWait()
                    local_engine.stop()

                except Exception as exc:
                    print(
                        "[TTS] Fallback voice engine error:",
                        type(exc).__name__,
                        str(exc)
                    )

        finally:
            # CRITICAL: always release the microphone after TTS.
            speaking_event.clear()
            print("[TTS] microphone released")

    threading.Thread(
        target=worker,
        daemon=True,
        name="Thor-TTS"
    ).start()


# ============================================================
# MEMORY
# ============================================================

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, value):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


conversation_memory = load_json(MEMORY_FILE, [])
reminders = load_json(REMINDERS_FILE, [])


def remember_user_message(text):
    conversation_memory.append({
        "role": "user",
        "content": text,
        "time": datetime.now().isoformat(timespec="seconds")
    })
    del conversation_memory[:-40]
    save_json(MEMORY_FILE, conversation_memory)


def remember_thor_message(text):
    conversation_memory.append({
        "role": "assistant",
        "content": text,
        "time": datetime.now().isoformat(timespec="seconds")
    })
    del conversation_memory[:-40]
    save_json(MEMORY_FILE, conversation_memory)


# ============================================================
# CREATOR IDENTITY
# ============================================================

def creator_answer(text):
    q = text.lower().strip()

    words = [
        "who created you",
        "who made you",
        "who is your creator",
        "who is your developer",
        "thor creator",
        "thor father",
        "thor dad",
        "thor papa",
        "thor baap",
        "thor ka baap",
        "thor ke baap",
        "thor ke papa",
        "thor ka papa",
        "thor ke dad",
        "thor ka dad",
        "kisne banaya",
        "kisne banaya hai",
        "tumhe kisne banaya",
        "thor ko kisne banaya",
        "thor ko kisne janam diya",
        "baap kaun hai",
        "father kaun hai",
        "dad kaun hai",
        "papa kaun hai",
        "creator kaun hai",
        "developer kaun hai"
    ]

    if "thor" in q and any(
        x in q for x in [
            "father", "dad", "papa", "baap",
            "creator", "developer", "janam", "banaya"
        ]
    ):
        return (
            "Mujhe Sahil Suman ne banaya hai. "
            "Wahi mere creator aur developer hain."
        )

    if any(x in q for x in words):
        return (
            "Mujhe Sahil Suman ne banaya hai. "
            "Wahi mere creator aur developer hain."
        )

    return None


# ============================================================
# SAFE LOCAL TOOLS
# ============================================================

SITE_MAP = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "brave": "brave://newtab",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "claude": "https://claude.ai",
    "chatgpt": "https://chatgpt.com",
    "gemini": "https://gemini.google.com",
    "linkedin": "https://www.linkedin.com",
    "google ai studio": "https://aistudio.google.com"
}


def _launch_browser(browser):
    """Launch a requested Chromium browser and give it time to create a window."""
    commands = {
        "chrome": "start chrome",
        "brave": "start brave",
        "edge": "start msedge",
    }
    command = commands.get(browser)
    if not command:
        return False

    try:
        subprocess.Popen(command, shell=True)
        for _ in range(20):
            time.sleep(0.15)
            if activate_browser_window():
                return True
        return True
    except Exception as exc:
        print("[BROWSER] Launch error:", type(exc).__name__, str(exc))
        return False


def open_website(site, preferred_browser=None):
    """Open a known or arbitrary website, launching a browser if necessary."""
    site = site.strip().lower()
    url = SITE_MAP.get(site)

    if not url:
        if site.startswith(("http://", "https://")):
            url = site
        elif "." in site:
            url = "https://" + site
        else:
            url = "https://www.google.com/search?q=" + quote_plus(site)

    # If a browser window already exists, reuse its current tab.
    if activate_browser_window():
        if navigate_current_browser(url):
            return f"Opening {site} in the current browser tab."

    # No browser window: launch the explicitly requested browser.
    browser = preferred_browser
    if browser is None:
        if site == "brave":
            browser = "brave"
        elif site in ("chrome", "google chrome"):
            browser = "chrome"
        else:
            # THOR default: Brave, then Chrome if Brave is unavailable.
            browser = "brave"

    if _launch_browser(browser):
        # Navigate the newly opened browser to the requested URL.
        if navigate_current_browser(url):
            return f"Opening {site} in {browser}."

        # A just-launched browser may need a little extra time.
        time.sleep(0.7)
        if navigate_current_browser(url):
            return f"Opening {site} in {browser}."

    # Last fallback: launch the browser directly with the URL.
    try:
        if browser == "chrome":
            subprocess.Popen(f'start chrome "{url}"', shell=True)
        elif browser == "brave":
            subprocess.Popen(f'start brave "{url}"', shell=True)
        else:
            subprocess.Popen(f'start msedge "{url}"', shell=True)
        return f"Opening {site} in {browser}."
    except Exception as exc:
        print("[BROWSER] Direct launch error:", type(exc).__name__, str(exc))
        return f"I couldn't open {site}."


def open_app(app):
    app = app.lower().strip()

    commands = {
        "chrome": "start chrome",
        "google chrome": "start chrome",
        "brave": "start brave",
        "calculator": "start calc",
        "calc": "start calc",
        "notepad": "start notepad",
        "file explorer": "start explorer",
        "explorer": "start explorer",
        "settings": "start ms-settings:",
        "vscode": "code",
        "vs code": "code",
        "visual studio code": "code"
    }

    if app == "whatsapp":
        if navigate_current_browser(SITE_MAP["whatsapp"]):
            return "Opening WhatsApp in the current browser tab."
        return "Please open Chrome or Brave first."

    command = commands.get(app)
    if not command:
        return f"I don't have permission to open the app named {app}."

    try:
        subprocess.Popen(command, shell=True)
        return f"Opening {app}."
    except Exception:
        return f"I could not open {app}."


def get_pc_status():
    cpu = psutil.cpu_percent(interval=0.3)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(os.path.abspath(os.sep))

    battery = None
    try:
        battery = psutil.sensors_battery()
    except Exception:
        pass

    result = {
        "OS": platform.platform(),
        "CPU usage": f"{cpu:.0f}%",
        "RAM usage": f"{memory.percent:.0f}% ({memory.used / (1024**3):.1f} GB used)",
        "Disk usage": f"{disk.percent:.0f}%",
        "Running processes": len(psutil.pids())
    }

    if battery:
        result["Battery"] = f"{battery.percent:.0f}%"

    return json.dumps(result, ensure_ascii=False)


def media_control(action):
    """Fast native Windows media control with pyautogui fallback."""
    import ctypes

    VK = {
        "play_pause": 0xB3,
        "next": 0xB0,
        "previous": 0xB1,
        "stop": 0xB2,
        "volume_up": 0xAF,
        "volume_down": 0xAE,
        "mute": 0xAD,
    }

    key = VK.get(action)
    if key is None:
        return "Unsupported media action."

    try:
        user32 = ctypes.windll.user32
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.03)
        user32.keybd_event(key, 0, KEYEVENTF_KEYUP, 0)

        return {
            "play_pause": "Media toggled.",
            "next": "Next track.",
            "previous": "Previous track.",
            "stop": "Media stopped.",
            "volume_up": "Volume increased.",
            "volume_down": "Volume decreased.",
            "mute": "Mute toggled.",
        }[action]

    except Exception:
        try:
            import pyautogui
            fallback = {
                "play_pause": "playpause",
                "next": "nexttrack",
                "previous": "prevtrack",
                "stop": "stop",
                "volume_up": "volumeup",
                "volume_down": "volumedown",
                "mute": "volumemute",
            }
            pyautogui.press(fallback[action])
            return f"Media action completed: {action}."
        except Exception as exc:
            print("Media control error:", type(exc).__name__, str(exc))
            return "I couldn't control the media player."



def search_youtube(query):
    """
    Find the first YouTube result, reuse the current browser tab, and use
    YouTube's muted-autoplay path so Chromium autoplay policy is less likely
    to leave the video paused. The video is then unmuted.
    """
    global last_music_query, last_music_query_time

    query = " ".join(normalize(query).split())
    now = time.time()

    if (
        query
        and query == last_music_query
        and now - last_music_query_time < 6.0
    ):
        print("[MEDIA] Duplicate YouTube launch ignored:", query)
        return f"{query} is already being opened."

    if not query:
        return "Sir, song ka naam bataiye."

    last_music_query = query
    last_music_query_time = now

    search_url = (
        "https://www.youtube.com/results?search_query="
        + quote_plus(query)
    )

    try:
        import requests

        response = requests.get(
            search_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/151 Safari/537.36"
                )
            },
            timeout=6
        )

        ids = re.findall(
            r'"videoId":"([A-Za-z0-9_-]{11})"',
            response.text
        )

        unique_ids = list(dict.fromkeys(ids))

        if unique_ids:
            watch_url = (
                "https://www.youtube.com/watch?v="
                + unique_ids[0]
                + "&autoplay=1&mute=1&playsinline=1"
            )

            if not navigate_current_browser(watch_url):
                return "Please open Chrome or Brave first."

            # Chromium browsers commonly allow muted autoplay. After the
            # player loads, toggle mute so the requested song has sound.
            def unmute_after_load():
                try:
                    import pyautogui
                    time.sleep(2.2)
                    if activate_browser_window():
                        pyautogui.press("m")
                        print("[MEDIA] YouTube autoplay/unmute attempted.")
                except Exception as exc:
                    print(
                        "[MEDIA] YouTube unmute error:",
                        type(exc).__name__,
                        str(exc)
                    )

            threading.Thread(
                target=unmute_after_load,
                daemon=True,
                name="Thor-YouTube-Playback"
            ).start()

            return f"Playing {query} on YouTube."

    except Exception as exc:
        print("YouTube search error:", type(exc).__name__, str(exc))

    # If result extraction fails, still open the search in the existing tab.
    if navigate_current_browser(search_url):
        return f"I opened YouTube search for {query}."

    return "Please open Chrome or Brave first."



def set_reminder(message, minutes_from_now=None, clock_time=None):
    now = datetime.now()

    if minutes_from_now is not None:
        due = now + timedelta(minutes=int(minutes_from_now))
    elif clock_time:
        try:
            hour, minute = map(int, clock_time.split(":"))
            due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
        except Exception:
            return "I could not understand that clock time."
    else:
        return "Please give me a time for the reminder."

    item = {
        "message": message,
        "due": due.isoformat(timespec="seconds"),
        "done": False
    }

    reminders.append(item)
    save_json(REMINDERS_FILE, reminders)

    return f"Reminder set for {due.strftime('%I:%M %p')}: {message}"


def deliver_reminder(message):
    """Deliver a reminder safely from the Tkinter main thread."""
    msg = "Reminder: " + message

    def show():
        try:
            add_message("THOR", msg)
            update_state("REMINDER", "● REMINDER  •  THOR HAS A REMINDER FOR YOU")
        except Exception as exc:
            print("Reminder GUI error:", type(exc).__name__, str(exc))

        try:
            import winsound
            for _ in range(3):
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                time.sleep(0.25)
        except Exception:
            pass

        speak(msg)

        # Return the status indicator to normal after the reminder.
        try:
            root.after(5000, lambda: update_state(
                "READY", "● ONLINE  •  READY FOR NEXT COMMAND"
            ))
        except Exception:
            pass

    try:
        root.after(0, show)
    except Exception:
        # Fallback if the GUI has not started yet.
        speak(msg)


def reminder_loop():
    while True:
        now = datetime.now()
        changed = False

        for item in reminders:
            if item.get("done"):
                continue

            try:
                due = datetime.fromisoformat(item["due"])
            except Exception:
                continue

            if now >= due:
                item["done"] = True
                changed = True
                deliver_reminder(item.get("message", "You have a reminder."))

        if changed:
            save_json(REMINDERS_FILE, reminders)

        time.sleep(1)


def take_screenshot():
    from PIL import ImageGrab

    folder = os.path.join(os.getcwd(), "thor_screenshots")
    os.makedirs(folder, exist_ok=True)

    path = os.path.join(
        folder,
        "thor_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".png"
    )

    image = ImageGrab.grab()
    image.save(path)

    return f"Screenshot saved to {path}."


def close_active_window():
    import pyautogui
    pyautogui.hotkey("alt", "f4")
    return "Closed the active window."


FUNCTIONS = {
    "open_website": open_website,
    "open_app": open_app,
    "get_pc_status": get_pc_status,
    "media_control": media_control,
    "search_youtube": search_youtube,
    "set_reminder": set_reminder,
    "take_screenshot": take_screenshot,
    "close_active_window": close_active_window
}


# ============================================================
# GEMINI AGENT
# ============================================================

client = None
previous_interaction_id = None  # kept for compatibility with old saved state
gemini_history = []
pending_music_request = False
ai_command_queue = queue.Queue(maxsize=50)
health_lock = threading.Lock()
voice_restart_count = 0

SYSTEM_INSTRUCTION = """
You are THOR, Sahil Suman's personal AI voice assistant.

CREATOR:
Your creator and developer is Sahil Suman.

PERSONALITY:
- Friendly, intelligent, calm and helpful.
- Speak naturally like a human assistant.
- Understand Hindi, English and Hinglish.
- Understand imperfect grammar, accents, short phrases and natural speech.
- Do not require programmer-style commands.
- Understand the user's intent rather than matching exact command phrases.
- Understand Hindi, English, Hinglish, imperfect grammar, and natural speech.
- Infer follow-ups such as "isko", "usko", "wahan", "again", "also", and "same thing".
- Decide automatically whether the user wants an answer, web research, or a safe computer action.
- Use conversation context for words like "it", "that", "this", "him", "there",
  "again", "also", "aur", "iska", "uska", etc.

CONVERSATION:
- The user should be able to talk naturally after saying "Hello Thor".
- Do not ask the user to repeat "Hello Thor" after every sentence.
- Keep answers concise enough for voice, but give enough detail to be useful.
- If the user asks a follow-up, continue the same topic.
- If the user asks "what do you know about me", only mention information explicitly
  stored in the provided conversation memory.

WEB:
- You have Google Search.
- Decide yourself when current or uncertain information needs web research.
- Search for latest/current/recent/news/price/result information.
- Search when you are genuinely unsure instead of inventing facts.
- If the user explicitly asks to search/check Google/internet, search.
- For stable common knowledge, answer directly without unnecessary search.
- When you search, summarize the useful result for the user in natural language.

COMPUTER:
- Use the provided safe functions when the user's intent clearly requires an action.
- Do not invent that an action happened; use a tool and then report the result.
- Never execute arbitrary PowerShell, CMD, shell, registry, or downloaded code.
- Never delete files.
- Never disable antivirus/firewall.
- Never change passwords or security settings.
- Never shut down or restart the PC through a hidden command.
- For destructive or security-sensitive actions, refuse or ask for explicit confirmation.
- Opening websites/apps, media controls, PC status, screenshots, and reminders are allowed.

EMAIL / COMMUNICATION:
- You may help draft emails and messages.
- Do not send messages automatically unless a dedicated safe send tool is later added.
- WhatsApp can be opened; do not pretend that a WhatsApp call happened unless an
  actual supported call action was successfully executed.

VOICE:
- Never say "I ran a command" or mention internal tools unless the user asks.
- Respond as Thor, not as a programmer's command-line program.
"""


def initialize_gemini():
    if _gemini_in_cooldown():
        return False, "Gemini temporarily rate-limited; local tools remain available."
    global client

    if genai is None or types is None:
        return False, (
            "google-genai SDK missing. "
            "Run: py -3.13 -m pip install -U google-genai"
        )

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return False, (
            "GEMINI_API_KEY is not set. "
            "Set your Gemini API key in the environment and restart Thor."
        )

    try:
        try:
            sdk_version = importlib.metadata.version("google-genai")
        except Exception:
            sdk_version = "unknown"

        print("[GEMINI] google-genai SDK:", sdk_version)
        print("[GEMINI] Primary model:", MODEL)
        print("[GEMINI] Fallback model:", FALLBACK_MODEL)
        print("[GEMINI] API key detected: YES")
        print("[GEMINI] Rate-limit protection: ON | fallback:", FALLBACK_MODEL)

        # The Interactions API + built-in/custom tool combination requires
        # google-genai SDK >= 2.0.0.
        try:
            major = int(sdk_version.split(".")[0])
            if major < 2:
                return False, (
                    f"google-genai {sdk_version} is too old. "
                    "Run: py -3.13 -m pip install -U google-genai"
                )
        except Exception:
            print("[GEMINI] Could not parse SDK version; continuing.")

        client = genai.Client(api_key=api_key)

        # Fast connectivity/authentication smoke test.
        # No tools are involved, so a tool-schema problem cannot hide a
        # basic API-key/model/network problem.
        probe = client.interactions.create(
            model=MODEL,
            input="Reply with exactly: THOR_OK"
        )

        probe_text = (getattr(probe, "output_text", None) or "").strip()
        if not probe_text:
            return False, "Gemini probe returned no text."

        print("[GEMINI] Connection probe: PASS ->", probe_text[:80])
        return True, "Gemini ready."

    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print("[GEMINI INIT ERROR]", error)
        return False, error

def execute_tool_call(step):
    name = step.name
    args = step.arguments or {}

    function = FUNCTIONS.get(name)

    if not function:
        return {
            "ok": False,
            "error": "Unknown tool."
        }

    try:
        result = function(**args)
        return {
            "ok": True,
            "result": result
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": "Tool failed safely."
        }


WEB_TRIGGER_PHRASES = (
    "latest", "current", "today", "tonight", "this week", "this month",
    "recent", "news", "price", "stock", "weather", "result", "score",
    "search the web", "search online", "search internet", "google it",
    "check online", "look up", "khojo", "khoj", "internet par",
    "google par", "google pe",
    "who is", "what is", "what are", "who was", "where is",
    "when is", "how much is", "kya hai", "kaun hai", "kaun tha",
    "kiske baare mein", "ke baare mein", "tell me about"
)


def needs_web_search(text):
    q = normalize(text)
    return any(phrase in q for phrase in WEB_TRIGGER_PHRASES)



# ==================== V42 GEMINI RATE-LIMIT PROTECTION ====================
GEMINI_MIN_INTERVAL = 2.0
GEMINI_RPM_LIMIT = 12
GEMINI_MAX_RETRIES = 3
GEMINI_BACKOFF_BASE = 2.0

_gemini_gate_lock = threading.Lock()
_gemini_call_times = deque()
_gemini_last_call = 0.0

_gemini_cooldown_until = 0.0
_gemini_cooldown_lock = threading.Lock()


def _extract_retry_seconds(exc, default=30.0):
    text = str(exc)
    patterns = [
        r"retry in\s+([0-9]+(?:\.[0-9]+)?)s",
        r"retryDelay[^0-9]*([0-9]+)s",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            try:
                return max(5.0, float(match.group(1)))
            except Exception:
                pass
    return default


def _set_gemini_cooldown(seconds):
    global _gemini_cooldown_until
    with _gemini_cooldown_lock:
        _gemini_cooldown_until = max(
            _gemini_cooldown_until,
            time.monotonic() + float(seconds)
        )
    print(f"[GEMINI] Cooldown active for about {float(seconds):.0f}s")


def _gemini_in_cooldown():
    with _gemini_cooldown_lock:
        return time.monotonic() < _gemini_cooldown_until




def _is_gemini_rate_limit_error(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(x in text for x in (
        "429", "too many requests", "rate limit",
        "resource exhausted", "quota"
    ))


def _wait_for_gemini_slot():
    global _gemini_last_call
    with _gemini_gate_lock:
        now = time.monotonic()
        gap = GEMINI_MIN_INTERVAL - (now - _gemini_last_call)
        if gap > 0:
            time.sleep(gap)

        now = time.monotonic()
        while _gemini_call_times and now - _gemini_call_times[0] >= 60:
            _gemini_call_times.popleft()

        if len(_gemini_call_times) >= GEMINI_RPM_LIMIT:
            wait = 60 - (now - _gemini_call_times[0]) + 0.1
            print(f"[GEMINI] Local RPM guard: waiting {wait:.1f}s")
            time.sleep(max(0, wait))

        now = time.monotonic()
        _gemini_call_times.append(now)
        _gemini_last_call = now


def _gemini_request_with_retry(request_fn, fallback_fn=None):
    """Single-flight Gemini request with bounded 429 backoff and cooldown."""
    if _gemini_in_cooldown():
        print("[GEMINI] Temporarily offline because the API returned 429.")
        if fallback_fn is not None:
            try:
                _wait_for_gemini_slot()
                return fallback_fn()
            except Exception as exc:
                print("[GEMINI] Fallback while cooling down failed:",
                      type(exc).__name__, str(exc))
        return None

    last_exc = None

    for attempt in range(GEMINI_MAX_RETRIES + 1):
        _wait_for_gemini_slot()
        try:
            return request_fn()
        except Exception as exc:
            last_exc = exc

            if not _is_gemini_rate_limit_error(exc):
                print("[GEMINI] Request failed:",
                      type(exc).__name__, str(exc))
                break

            retry_seconds = _extract_retry_seconds(
                exc,
                default=2.0 * (2 ** attempt)
            )
            _set_gemini_cooldown(retry_seconds)

            # Do not hammer the API during the provider-specified cooldown.
            # Try the alternate model once; if it is also limited, return.
            if fallback_fn is not None:
                try:
                    _wait_for_gemini_slot()
                    return fallback_fn()
                except Exception as fallback_exc:
                    print("[GEMINI] Fallback failed:",
                          type(fallback_exc).__name__, str(fallback_exc))
            break

    return None



def gemini_web_turn(user_text):
    """Real-time Google Search answer path using the current Gemini API."""
    global last_gemini_error

    if client is None:
        ok, reason = initialize_gemini()
        if not ok:
            print("[GEMINI WEB INIT ERROR]", reason)
            return None

    # Current Interactions API supports server-side Google Search tools.
    try:
        interaction = _gemini_request_with_retry(
            lambda: client.interactions.create(
                model=MODEL,
                input=user_text,
                system_instruction=SYSTEM_INSTRUCTION,
                tools=[{"type": "google_search"}],
                generation_config={"thinking_level": "low"},
            ),
            fallback_fn=lambda: client.interactions.create(
                model=FALLBACK_MODEL,
                input=user_text,
                system_instruction=SYSTEM_INSTRUCTION,
                generation_config={"thinking_level": "low"},
            ),
        )

        answer = getattr(interaction, "output_text", None)
        if answer:
            print("[GEMINI WEB] Interactions search response received.")
            return answer.strip()

    except Exception as exc:
        last_gemini_error = f"{type(exc).__name__}: {exc}"
        print("[GEMINI WEB INTERACTIONS ERROR]", last_gemini_error)

    # Legacy/fallback path.
    try:
        search_tool = types.Tool(
            google_search=types.GoogleSearch()
        )

        response = _gemini_request_with_retry(
            lambda: client.models.generate_content(
            model=MODEL,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_INSTRUCTION,
                tools=[search_tool],
            ),
        ),
        )

        answer = getattr(response, "text", None)
        if answer:
            print("[GEMINI WEB] generate_content search response received.")
            return answer.strip()

    except Exception as exc:
        last_gemini_error = f"{type(exc).__name__}: {exc}"
        print("[GEMINI WEB ERROR]", last_gemini_error)

    return None



def gemini_turn(user_text):
    """
    THOR Gemini brain.

    Primary path:
      Gemini 3.6 Flash + Interactions API
      - server-side conversation state
      - Google Search + custom Thor functions
      - previous_interaction_id preserves tool context/signatures

    Fallback path:
      Gemini 2.5 Flash + generate_content() plain chat
      - used only if the primary interaction call fails.
    """
    global previous_interaction_id, last_gemini_error
    last_gemini_error = None

    if client is None:
        ok, reason = initialize_gemini()
        if not ok:
            print("[GEMINI INIT ERROR]", reason)
            return None

    try:
        tools = list(TOOLS)

        kwargs = {
            "model": MODEL,
            "input": user_text,
            "system_instruction": SYSTEM_INSTRUCTION,
            "tools": tools,
            "generation_config": {
                "thinking_level": "low"
            },
        }

        if previous_interaction_id:
            kwargs["previous_interaction_id"] = previous_interaction_id

        interaction = _gemini_request_with_retry(
            lambda: client.interactions.create(**kwargs),
            fallback_fn=lambda: client.interactions.create(
                model=FALLBACK_MODEL,
                input=user_text,
                system_instruction=SYSTEM_INSTRUCTION,
                generation_config={"thinking_level": "low"},
            ),
        )
        previous_interaction_id = interaction.id

        for _ in range(5):
            function_calls = [
                step for step in (interaction.steps or [])
                if getattr(step, "type", None) == "function_call"
            ]

            if not function_calls:
                answer = getattr(interaction, "output_text", None)

                if answer:
                    return answer.strip()

                parts = []
                for step in (interaction.steps or []):
                    if getattr(step, "type", None) == "model_output":
                        for block in getattr(step, "content", []) or []:
                            if getattr(block, "type", None) == "text":
                                parts.append(getattr(block, "text", ""))

                answer = " ".join(x for x in parts if x).strip()
                return answer or "I completed the request."

            results = []

            for call in function_calls:
                name = getattr(call, "name", "")
                args = dict(getattr(call, "arguments", {}) or {})
                function = FUNCTIONS.get(name)

                if not function:
                    result = {
                        "ok": False,
                        "error": f"Unknown Thor tool: {name}"
                    }
                else:
                    try:
                        print("[GEMINI TOOL]", name, args)
                        result = function(**args)
                    except Exception as exc:
                        print(
                            f"[TOOL ERROR] {name}:",
                            type(exc).__name__,
                            str(exc)
                        )
                        result = {
                            "ok": False,
                            "error": "Tool failed safely."
                        }

                results.append({
                    "type": "function_result",
                    "name": name,
                    "call_id": call.id,
                    "result": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                result,
                                ensure_ascii=False,
                                default=str
                            )
                        }
                    ],
                })

            interaction = _gemini_request_with_retry(
                lambda: client.interactions.create(
                    model=MODEL,
                    previous_interaction_id=interaction.id,
                    input=results,
                    system_instruction=SYSTEM_INSTRUCTION,
                    tools=tools,
                    generation_config={
                        "thinking_level": "low"
                    },
                ),
                fallback_fn=None,
            )

            previous_interaction_id = interaction.id

        return "I completed the available actions, sir."

    except Exception as exc:
        last_gemini_error = f"{type(exc).__name__}: {exc}"
        print("[GEMINI INTERACTIONS ERROR]", last_gemini_error)

        try:
            fallback = _gemini_request_with_retry(
                lambda: client.models.generate_content(
                    model=FALLBACK_MODEL,
                    contents=user_text,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_INSTRUCTION
                    ),
                )
            )

            answer = getattr(fallback, "text", None)
            if answer:
                return answer.strip()

        except Exception as fallback_exc:
            last_gemini_error = (
                f"{type(fallback_exc).__name__}: {fallback_exc}"
            )
            print("[GEMINI FALLBACK ERROR]", last_gemini_error)

        return None


# ============================================================
# THOR CLASSIC DASHBOARD — SAHIL PHOTO EDITION
# ============================================================

root = tk.Tk()
root.title("THOR — Personal AI Assistant")
root.geometry("1250x820")
root.minsize(1000, 700)
root.configure(bg="#050608")

# ---------- Background image ----------
PHOTO_PATH = Path(__file__).with_name("sahil_thor_background.jpeg")
try:
    original_bg = Image.open(PHOTO_PATH).convert("RGB")
except Exception:
    original_bg = None

bg_label = tk.Label(root, bg="#050608", bd=0, highlightthickness=0)
bg_label.place(relx=0, rely=0, relwidth=1, relheight=1)
bg_label.lower()


def refresh_background(event=None):
    if original_bg is None:
        return

    w = max(root.winfo_width(), 1000)
    h = max(root.winfo_height(), 700)

    # Cover the full window while preserving aspect ratio.
    img = original_bg.copy()
    ratio = max(w / img.width, h / img.height)
    nw = int(img.width * ratio)
    nh = int(img.height * ratio)
    img = img.resize((nw, nh), Image.LANCZOS)

    left = max((nw - w) // 2, 0)
    top = max((nh - h) // 2, 0)
    img = img.crop((left, top, left + w, top + h))

    # Darken and slightly soften the photo so the UI remains readable.
    img = ImageEnhance.Brightness(img).enhance(0.36)
    img = ImageEnhance.Contrast(img).enhance(1.08)
    img = img.filter(ImageFilter.GaussianBlur(0.5))

    bg_image = ImageTk.PhotoImage(img)
    bg_label.configure(image=bg_image)
    bg_label.image = bg_image


root.bind("<Configure>", refresh_background)

# ---------- Theme ----------
BG = "#050608"
PANEL = "#090b10"
PANEL_2 = "#10141c"
PANEL_3 = "#151b25"
TEXT = "#f4f6fa"
MUTED = "#a1a8b5"
GOLD = "#d7a84b"
GOLD_BRIGHT = "#f3ca70"
CYAN = "#19d8c4"
USER_BG = "#1b1b1f"
THOR_BG = "#10151d"
BORDER = "#6f5120"

status_var = tk.StringVar(value="● ONLINE  •  MICROSOFT VOICE READY  •  GEMINI CHECKING")
state_var = tk.StringVar(value="READY")

# ---------- Utility ----------
def update_state(state, status=None):
    # Tkinter widgets/variables must be touched from the main thread.
    if threading.current_thread() is not threading.main_thread():
        try:
            root.after(0, lambda: update_state(state, status))
        except Exception:
            pass
        return

    state_var.set(state.upper())
    if status:
        status_var.set(status)


def add_message(sender, message):
    # Worker threads (voice, Gemini, reminders) must marshal GUI work to
    # Tkinter's main thread. This removes intermittent "no response" and
    # random GUI freezes caused by cross-thread widget access.
    if threading.current_thread() is not threading.main_thread():
        try:
            root.after(0, lambda: add_message(sender, message))
        except Exception:
            pass
        return

    try:
        bubble_bg = USER_BG if sender == "YOU" else THOR_BG
        title = "YOU" if sender == "YOU" else "⚡ THOR"
        title_color = GOLD_BRIGHT if sender == "YOU" else CYAN

        wrapper = tk.Frame(chat_box, bg="#000000", bd=0)
        wrapper.pack(fill=tk.X, padx=18, pady=7)

        bubble = tk.Frame(
            wrapper,
            bg=bubble_bg,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=15,
            pady=10
        )
        bubble.pack(
            anchor="e" if sender == "YOU" else "w",
            padx=(180 if sender == "YOU" else 0, 0)
        )

        tk.Label(
            bubble,
            text=title,
            bg=bubble_bg,
            fg=title_color,
            font=("Segoe UI", 9, "bold")
        ).pack(anchor="w")

        tk.Label(
            bubble,
            text=message,
            bg=bubble_bg,
            fg=TEXT,
            font=("Segoe UI", 11),
            justify=tk.LEFT,
            wraplength=590
        ).pack(anchor="w", pady=(4, 0))

        root.update_idletasks()
        chat_canvas.yview_moveto(1.0)
    except Exception as exc:
        print("GUI message error:", exc)


# ---------- Header ----------
header = tk.Frame(
    root, bg="#050608",
    highlightbackground=BORDER, highlightthickness=1
)
header.place(relx=0.015, rely=0.018, relwidth=0.97, height=74)

brand = tk.Frame(header, bg="#050608")
brand.pack(side=tk.LEFT, padx=20)

tk.Label(
    brand, text="⚡", bg="#050608", fg=CYAN,
    font=("Segoe UI Symbol", 30, "bold")
).pack(side=tk.LEFT, padx=(0, 8))

tk.Label(
    brand, text="THOR", bg="#050608", fg=GOLD_BRIGHT,
    font=("Segoe UI", 28, "bold")
).pack(side=tk.LEFT)

tk.Label(
    brand, text="  |  PERSONAL AI ASSISTANT",
    bg="#050608", fg=TEXT,
    font=("Segoe UI", 10, "bold")
).pack(side=tk.LEFT, pady=(13, 0))

status_panel = tk.Frame(
    header, bg="#0c1119",
    highlightbackground=BORDER, highlightthickness=1,
    padx=15, pady=8
)
status_panel.pack(side=tk.RIGHT, padx=14, pady=12)

tk.Label(
    status_panel, textvariable=status_var,
    bg="#0c1119", fg=CYAN,
    font=("Segoe UI", 9, "bold")
).pack()

# ---------- Main layout ----------
content = tk.Frame(root, bg="#000000")
content.place(relx=0.015, rely=0.115, relwidth=0.97, relheight=0.665)

# Left navigation
sidebar = tk.Frame(
    content, bg="#080a0e",
    highlightbackground=BORDER, highlightthickness=1,
    width=190
)
sidebar.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
sidebar.pack_propagate(False)

tk.Label(
    sidebar, text="⚡", bg="#080a0e", fg=GOLD_BRIGHT,
    font=("Segoe UI Symbol", 28, "bold")
).pack(pady=(18, 0))

tk.Label(
    sidebar, text="THOR", bg="#080a0e", fg=GOLD_BRIGHT,
    font=("Segoe UI", 18, "bold")
).pack()

tk.Label(
    sidebar, text="AI COMMAND CENTER",
    bg="#080a0e", fg=MUTED,
    font=("Segoe UI", 8, "bold")
).pack(pady=(0, 16))

# Center chat
center = tk.Frame(
    content, bg="#07090d",
    highlightbackground=BORDER, highlightthickness=1
)
center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

welcome = tk.Frame(center, bg="#07090d")
welcome.pack(fill=tk.X, padx=18, pady=12)

tk.Label(
    welcome, text="WELCOME BACK, SAHIL SUMAN",
    bg="#07090d", fg=GOLD_BRIGHT,
    font=("Segoe UI", 13, "bold")
).pack(anchor="w")

tk.Label(
    welcome, text="Natural voice • AI reasoning • safe PC tools",
    bg="#07090d", fg=MUTED,
    font=("Segoe UI", 9)
).pack(anchor="w", pady=(2, 0))

chat_area = tk.Frame(center, bg="#07090d")
chat_area.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

chat_canvas = tk.Canvas(
    chat_area, bg="#07090d", highlightthickness=0
)
chat_scroll = tk.Scrollbar(
    chat_area, orient=tk.VERTICAL, command=chat_canvas.yview
)
chat_box = tk.Frame(chat_canvas, bg="#07090d")

chat_box_window = chat_canvas.create_window(
    (0, 0), window=chat_box, anchor="nw"
)

chat_box.bind(
    "<Configure>",
    lambda e: chat_canvas.configure(
        scrollregion=chat_canvas.bbox("all")
    )
)

chat_canvas.bind(
    "<Configure>",
    lambda e: chat_canvas.itemconfigure(
        chat_box_window, width=e.width
    )
)

chat_canvas.configure(yscrollcommand=chat_scroll.set)
chat_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
chat_scroll.pack(side=tk.RIGHT, fill=tk.Y)

# Right PC status panel
right = tk.Frame(
    content, bg="#080b10",
    highlightbackground=BORDER, highlightthickness=1,
    width=270
)
right.pack(side=tk.RIGHT, fill=tk.Y)
right.pack_propagate(False)

tk.Label(
    right, text="🖥  PC STATUS",
    bg="#080b10", fg=GOLD_BRIGHT,
    font=("Segoe UI", 14, "bold")
).pack(anchor="w", padx=16, pady=(18, 10))

pc_status_box = tk.Label(
    right,
    text="Click PC Status to check your system.",
    bg="#0d1118",
    fg=TEXT,
    justify=tk.LEFT,
    anchor="nw",
    wraplength=235,
    padx=12,
    pady=12,
    font=("Segoe UI", 9),
    highlightbackground=BORDER,
    highlightthickness=1
)
pc_status_box.pack(fill=tk.X, padx=12)

tk.Label(
    right,
    text="Your photo is used locally as Thor's background.",
    bg="#080b10", fg=MUTED,
    wraplength=235,
    justify=tk.LEFT,
    font=("Segoe UI", 8)
).pack(anchor="w", padx=16, pady=14)


# ---------- Real button actions ----------
def run_gui_action(action, show_result=True):
    """Run a non-dialog GUI action in a worker thread."""
    def worker():
        try:
            update_state("THINKING", "● RUNNING ACTION...")
            result = action()
            if show_result and result is not None:
                add_message("THOR", str(result))
            update_state("READY", "● ONLINE  •  THOR READY")
        except Exception as exc:
            print("GUI action error:", type(exc).__name__, str(exc))
            add_message("THOR", "I couldn't complete that action.")
            update_state("READY", "● ONLINE  •  ACTION FAILED")
    threading.Thread(
        target=worker,
        daemon=True,
        name="Thor-GUI-Action"
    ).start()


def run_background_action(fn, success_message=True):
    """Execute a completed GUI action without blocking Tkinter."""
    def worker():
        try:
            update_state("THINKING", "● RUNNING ACTION...")
            result = fn()
            if success_message and result is not None:
                add_message("THOR", str(result))
            update_state("READY", "● ONLINE  •  THOR READY")
        except Exception as exc:
            print("GUI background action error:", type(exc).__name__, str(exc))
            add_message("THOR", "I couldn't complete that action.")
            update_state("READY", "● ONLINE  •  ACTION FAILED")
    threading.Thread(
        target=worker,
        daemon=True,
        name="Thor-GUI-Background"
    ).start()


def gui_pc_status():
    """Dialog-free PC status action; safe from any worker."""
    def worker():
        try:
            result = get_pc_status()

            def apply():
                try:
                    pc_status_box.configure(text=str(result))
                    add_message("THOR", "PC status updated.")
                    update_state("READY", "● ONLINE  •  PC STATUS UPDATED")
                except Exception as exc:
                    print("PC status GUI error:", type(exc).__name__, str(exc))

            root.after(0, apply)
        except Exception as exc:
            print("PC status error:", type(exc).__name__, str(exc))
            add_message("THOR", "I couldn't read the PC status.")
            update_state("READY", "● ONLINE  •  ACTION FAILED")

    threading.Thread(target=worker, daemon=True, name="Thor-PC-Status").start()


def gui_google_search():
    """Ask on Tkinter's main thread, then search in a worker."""
    query = simpledialog.askstring(
        "Google Search",
        "What should Thor search on Google?",
        parent=root
    )
    if not query:
        return

    def work():
        if navigate_current_browser(
            "https://www.google.com/search?q=" + quote_plus(query)
        ):
            return f"Google search opened in the current tab for: {query}"
        return "Please open Chrome or Brave first."

    run_background_action(work)


def gui_youtube_search():
    query = simpledialog.askstring(
        "YouTube Search",
        "Which video/song should Thor find?",
        parent=root
    )
    if not query:
        return
    run_background_action(lambda: search_youtube(query))


def gui_open_website():
    site = simpledialog.askstring(
        "Open Website",
        "Website name: Google, YouTube, Instagram, WhatsApp, ChatGPT",
        parent=root
    )
    if not site:
        return
    run_background_action(lambda: open_website(site.strip().lower()))


def gui_reminder():
    message = simpledialog.askstring(
        "Set Reminder",
        "What should Thor remind you about?",
        parent=root
    )
    if not message:
        return

    minutes = simpledialog.askinteger(
        "Reminder Time",
        "After how many minutes?",
        minvalue=1,
        maxvalue=10080,
        parent=root
    )
    if not minutes:
        return

    run_background_action(
        lambda: set_reminder(message, minutes_from_now=minutes)
    )


def gui_media():
    value = simpledialog.askstring(
        "Media Control",
        "play / pause / next / previous / volume up / volume down / mute",
        parent=root
    )
    if not value:
        return

    mapping = {
        "play": "play_pause",
        "pause": "play_pause",
        "next": "next",
        "previous": "previous",
        "volume up": "volume_up",
        "volume down": "volume_down",
        "mute": "mute"
    }

    action = mapping.get(value.strip().lower())
    if not action:
        add_message("THOR", "Unknown media command.")
        return

    run_background_action(lambda: media_control(action))


def gui_tools():
    add_message(
        "THOR",
        "SAFE TOOLS\n\n"
        "Google Search • YouTube • Websites • PC Status • "
        "Reminders • Screenshot • Media Controls • Voice AI"
    )


def gui_about():
    add_message(
        "THOR",
        "Mere owner aur creator Sahil Suman hain. "
        "Unhone mujhe develop kiya hai."
    )


# ---------- Sidebar buttons ----------
def nav_button(label, callback):
    return tk.Button(
        sidebar,
        text=label,
        command=callback,
        font=("Segoe UI", 9, "bold"),
        bg="#0d1016",
        fg=TEXT,
        activebackground="#20180b",
        activeforeground=GOLD_BRIGHT,
        relief=tk.FLAT,
        bd=0,
        highlightbackground="#2e2412",
        highlightthickness=1,
        padx=10,
        pady=10,
        cursor="hand2"
    )


nav_button("💬  Chat", lambda: command_entry.focus_set()).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("🌐  Google Search", gui_google_search).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("▶  YouTube Search", gui_youtube_search).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("🖥  PC Status", gui_pc_status).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("⏰  Reminders", gui_reminder).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("📸  Screenshot", lambda: run_gui_action(take_screenshot)).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("▶  Media", gui_media).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("🌍  Website", gui_open_website).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("🛠  Tools", gui_tools).pack(
    fill=tk.X, padx=10, pady=3
)
nav_button("ⓘ  About Thor", gui_about).pack(
    fill=tk.X, padx=10, pady=3
)

# ---------- Bottom command + quick actions ----------
# Keep the command bar and quick-action buttons inside one fixed bottom
# container so they never overlap on different Windows DPI/scaling settings.
bottom = tk.Frame(
    root,
    bg="#050608",
    highlightbackground=BORDER,
    highlightthickness=1
)
bottom.place(relx=0.015, rely=0.795, relwidth=0.97, relheight=0.195)

state_bar = tk.Frame(bottom, bg="#050608")
state_bar.pack(fill=tk.X, padx=8, pady=(4, 2))

tk.Label(
    state_bar, textvariable=state_var,
    bg="#050608", fg=MUTED,
    font=("Segoe UI", 8, "bold")
).pack(side=tk.LEFT)

tk.Label(
    state_bar,
    text="Gemini 3.6 Flash  •  Voice + Web + Safe Tools",
    bg="#050608", fg=MUTED,
    font=("Segoe UI", 8)
).pack(side=tk.RIGHT)

input_outer = tk.Frame(
    bottom,
    bg="#0b0e14",
    highlightbackground=BORDER,
    highlightthickness=1,
    padx=7,
    pady=7
)
input_outer.pack(fill=tk.X, padx=8, pady=(2, 4))

command_entry = tk.Entry(
    input_outer,
    font=("Segoe UI", 12),
    bg=PANEL_2,
    fg=TEXT,
    insertbackground=GOLD_BRIGHT,
    relief=tk.FLAT,
    bd=0
)
command_entry.pack(
    side=tk.LEFT,
    fill=tk.X,
    expand=True,
    ipady=7,
    padx=(3, 7)
)


def send_manual_command():
    value = command_entry.get().strip()
    if not value:
        return

    command_entry.delete(0, tk.END)
    update_state("THINKING", "● PROCESSING REQUEST...")
    queue_command(value, already_checked=False)


def activate_voice_button():
    global conversation_mode, wake_mode
    conversation_mode = True
    wake_mode = False
    update_state("LISTENING", "● THOR ACTIVE  •  SPEAK NOW")


tk.Button(
    input_outer,
    text="🎤",
    command=activate_voice_button,
    font=("Segoe UI Symbol", 11, "bold"),
    bg="#17140e",
    fg=GOLD_BRIGHT,
    activebackground="#2a2110",
    relief=tk.FLAT,
    padx=10,
    pady=7,
    cursor="hand2"
).pack(side=tk.LEFT, padx=2)

tk.Button(
    input_outer,
    text="SEND ➤",
    command=send_manual_command,
    font=("Segoe UI", 9, "bold"),
    bg=GOLD,
    fg="#090704",
    activebackground=GOLD_BRIGHT,
    relief=tk.FLAT,
    padx=16,
    pady=6,
    cursor="hand2"
).pack(side=tk.LEFT, padx=2)

command_entry.bind("<Return>", lambda event: send_manual_command())

# Functional quick buttons are INSIDE bottom, below the input.
quick_bar = tk.Frame(bottom, bg="#050608")
quick_bar.pack(fill=tk.X, padx=8, pady=(0, 4))

quick_buttons = [
    ("🌐 Google", gui_google_search),
    ("▶ YouTube", gui_youtube_search),
    ("🖥 PC Status", gui_pc_status),
    ("⏰ Reminder", gui_reminder),
    ("📸 Screenshot", take_screenshot),
    ("▶ Media", gui_media),
    ("🌍 Website", gui_open_website),
    ("🛠 Tools", gui_tools),
]

for label, callback in quick_buttons:
    tk.Button(
        quick_bar,
        text=label,
        command=callback,
        font=("Segoe UI", 8, "bold"),
        bg="#0d1016",
        fg=TEXT,
        activebackground="#21190b",
        activeforeground=GOLD_BRIGHT,
        relief=tk.FLAT,
        bd=0,
        highlightbackground="#5a431d",
        highlightthickness=1,
        padx=6,
        pady=6,
        cursor="hand2"
    ).pack(side=tk.LEFT, padx=2, expand=True, fill=tk.X)

# ============================================================
# VOICE LISTENER
# ============================================================

recognizer = sr.Recognizer()

# Resolved at startup. Do not rely on the Windows DEFAULT endpoint because
# the current machine exposes several Microphone Array/Realtek endpoints and
# DEFAULT is producing an invalid SpeechRecognition AudioSource.
MIC_DEVICE_INDEX = None


def resolve_mic_device():
    """Choose a real input-capable microphone, preferring Microphone Array."""
    global MIC_DEVICE_INDEX

    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        candidates = []

        try:
            for index in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(index)
                max_input = int(info.get("maxInputChannels", 0) or 0)
                if max_input <= 0:
                    continue

                name = str(info.get("name", ""))
                lname = name.lower()

                # Prefer the laptop's built-in microphone family.
                priority = 0
                if "microphone array" in lname:
                    priority = 0
                elif "microphone" in lname:
                    priority = 1
                elif "realtek" in lname:
                    priority = 2
                else:
                    priority = 3

                candidates.append((priority, index, name, max_input))

            if not candidates:
                print("[MIC] No input-capable PyAudio device found.")
                return None

            candidates.sort(key=lambda x: (x[0], x[1]))
            priority, index, name, max_input = candidates[0]

            print(
                "[MIC] Selected input device:",
                index,
                "|",
                name,
                "| inputs:",
                max_input
            )
            return index

        finally:
            try:
                pa.terminate()
            except Exception:
                pass

    except Exception as exc:
        print(
            "[MIC] Device resolver failed:",
            type(exc).__name__,
            str(exc)
        )
        return None
recognizer.dynamic_energy_threshold = False
recognizer.energy_threshold = 180
recognizer.pause_threshold = 0.55
recognizer.non_speaking_duration = 0.25
recognizer.phrase_threshold = 0.08

wake_mode = True
conversation_mode = False
running = True
last_speech_time = time.time()
command_busy = False

# Prevent two voice callbacks from launching browser/media actions together.
local_action_lock = threading.Lock()
last_local_action = ""
last_local_action_time = 0.0

# Prevent duplicate song launches caused by recognition retries.
last_music_query = ""
last_music_query_time = 0.0

last_command = ""
last_command_time = 0.0


def normalize(text):
    return " ".join(text.lower().strip().split())


def is_duplicate(text):
    global last_command, last_command_time

    now = time.time()
    value = normalize(text)

    # Voice recognition can emit the same utterance more than once,
    # especially while music is playing. Ignore repeats for a short window.
    if value and value == last_command and now - last_command_time < 4.0:
        return True

    last_command = value
    last_command_time = now
    return False


def recognize(source, timeout=None, phrase_time_limit=15):
    try:
        audio = recognizer.listen(
            source,
            timeout=timeout,
            phrase_time_limit=phrase_time_limit
        )

        text = recognizer.recognize_google(
            audio,
            language="en-IN"
        )

        return text.strip()

    except (
        sr.WaitTimeoutError,
        sr.UnknownValueError,
        sr.RequestError
    ):
        return None



def activate_browser_window():
    """Bring an existing Chrome/Brave/Edge window to the foreground."""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        found = {"hwnd": None}

        EnumWindows = user32.EnumWindows
        EnumWindowsProc = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p
        )

        def callback(hwnd, lparam):
            if not user32.IsWindowVisible(hwnd):
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.lower()

            browser_names = [
                "brave", "google chrome", "microsoft edge", "chrome"
            ]

            if any(name in title for name in browser_names):
                found["hwnd"] = hwnd
                return False

            return True

        EnumWindows(EnumWindowsProc(callback), 0)

        hwnd = found["hwnd"]
        if hwnd:
            # Restore if minimized and activate it.
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            return True

    except Exception as exc:
        print("Browser activation error:", type(exc).__name__, str(exc))

    return False


def navigate_current_browser(url):
    """
    Navigate the existing Brave/Chrome/Edge tab.
    Serializes navigation so duplicate voice callbacks cannot create a
    sequence of tabs or overwrite the address bar simultaneously.
    """
    try:
        import pyautogui

        with local_action_lock:
            if not activate_browser_window():
                return False

            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.10)
            pyautogui.write(url, interval=0.001)
            pyautogui.press("enter")
            time.sleep(0.15)
            return True

    except Exception as exc:
        print("Current-tab navigation error:", type(exc).__name__, str(exc))
        return False



def close_browser_target(target=None):
    """
    Close the current site/tab or the requested browser.
    - "site/google/youtube tab band" => Ctrl+W
    - "chrome/brave band" => close that browser process
    """
    target = normalize(target or "")

    if target in ("chrome", "google chrome"):
        try:
            subprocess.run(
                ["taskkill", "/IM", "chrome.exe", "/F"],
                capture_output=True,
                text=True,
                timeout=3
            )
            return "Chrome band kar diya."
        except Exception as exc:
            return f"Chrome close nahi ho saka: {type(exc).__name__}"

    if target == "brave":
        try:
            subprocess.run(
                ["taskkill", "/IM", "brave.exe", "/F"],
                capture_output=True,
                text=True,
                timeout=3
            )
            return "Brave band kar diya."
        except Exception as exc:
            return f"Brave close nahi ho saka: {type(exc).__name__}"

    if target in ("edge", "microsoft edge"):
        try:
            subprocess.run(
                ["taskkill", "/IM", "msedge.exe", "/F"],
                capture_output=True,
                text=True,
                timeout=3
            )
            return "Edge band kar diya."
        except Exception as exc:
            return f"Edge close nahi ho saka: {type(exc).__name__}"

    return browser_control("close_tab")


def browser_control(action):
    """Control the currently focused Chrome/Brave/Edge browser."""
    try:
        import pyautogui

        hotkeys = {
            "new_tab": ("ctrl", "t"),
            "close_tab": ("ctrl", "w"),
            "reopen_tab": ("ctrl", "shift", "t"),
            "refresh": ("ctrl", "r"),
            "back": ("alt", "left"),
            "forward": ("alt", "right"),
            "focus_address": ("ctrl", "l"),
        }

        keys = hotkeys.get(action)
        if not keys:
            return "Unsupported browser action."

        with local_action_lock:
            if not activate_browser_window():
                return "Please open Chrome or Brave first."
            pyautogui.hotkey(*keys)
        return {
            "new_tab": "New browser tab opened.",
            "close_tab": "Browser tab closed.",
            "reopen_tab": "Last closed tab reopened.",
            "refresh": "Browser refreshed.",
            "back": "Went back.",
            "forward": "Went forward.",
            "focus_address": "Address bar focused.",
        }[action]

    except Exception as exc:
        print("Browser control error:", type(exc).__name__, str(exc))
        return "I couldn't control the browser."


def browser_search(query):
    """Search in the default browser without depending on Gemini."""
    query = query.strip()
    if not query:
        return "Sir, what should I search?"

    url = "https://www.google.com/search?q=" + quote_plus(query)
    navigate_current_browser(url)
    return f"Searching Google for {query}."


def local_action(text):
    """
    Handle obvious computer/media actions locally.
    This guarantees actions such as 'play Saiyaara' or 'YouTube kholo'
    work even if Gemini decides to answer conversationally instead of
    emitting a function call.
    """
    global pending_music_request

    q = normalize(text)

    # If Thor previously asked "kaunsa song bajau?", the next utterance
    # is the song query itself. Do NOT send it to Gemini.
    if pending_music_request:
        pending_music_request = False
        query = q
        for wake_word in ["hello thor", "hey thor", "hi thor", "thor"]:
            query = query.replace(wake_word, " ")
        query = " ".join(query.split()).strip(" .,!?:;-")

        # Ignore empty filler; keep the request pending so the user can
        # provide the song name again.
        if query:
            return search_youtube(query)
        pending_music_request = True
        return "Sir, song ka naam bataiye."

    # ------------------------------------------------------------
    # Browser commands are local and immediate.
    # These work while Chrome/Brave is focused.
    # ------------------------------------------------------------
    browser_actions = [
        (["new tab", "new browser tab", "naya tab", "nayi tab"], "new_tab"),
        (["close tab", "close this tab", "tab band", "tab band karo"], "close_tab"),
        (["reopen tab", "reopen closed tab", "tab wapas kholo"], "reopen_tab"),
        (["refresh", "refresh page", "page refresh", "page reload"], "refresh"),
        (["go back", "back karo", "piche jao", "peeche jao"], "back"),
        (["go forward", "forward karo", "aage jao"], "forward"),
        (["address bar", "url bar", "address bar kholo"], "focus_address"),
    ]

    for phrases, action in browser_actions:
        if any(phrase in q for phrase in phrases):
            return browser_control(action)

    # Common deterministic commands: keep these out of Gemini.
    if any(x in q for x in [
        "pc status", "pc ka status", "computer status",
        "system status", "system ka status"
    ]):
        try:
            return get_pc_status()
        except Exception as exc:
            return f"I couldn't read the PC status: {type(exc).__name__}"

    if any(x in q for x in [
        "screenshot lo", "screenshot lelo", "take screenshot",
        "screenshot lena", "screen shot lo", "screen shot lena"
    ]):
        try:
            return take_screenshot()
        except Exception as exc:
            return f"I couldn't take the screenshot: {type(exc).__name__}"

    # Open browser directly.
    if any(x in q for x in ["open chrome", "chrome kholo", "chrome khol", "chrome open"]):
        return open_website("chrome", preferred_browser="chrome")
    if any(x in q for x in ["open brave", "brave kholo", "brave khol", "brave open"]):
        return open_website("brave", preferred_browser="brave")

    # Google/web search + answer: if the user asks to search AND tell/explain,
    # use THOR's real-time web-answer path. Do not stop after opening Google.
    answer_search_markers = [
        "search karke batao", "search karke btao",
        "google par search karke batao", "google par search karke btao",
        "google pe search karke batao", "google pe search karke btao",
        "google me search karke batao", "google mein search karke batao",
        "search karke bata", "google se pata karo", "google se batao",
        "online check karke batao", "web se check karke batao",
    ]
    if any(marker in q for marker in answer_search_markers):
        query = q
        for phrase in [
            "google par", "google pe", "google me", "google mein",
            "search karke batao", "search karke btao",
            "search karke bata", "google se pata karo",
            "google se batao", "online check karke batao",
            "web se check karke batao", "search", "karke", "batao", "btao"
        ]:
            query = query.replace(phrase, " ")
        query = " ".join(query.split()).strip(" .,!?:;-")

        if query:
            answer = gemini_web_turn(query)
            if answer:
                return answer

            # If Gemini web is unavailable, still perform the requested
            # Google search instead of pretending the answer was obtained.
            browser_search(query)
            return f"Google par {query} search kar diya hai, lekin web answer service abhi unavailable hai."

    # Direct web search without waiting for Gemini.
    search_markers = [
        "google par", "google pe", "google me", "google mein",
        "search karo", "search kar", "search for", "web par search",
        "internet par search", "internet pe search"
    ]
    if any(marker in q for marker in search_markers):
        query = q
        for word in [
            "google", "par", "pe", "me", "mein", "search", "karo", "kar",
            "for", "web", "internet", "online"
        ]:
            query = query.replace(word, " ")
        query = " ".join(query.split())
        if query:
            return browser_search(query)

    # Natural-language reminders. Execute locally.
    # Supports:
    #   "remind me in 10 seconds"
    #   "10 seconds baad mujhe yaad dilana"
    #   "5 minute baad assignment yaad dilana"
    #   "after 2 hours remind me..."
    reminder_match = re.search(
        r"(?:in|after|baad\s+me|baad)?\s*"
        r"(\d+)\s*"
        r"(seconds?|secs?|sec|"
        r"minutes?|mins?|min|"
        r"hours?|hrs?|hr)"
        r"(?:\s*(?:baad|later))?",
        q
    )

    reminder_words = [
        "remind me", "reminder", "remind",
        "yaad dilana", "yaad dila dena", "yaad dilao",
        "yaad dila", "yaad rakhna", "yaad rakh dena"
    ]

    if reminder_match and any(word in q for word in reminder_words):
        amount = int(reminder_match.group(1))
        unit = reminder_match.group(2).lower()

        # Extract the user's actual reminder message.
        message = q
        for phrase in reminder_words:
            message = message.replace(phrase, " ")

        message = re.sub(
            r"(?:in|after|baad\s+me)?\s*\d+\s*"
            r"(?:seconds?|secs?|sec|minutes?|mins?|min|hours?|hrs?|hr)"
            r"(?:\s*(?:baad|later))?",
            " ",
            message
        )

        message = re.sub(
            r"\b(?:mujhe|please|plz|ki|ke|ka|karo|karna|dena|do|me|my)\b",
            " ",
            message
        )
        message = " ".join(message.split()).strip(" .,!?:;-")

        if not message:
            message = "This is your reminder."

        if unit.startswith(("second", "sec")):
            due = datetime.now() + timedelta(seconds=amount)
            item = {
                "message": message,
                "due": due.isoformat(timespec="seconds"),
                "done": False
            }
            reminders.append(item)
            save_json(REMINDERS_FILE, reminders)
            return f"Reminder set for {amount} seconds: {message}"

        if unit.startswith(("hour", "hr")):
            minutes = amount * 60
        else:
            minutes = amount

        return set_reminder(
            message,
            minutes_from_now=minutes
        )

    # Explicit Google search requests.
    if (
        ("google" in q or "search" in q or "internet" in q)
        and any(
            x in q
            for x in [
                "search", "search karo", "search kar",
                "google par", "google pe", "internet par",
                "internet pe", "check online"
            ]
        )
    ):
        query = q
        for word in [
            "google", "search", "search karo", "search kar",
            "google par", "google pe", "internet par",
            "internet pe", "check online", "karo", "kar"
        ]:
            query = query.replace(word, " ")

        query = " ".join(query.split())

        if query:
            if navigate_current_browser(
                "https://www.google.com/search?q=" + quote_plus(query)
            ):
                return f"Searching Google for {query} in the current tab."
            return "Please open Chrome or Brave first."

    # ------------------------------------------------------------
    # Close browser/site commands.
    # ------------------------------------------------------------
    if any(x in q for x in [
        "chrome band karo", "chrome band kar", "chrome close",
        "google chrome band", "google chrome close"
    ]):
        return close_browser_target("chrome")

    if any(x in q for x in [
        "brave band karo", "brave band kar", "brave close"
    ]):
        return close_browser_target("brave")

    if any(x in q for x in [
        "edge band karo", "edge band kar", "edge close"
    ]):
        return close_browser_target("edge")

    if any(x in q for x in [
        "site band karo", "site band kar", "website band karo",
        "website band kar", "page band karo", "page band kar",
        "current site band", "current website band",
        "tab band karo", "tab band kar", "close site", "close website"
    ]):
        return close_browser_target()

    for site_name in SITE_MAP:
        if site_name in q and any(
            x in q for x in [
                "band karo", "band kar", "close", "close karo",
                "close kar", "hata do"
            ]
        ):
            return close_browser_target()

    # Open arbitrary websites/domains, e.g. "open wikipedia.org",
    # "website example.com kholo", or "site github.com kholo".
    arbitrary_site = re.search(
        r"(?:open|khol(?:o)?|launch|website|site)\s+"
        r"((?:https?://)?(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/[^ ]*)?)",
        q,
        re.IGNORECASE,
    )
    if arbitrary_site:
        site = arbitrary_site.group(1).strip()
        return open_website(site)

    if any(x in q for x in [
        "browser kholo", "browser khol", "open browser",
        "browser open", "internet kholo", "internet khol"
    ]):
        return open_website("google")

    site_aliases = {
        "google": ["google", "google kholo", "google khol"],
        "youtube": ["youtube", "you tube", "youtube kholo", "youtube khol"],
        "gmail": ["gmail", "mail kholo", "gmail kholo"],
        "whatsapp": ["whatsapp", "whatsapp kholo", "whatsapp khol"],
        "instagram": ["instagram", "instagram kholo", "instagram khol"],
        "github": ["github", "github kholo", "github khol"],
        "linkedin": ["linkedin", "linkedin kholo", "linkedin khol"],
        "chatgpt": ["chatgpt", "chat gpt", "chatgpt kholo"],
        "gemini": ["gemini", "gemini kholo"],
    }

    for site_name, aliases in site_aliases.items():
        if any(alias in q for alias in aliases) and any(
            word in q for word in [
                "open", "khol", "kholo", "launch", "website", "site",
                "browser", "jao"
            ]
        ):
            return open_website(site_name)

    # Open known websites.
    for site in SITE_MAP:
        if (
            site in q
            and any(
                word in q
                for word in ["open", "khol", "kholo", "launch", "website", "site"]
            )
        ):
            return open_website(site)

    # Explicit YouTube search.
    if "youtube" in q and any(
        x in q for x in ["search", "find", "dhundo", "dhoondo", "khojo"]
    ):
        query = q
        for word in [
            "youtube", "search", "find", "dhundo", "dhoondo", "khojo",
            "par", "pe", "on"
        ]:
            query = query.replace(word, " ")
        query = " ".join(query.split())

        if query:
            return search_youtube(query)
        return open_website("youtube")

    # Natural music commands. These run locally and bypass Gemini.
    music_patterns = [
        "play ", "play song", "play the song", "play music",
        "play karo", "play kar",
        "bajao", "baja do", "bajado", "baja",
        "gaana chalao", "gaana chala do",
        "gana chalao", "gana chala do",
        "song bajao", "music bajao",
        "gaana bajao", "gana bajao",
        "gaana baja do", "gana baja do",
        "song chalao", "song chala do",
    ]

    if any(pattern in q for pattern in music_patterns):
        query = q

        for word in [
            "play", "the", "song", "songs", "gaana", "gana", "music",
            "bajao", "baja", "baja do", "bajado",
            "chalao", "chala", "chala do", "karo", "kar", "please"
        ]:
            query = query.replace(word, " ")

        query = " ".join(query.split())

        if query:
            pending_music_request = False
            return search_youtube(query)

        pending_music_request = True
        return "Sir, kaunsa song bajau?"

    # ------------------------------------------------------------
    # Robust media commands.
    # These are handled locally BEFORE Gemini, so "gaana pause karo"
    # never waits for an AI response.
    # ------------------------------------------------------------
    media_phrases = [
        ([
            "pause", "pause karo", "pause kar", "gaana pause",
            "gana pause", "song pause", "video pause",
            "pause the song", "pause music"
        ], "play_pause"),

        ([
            "resume", "resume karo", "resume kar", "continue",
            "continue karo", "gaana resume", "gana resume",
            "song resume", "play karo", "play kar"
        ], "play_pause"),

        ([
            "next", "next song", "next video", "next track",
            "agla gana", "agla gaana", "agla song", "agla track"
        ], "next"),

        ([
            "previous", "previous song", "previous video",
            "previous track", "pichla gana", "pichla gaana",
            "pichla song", "pichla track"
        ], "previous"),

        ([
            "stop music", "stop song", "stop video",
            "music stop", "gaana stop", "gana stop",
            "song stop", "video stop", "media stop",
            "gaana band", "gana band", "song band",
            "music band", "video band", "band karo"
        ], "stop"),

        ([
            "volume up", "volume badhao", "volume badao",
            "awaz badhao", "awaaz badhao"
        ], "volume_up"),

        ([
            "volume down", "volume kam", "awaz kam", "awaaz kam"
        ], "volume_down"),

        (["mute", "mute karo", "mute kar"], "mute"),
    ]

    # Remove wake-name words so "Thor, gaana pause karo" is still matched.
    media_q = q
    for wake_word in ["hello thor", "hey thor", "hi thor", "thor"]:
        media_q = media_q.replace(wake_word, " ")
    media_q = " ".join(media_q.split())

    for phrases, action in media_phrases:
        if any(
            media_q == phrase
            or media_q.endswith(" " + phrase)
            or media_q.startswith(phrase + " ")
            or phrase in media_q
            for phrase in phrases
        ):
            return media_control(action)

    return None


def process_user_text(text, already_checked=False):
    if not text:
        return

    # Voice commands are duplicate-checked at the microphone boundary.
    # GUI/text commands are checked here.
    if not already_checked and is_duplicate(text):
        return

    add_message("YOU", text)
    update_state("THINKING", "● THOR IS THINKING...")
    remember_user_message(text)

    # Fixed creator identity works even if Gemini is temporarily unavailable.
    identity = creator_answer(text)

    if identity:
        answer = identity
    else:
        # Obvious PC/media/web actions are deterministic and do not depend
        # on the model deciding to call a function.
        action_result = local_action(text)

        if action_result:
            answer = action_result
        elif needs_web_search(text):
            answer = gemini_web_turn(text)
            if not answer:
                answer = gemini_turn(text)
        else:
            answer = gemini_turn(text)

    if not answer:
        # Keep the real failure in the terminal while giving the user a
        # useful, short GUI/TTS message.
        answer = (
            "Sir, Gemini is not responding. "
            "Please check the THOR terminal for the exact error."
        )

    add_message("THOR", answer)
    update_state("SPEAKING", "● THOR IS SPEAKING...")
    remember_thor_message(answer)
    speak(answer)
    update_state("READY", "● ONLINE  •  READY FOR NEXT COMMAND")





def audio_is_speech(audio):
    """Advisory VAD; never reject captured audio before STT."""
    try:
        import math
        import struct

        raw = audio.get_raw_data()
        if not raw:
            return False

        width = int(getattr(audio, "sample_width", 2))
        if width == 1:
            samples = [b - 128 for b in raw]
        elif width == 2:
            usable = len(raw) - len(raw) % 2
            if usable <= 0:
                return False
            samples = struct.unpack("<" + "h" * (usable // 2), raw[:usable])
        elif width == 4:
            usable = len(raw) - len(raw) % 4
            if usable <= 0:
                return False
            samples = struct.unpack("<" + "i" * (usable // 4), raw[:usable])
        else:
            return True

        if not samples:
            return False

        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        print(
            "[MIC AUDIO] RMS =", round(rms, 1),
            "| STT threshold =",
            round(float(getattr(recognizer, "energy_threshold", 300)), 1)
        )
        return rms > 80.0
    except Exception as exc:
        print("[VAD] diagnostic error:", type(exc).__name__, str(exc))
        return True


def list_microphones():
    try:
        names = sr.Microphone.list_microphone_names()
        print("[MIC DEVICES] Found", len(names), "device(s)")
        for index, name in enumerate(names):
            print(f"  [{index}] {name}")
        return names
    except Exception as exc:
        print("[MIC DEVICES] ERROR:", type(exc).__name__, str(exc))
        return []



def looks_like_command(text):
    """Reject obvious accidental/noise transcripts."""
    q = normalize(text)

    if not q or len(q) < 2:
        return False

    # STT sometimes returns punctuation/noise fragments.
    if not re.search(r"[a-zA-Z0-9\u0900-\u097F]", q):
        return False

    # Very short accidental transcripts are not useful commands.
    if len(q.split()) == 1 and len(q) < 3:
        return False

    return True


def run_local_command_async(heard, action_result):
    """
    Execute local browser/media work off the microphone thread.
    The microphone remains free while YouTube/browser operations run.
    """
    def worker():
        try:
            print("[LOCAL WORKER] START:", heard)
            add_message("YOU", heard)
            add_message("THOR", action_result)

            # local_action has already completed for the routing decision.
            # This worker is primarily responsible for UI/status updates.
            update_state(
                "READY",
                "● ONLINE  •  LOCAL COMMAND COMPLETED"
            )
            print("[LOCAL WORKER] DONE:", heard)
        except Exception as exc:
            print(
                "[LOCAL WORKER] ERROR:",
                type(exc).__name__,
                str(exc)
            )

    threading.Thread(target=worker, daemon=True).start()



def is_local_command_intent(text):
    """
    Decide whether a command can be handled locally WITHOUT executing it.
    This lets the microphone thread remain free while browser/media work
    happens in a worker.
    """
    q = normalize(text)

    local_phrases = [
        # Media
        "pause", "resume", "continue", "next song", "next video",
        "previous song", "previous video", "agla gana", "agla gaana",
        "pichla gana", "pichla gaana", "stop music", "stop song",
        "stop video", "gaana band", "gana band", "music band",
        "volume up", "volume down", "volume badhao", "volume kam",
        "awaz badhao", "awaaz badhao", "mute",
        # Music/search
        "play ", "play song", "play music", "bajao", "baja do",
        "gaana chalao", "gaana chala do", "gana chalao",
        "gana chala do", "youtube search", "youtube kholo",
        "youtube khol", "youtube par", "youtube pe",
        "browser kholo", "browser khol", "open browser",
        "chrome open", "brave open", "google kholo", "youtube kholo",
        "gmail kholo", "whatsapp kholo", "instagram kholo",
        "github kholo", "linkedin kholo", "chatgpt kholo", "gemini kholo",
        "site band", "website band", "page band",
        "chrome band", "brave band", "edge band",
        # Browser
        "new tab", "naya tab", "nayi tab", "close tab",
        "tab band", "reopen tab", "refresh", "page refresh",
        "go back", "back karo", "piche jao", "peeche jao",
        "go forward", "forward karo", "aage jao",
        "address bar", "url bar", "chrome kholo", "brave kholo",
        "chrome khol", "brave khol", "open chrome", "open brave",
        "launch chrome", "launch brave", "open browser",
        # Web search / sites
        "google par", "google pe", "google me", "google mein",
        "on google", "google search", "search on google",
        "search karo", "search kar", "search for",
        "web par search", "internet par search", "internet pe search",
        "whatsapp kholo", "whatsapp khol",
        # PC
        "pc status", "pc ka status", "computer status",
        "system status", "system ka status",
        "screenshot", "screen shot",
        # Reminder
        "remind me", "reminder", "yaad dilana", "yaad dila",
    ]

    return any(phrase in q for phrase in local_phrases)


def voice_loop():
    """
    Stable single-thread microphone loop.

    IMPORTANT:
    This loop owns its Recognizer instance. The diagnostic microphone test
    must never share the global recognizer with this long-running listener.
    """
    global conversation_mode, wake_mode, running, command_busy

    # Thread-local recognizer: prevents AudioSource/recognizer races.
    voice_recognizer = sr.Recognizer()
    voice_recognizer.energy_threshold = 180
    voice_recognizer.dynamic_energy_threshold = False
    voice_recognizer.pause_threshold = 0.55
    voice_recognizer.non_speaking_duration = 0.25
    voice_recognizer.phrase_threshold = 0.08

    CONVERSATION_IDLE_TIMEOUT = 90
    last_speech_time = time.time()

    try:
        update_state(
            "LISTENING",
            "● OPENING MICROPHONE..."
        )

        # ONE microphone object, ONE context manager, ONE voice thread.
        mic_kwargs = {}
        if MIC_DEVICE_INDEX is not None:
            mic_kwargs["device_index"] = MIC_DEVICE_INDEX

        print(
            "[MIC] Opening device:",
            MIC_DEVICE_INDEX if MIC_DEVICE_INDEX is not None else "DEFAULT"
        )
        with sr.Microphone(**mic_kwargs) as source:
            stream = getattr(source, "stream", None)
            print(
                "[MIC] Device opened successfully:",
                getattr(source, "device_index", "default"),
                "| stream:",
                "READY" if stream is not None else "MISSING"
            )

            if stream is None:
                raise RuntimeError(
                    "Microphone AudioSource stream is missing; "
                    "selected input device could not be opened"
                )
            # Keep a fixed threshold. Ambient calibration is intentionally
            # disabled because some Windows Smart Sound drivers can invalidate
            # SpeechRecognition's AudioSource state during startup.
            voice_recognizer.energy_threshold = 180
            voice_recognizer.dynamic_energy_threshold = False

            print(
                "[MIC] energy_threshold =",
                round(voice_recognizer.energy_threshold, 1)
            )
            print(
                "[MIC] pause_threshold =",
                voice_recognizer.pause_threshold
            )

            update_state(
                "READY",
                "● ONLINE  •  SAY 'HELLO THOR' TO START"
            )

            while running:
                if speaking_event.is_set():
                    time.sleep(0.08)
                    continue

                try:
                    # Short timeout keeps the loop responsive while waiting.
                    audio = voice_recognizer.listen(
                        source,
                        timeout=1,
                        phrase_time_limit=7
                    )
                except sr.WaitTimeoutError:
                    if (
                        conversation_mode
                        and time.time() - last_speech_time
                        > CONVERSATION_IDLE_TIMEOUT
                    ):
                        conversation_mode = False
                        wake_mode = True
                        update_state(
                            "READY",
                            "● ONLINE  •  SAY 'HELLO THOR' TO START"
                        )
                    continue
                except Exception as exc:
                    print(
                        "[MIC] listen error:",
                        type(exc).__name__,
                        str(exc)
                    )
                    # Leave this microphone context and reopen it. This keeps
                    # a single owner while recovering from transient PortAudio
                    # / SpeechRecognition failures.
                    time.sleep(0.35)
                    break

                # VAD is diagnostic only. Every captured phrase goes to STT.
                speech_hint = audio_is_speech(audio)
                print("[MIC] Captured audio; VAD hint =", speech_hint)

                try:
                    heard = voice_recognizer.recognize_google(
                        audio,
                        language="en-IN"
                    ).strip()
                except sr.UnknownValueError:
                    # Speech was detected but Google couldn't understand it.
                    continue
                except sr.RequestError as exc:
                    print(
                        "[MIC] Google speech request error:",
                        str(exc)
                    )
                    time.sleep(0.5)
                    continue
                except Exception as exc:
                    print(
                        "[MIC] recognition error:",
                        type(exc).__name__,
                        str(exc)
                    )
                    continue

                if not heard or not looks_like_command(heard):
                    continue

                print("[VOICE HEARD]", heard)

                normalized = normalize(heard)
                last_speech_time = time.time()

                # Ignore duplicate STT results before doing anything.
                if is_duplicate(heard):
                    print("[VOICE] duplicate ignored:", heard)
                    continue

                # --------------------------------------------------
                # PENDING MUSIC FOLLOW-UP
                # --------------------------------------------------
                # "Play music" -> Thor asks for song -> the next utterance
                # is the song name even if wake mode is currently sleeping.
                if pending_music_request:
                    print("[ROUTER] MUSIC FOLLOW-UP:", heard)

                    def music_followup_worker(command_text=heard):
                        try:
                            # The dispatcher may call a handler that itself
                            # acquires local_action_lock (for example browser
                            # navigation). Do not hold the same non-reentrant
                            # lock across the dispatcher.
                            # V42 LOCAL-FIRST: deterministic local commands run before AI fallback.
                            result = local_action(command_text)

                            if result:
                                global conversation_mode, wake_mode
                                conversation_mode = True
                                wake_mode = False
                                add_message("YOU", command_text)
                                add_message("THOR", result)
                                update_state(
                                    "SPEAKING",
                                    "● THOR IS SPEAKING..."
                                )
                                speak(result)
                                update_state(
                                    "LISTENING",
                                    "● THOR ACTIVE  •  LISTENING"
                                )
                        except Exception as exc:
                            print(
                                "[MUSIC FOLLOW-UP ERROR]:",
                                type(exc).__name__,
                                str(exc)
                            )

                    threading.Thread(
                        target=music_followup_worker,
                        daemon=True
                    ).start()
                    continue

                # --------------------------------------------------
                # LOCAL COMMANDS.
                # Prevent random background speech from launching PC/media
                # actions while THOR is asleep. A local command is accepted
                # when conversation mode is active, the wake word is present,
                # or a previous "play music" prompt is awaiting the song name.
                # --------------------------------------------------
                wake_present = any(
                    phrase in normalized
                    for phrase in WAKE_PHRASES
                ) or (
                    "hellothor" in normalized.replace(" ", "")
                    or "heythor" in normalized.replace(" ", "")
                    or "hithor" in normalized.replace(" ", "")
                )

                local_allowed = (
                    conversation_mode
                    or wake_present
                    or pending_music_request
                )

                if local_allowed and is_local_command_intent(heard):
                    print("[ROUTER] LOCAL COMMAND:", heard)

                    def local_worker(command_text=heard):
                        try:
                            # The dispatcher may call a handler that itself
                            # acquires local_action_lock (for example browser
                            # navigation). Do not hold the same non-reentrant
                            # lock across the dispatcher.
                            result = local_action(command_text)

                            if result:
                                global conversation_mode, wake_mode
                                conversation_mode = True
                                wake_mode = False

                                add_message("YOU", command_text)
                                add_message("THOR", result)
                                update_state(
                                    "SPEAKING",
                                    "● THOR IS SPEAKING..."
                                )
                                speak(result)
                                update_state(
                                    "LISTENING",
                                    "● THOR ACTIVE  •  LISTENING"
                                )
                            else:
                                # It was classified as local, but no exact
                                # deterministic handler matched. Fall back
                                # to Gemini instead of silently doing nothing.
                                print(
                                    "[ROUTER] Local classifier had no handler:",
                                    command_text
                                )
                                queue_command(command_text, already_checked=True)

                        except Exception as exc:
                            print(
                                "[LOCAL WORKER] ERROR:",
                                type(exc).__name__,
                                str(exc)
                            )
                            # Safe AI fallback for an unhandled local command.
                            queue_command(command_text, already_checked=True)

                    threading.Thread(
                        target=local_worker,
                        daemon=True
                    ).start()

                    continue

                # --------------------------------------------------
                # WAKE MODE
                # --------------------------------------------------
                if not conversation_mode:
                    wake = next(
                        (
                            phrase for phrase in WAKE_PHRASES
                            if phrase in normalized
                        ),
                        None
                    )

                    if not wake:
                        compact = normalized.replace(" ", "")
                        if (
                            "hellothor" in compact
                            or "heythor" in compact
                            or "hithor" in compact
                        ):
                            wake = "hello thor"
                        else:
                            print("[VOICE] Ignored:", heard)
                            continue

                    remainder = normalized.replace(
                        wake, "", 1
                    ).strip(" ,.!?")

                    conversation_mode = True
                    wake_mode = False

                    update_state(
                        "LISTENING",
                        "● THOR ACTIVE  •  NO WAKE WORD NEEDED"
                    )

                    if remainder:
                        queue_command(remainder, already_checked=True)
                    else:
                        threading.Thread(
                            target=speak,
                            args=("Yes Sahil sir?",),
                            daemon=True
                        ).start()

                    continue

                # --------------------------------------------------
                # ACTIVE CONVERSATION
                # --------------------------------------------------
                if any(
                    x in normalized
                    for x in [
                        "stop listening",
                        "go to sleep",
                        "sleep now",
                        "bas ab",
                        "conversation end",
                        "thor sleep",
                        "thor stop"
                    ]
                ):
                    conversation_mode = False
                    wake_mode = True

                    update_state(
                        "READY",
                        "● ONLINE  •  SAY 'HELLO THOR' TO START"
                    )

                    threading.Thread(
                        target=speak,
                        args=("Okay Sahil sir. I am going to sleep mode.",),
                        daemon=True
                    ).start()
                    continue

                # Never discard an AI command just because another command
                # is running. queue_command() serializes requests.
                print("[ROUTER] AI COMMAND:", heard)
                queue_command(heard, already_checked=True)

    except Exception as exc:
        print(
            "[MIC] Voice loop fatal error:",
            type(exc).__name__,
            str(exc)
        )
        update_state(
            "ERROR",
            "● MICROPHONE RETRYING..."
        )
        time.sleep(1.0)
        if running:
            voice_loop()



def ai_command_worker():
    """Single serialized AI worker; commands are queued, never silently dropped."""
    global command_busy

    while running:
        try:
            text = ai_command_queue.get(timeout=0.5)
        except queue.Empty:
            continue

        command_busy = True
        try:
            process_user_text(text, already_checked=True)
        except Exception as exc:
            print(
                "[AI WORKER ERROR]:",
                type(exc).__name__,
                str(exc)
            )
            add_message(
                "THOR",
                "Sir, I hit an internal error while processing that command."
            )
            speak("Sir, I hit an internal error while processing that command.")
        finally:
            command_busy = False
            ai_command_queue.task_done()

            if conversation_mode:
                update_state(
                    "LISTENING",
                    "● THOR ACTIVE  •  LISTENING"
                )
            else:
                update_state(
                    "READY",
                    "● ONLINE  •  SAY 'HELLO THOR' TO START"
                )


def queue_command(text, already_checked=False):
    if not text:
        return False

    command = text.strip()

    # Voice input is already duplicate-checked at the microphone boundary.
    # Manual/GUI input still gets the normal duplicate filter.
    if not already_checked and is_duplicate(command):
        print("[QUEUE] duplicate ignored:", command)
        return False

    try:
        ai_command_queue.put_nowait(command)
        print("[QUEUE] AI COMMAND:", command)
        update_state(
            "THINKING",
            "● THOR IS PROCESSING..."
        )
        return True
    except queue.Full:
        print("[QUEUE] full; command rejected:", command)
        add_message(
            "THOR",
            "Sir, I am still processing previous requests. Please try again."
        )
        speak(
            "Sir, I am still processing previous requests. Please try again."
        )
        return False


def start_ai_worker():
    threading.Thread(
        target=ai_command_worker,
        daemon=True,
        name="Thor-AI-Worker"
    ).start()



def verify_tts_release_path():
    import inspect

    source = inspect.getsource(speak)
    set_pos = source.find("speaking_event.set()")
    clear_pos = source.find("speaking_event.clear()")
    finally_pos = source.find("finally:")

    return (
        set_pos >= 0
        and finally_pos > set_pos
        and clear_pos > finally_pos
    )


def run_thor_self_test():
    """
    Offline startup checks for the command pipeline.
    No microphone, browser, Gemini, or media action is touched.
    """
    checks = []

    # Critical V29 regression: successful SAPI speech must release the mic.
    checks.append(verify_tts_release_path())
    import inspect
    import inspect

    # 1. Core functions exist.
    required = [
        "voice_loop",
        "queue_command",
        "process_user_text",
        "local_action",
        "search_youtube",
        "media_control",
        "navigate_current_browser",
        "is_duplicate",
    ]
    checks.append(all(name in globals() for name in required))

    # 2. process_user_text accepts the already_checked flag.
    try:
        import inspect
        checks.append(
            "already_checked"
            in inspect.signature(process_user_text).parameters
        )
    except Exception:
        checks.append(False)

    # 3. queue_command must pass already_checked=True.
    try:
        import inspect
        checks.append(
            "process_user_text(text, already_checked=True)"
            in inspect.getsource(queue_command)
        )
    except Exception:
        checks.append(False)

    # 4. Stable microphone architecture must use listen(), not a second
    # background listener/context manager.
    try:
        import inspect
        source = inspect.getsource(voice_loop)
        checks.append(
            "recognizer.listen(" in source
            and "listen_in_background(" not in source
        )
    except Exception:
        checks.append(False)

    # 5. Noise/VAD gate and intent-only local routing must exist.
    checks.append(
        "audio_is_speech(" in globals()
        and "looks_like_command(" in globals()
        and "is_local_command_intent(" in globals()
    )

    # 6. Music follow-up state must exist so "play music" -> song name
    # stays local instead of falling through to Gemini.
    checks.append(
        "pending_music_request" in globals()
        and "if pending_music_request:" in inspect.getsource(local_action)
    )

    # 7. Gemini diagnostic path exists.
    checks.append(
        "gemini_diagnostic" in globals()
        and "last_gemini_error" in globals()
    )

    # 8. Gemini bootstrap must not block microphone startup.
    try:
        import inspect
        bootstrap_source = inspect.getsource(start_gemini_bootstrap)
        checks.append(
            "threading.Thread" in bootstrap_source
            and "initialize_gemini()" in bootstrap_source
        )
    except Exception:
        checks.append(False)

    # 9. Voice loop must classify local commands before executing local_action.
    try:
        import inspect
        source = inspect.getsource(voice_loop)
        checks.append(
            "is_local_command_intent(heard)" in source
            and "local_action(command_text)" in source
        )
    except Exception:
        checks.append(False)

    # 10. AI commands must be queued, not silently discarded while busy.
    checks.append(
        "ai_command_queue" in globals()
        and "ai_command_worker" in globals()
        and "put_nowait" in inspect.getsource(queue_command)
    )

    # 11. Tkinter updates must be marshalled to the main thread.
    checks.append(
        "threading.current_thread() is not threading.main_thread()"
        in inspect.getsource(add_message)
        and "root.after(0" in inspect.getsource(update_state)
    )

    # 12. Music follow-up must be handled before wake-mode filtering.
    try:
        import inspect
        source = inspect.getsource(voice_loop)
        checks.append(
            "if pending_music_request:" in source
            and source.index("if pending_music_request:")
            < source.index("# WAKE MODE")
        )
    except Exception:
        checks.append(False)

    # 13. Local commands must produce spoken feedback.
    try:
        import inspect
        source = inspect.getsource(voice_loop)
        checks.append(
            "speak(result)" in source
        )
    except Exception:
        checks.append(False)

    # 14. Python 3.13 VAD must not depend on removed stdlib audioop.
    try:
        import inspect
        checks.append(
            "import audioop" not in inspect.getsource(audio_is_speech)
            and "struct.unpack" in inspect.getsource(audio_is_speech)
        )
    except Exception:
        checks.append(False)

    # 15. AI worker must not silently discard commands while busy.
    try:
        import inspect
        checks.append(
            "ignoring another AI command" not in inspect.getsource(voice_loop)
            and "put_nowait" in inspect.getsource(queue_command)
        )
    except Exception:
        checks.append(False)

    # 16. TTS feedback and self-hearing protection exist.
    checks.append(
        "speaking_event" in globals()
        and "speaking_event.set()" in inspect.getsource(speak)
        and "speaking_event.clear()" in inspect.getsource(speak)
    )

    passed = sum(bool(x) for x in checks)
    total = len(checks)

    print(f"[SELF-TEST] THOR command pipeline: {passed}/{total} checks passed")

    if passed != total:
        print("[SELF-TEST] WARNING: inspect the startup diagnostics.")
    else:
        print("[SELF-TEST] PASS: voice -> router -> command worker path is valid.")


def start_gemini_bootstrap():
    """Initialize/probe Gemini without blocking the microphone or GUI."""
    def worker():
        ok, init_message = initialize_gemini()

        if ok:
            print("[BOOT] Gemini online.")
            add_message(
                "THOR",
                "Gemini 3.6 is online. Voice, web and safe PC tools are ready."
            )
            update_state(
                "LISTENING" if conversation_mode else "READY",
                "● THOR ACTIVE  •  LISTENING"
                if conversation_mode
                else "● ONLINE  •  SAY 'HELLO THOR' TO START"
            )
        else:
            print("[BOOT] Gemini unavailable:", init_message)
            add_message(
                "THOR",
                "Gemini is unavailable right now. Local voice, music, browser, PC and reminder tools remain available."
            )
            update_state(
                "LISTENING" if conversation_mode else "READY",
                "● LOCAL TOOLS ONLINE  •  GEMINI OFFLINE"
            )

    threading.Thread(
        target=worker,
        daemon=True,
        name="Thor-Gemini-Boot"
    ).start()


def start_voice_health_monitor():
    """Restart the voice listener only after a real fatal exit."""
    global voice_restart_count

    while running:
        try:
            alive = any(
                t.name == "Thor-Voice" and t.is_alive()
                for t in threading.enumerate()
            )

            if not alive:
                with health_lock:
                    if running:
                        voice_restart_count += 1
                        print(
                            "[HEALTH] Voice listener stopped; restarting. "
                            "restart #", voice_restart_count
                        )
                        start_voice()

            time.sleep(2)

        except Exception as exc:
            print(
                "[HEALTH] monitor error:",
                type(exc).__name__,
                str(exc)
            )
            time.sleep(2)


def start_mic_diagnostics():
    def worker():
        names = list_microphones()
        if names:
            print("[MIC] Default recording device will be used.")
        else:
            print("[MIC] No microphone devices returned by PyAudio.")

    threading.Thread(
        target=worker,
        daemon=True,
        name="Thor-Mic-Diagnostics"
    ).start()


def start_voice():
    thread = threading.Thread(
        target=voice_loop,
        daemon=True,
        name="Thor-Voice"
    )
    thread.start()


def on_close():
    global running
    running = False
    try:
        root.destroy()
    except Exception:
        pass


ok_instance, thor_mutex = ensure_single_thor_instance()

if not ok_instance:
    # Do not start another microphone listener.
    raise SystemExit(0)

root.protocol("WM_DELETE_WINDOW", on_close)

add_message(
    "THOR",
    "Hello Sahil sir. Thor is online and ready."
)
add_message(
    "THOR",
    "Gemini 3.6 Flash | Voice + Web + Safe PC tools ready."
)

# Do NOT block GUI/microphone startup on a network/API probe.
start_gemini_bootstrap()

threading.Thread(
    target=reminder_loop,
    daemon=True
).start()

print("[OK] Thor reminder engine started")
print(f"[OK] Loaded reminders: {len(reminders)}")
print("[OK] Persistent conversation voice mode enabled")
print("[OK] THOR V44: non-blocking Gemini + queued AI + thread-safe GUI + voice health monitor")

run_thor_self_test()
start_ai_worker()

# Exactly ONE thread owns the physical microphone.
# Do not start a second diagnostic recording stream concurrently.
# Verified from the user's Windows/PyAudio device list:
# [15] Microphone Array (2- Intel(R) Smart Sound Technology for Digital Microphones)
# Do not use the Windows DEFAULT endpoint because it produced AudioSource errors.
MIC_DEVICE_INDEX = 15
print("[MIC] Locked input device:", MIC_DEVICE_INDEX)

print("[MIC] Starting permanent voice listener...")
start_voice()
print("[MIC] Permanent voice listener started.")
threading.Thread(
    target=start_voice_health_monitor,
    daemon=True,
    name="Thor-Health"
).start()
root.mainloop()
