# ⚡ THOR — Personal AI Voice Assistant

THOR is an AI-powered personal voice assistant built with Python. It can understand natural voice commands and perform AI, web, media and safe PC automation tasks.

## 🚀 Features

- 🎤 Voice command recognition
- 🤖 Google Gemini AI integration
- 🔊 Microsoft Windows Text-to-Speech
- 🌐 Google/web search
- ▶️ YouTube search and music control
- 💻 Safe PC controls
- 📸 Screenshot capture
- ⏰ Reminder system
- 🧠 Conversation support
- 🇮🇳 Hindi, English and Hinglish commands
- 🔄 Error handling and voice recovery
- 🖥️ Desktop GUI using Tkinter

## 🛠️ Tech Stack

### Frontend
- Python Tkinter

### Backend
- Python
- Command routing
- Threading
- Queue
- JSON-based local storage

### AI
- Google Gemini API

### Voice
- SpeechRecognition
- Microphone input
- Windows SAPI / Microsoft TTS
- pyttsx3 fallback

### Automation
- Webbrowser
- PyAutoGUI
- YouTube/media controls
- Windows PC tools

## 🏗️ Project Architecture

```text
Voice Input
    ↓
Speech Recognition
    ↓
Command / Intent Detection
    ↓
 ┌───────────────┬────────────────┐
 │ Local Command │   Gemini AI    │
 ↓               ↓                │
PC / Web / Media  AI Response      │
 │               │                │
 └──────────┬────┴────────────────┘
            ↓
       Microsoft TTS
            ↓
       THOR Voice Output
