from __future__ import annotations

import json
import re
import os
import hashlib
import secrets
import base64
import mimetypes
from datetime import date
from html import escape
from pathlib import Path

from aqt import mw, gui_hooks
from aqt.qt import QAction, QDialog, QVBoxLayout, QFileDialog
from aqt.webview import AnkiWebView


ADDON_DIR = Path(__file__).parent


def _plain(value: str) -> str:
    """Keep basic card formatting while removing scripts and unsafe embeds."""
    value = re.sub(r"(?is)<(script|iframe).*?>.*?</\1>", "", value)
    return value


class VanillaStudyDialog(QDialog):
    def __init__(self, embedded: bool = False) -> None:
        super().__init__(mw)
        self.embedded = embedded
        self._page_loaded = False
        self.setWindowTitle("Vanilla Study")
        self.resize(1120, 760)
        if embedded:
            self.web = mw.web
        else:
            self.web = AnkiWebView(parent=self)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.web)
        self.web.set_bridge_command(self._bridge, self)
        self._show_decks()

    def _bridge(self, command: str) -> None:
        try:
            message = json.loads(command)
        except (TypeError, json.JSONDecodeError):
            return

        action = message.get("action")
        if action == "decks":
            self._show_decks()
        elif action == "open":
            self._show_cards(int(message["deckId"]))
        elif action == "answer":
            self._answer_card(int(message["cardId"]), int(message["ease"]))
        elif action == "fieldMapping":
            self._save_field_mapping(
                str(message.get("deck", "")),
                str(message.get("termField", "")),
                str(message.get("definitionField", "")),
            )
        elif action == "matchRecord":
            self._save_match_record(
                str(message.get("deck", "")),
                float(message.get("seconds", 0)),
            )
        elif action == "settings":
            self._save_settings(message)
        elif action == "saveLearn":
            self._save_learn_session(str(message.get("deck", "")), message.get("session", {}))
        elif action == "clearLearn":
            self._clear_learn_session(str(message.get("deck", "")))
        elif action == "studyActivity":
            self._record_study_activity()
        elif action == "xp":
            self._add_xp(int(message.get("amount", 0)), str(message.get("reason", "Study")))
        elif action == "profilePicture":
            self._save_profile_picture(str(message.get("data", "")))
        elif action == "profilePicturePick":
            self._pick_profile_picture()
        elif action == "personalMusicPick":
            self._pick_personal_music()
        elif action == "personalMusicRemove":
            self._remove_personal_music()
        elif action == "welcomeSeen":
            self._mark_welcome_seen()
        elif action == "onboardingProfile":
            self._save_onboarding_profile(str(message.get("name", "")))
        elif action == "accountCreate":
            self._create_local_account(str(message.get("email", "")), str(message.get("password", "")))
        elif action == "accountRemove":
            self._remove_local_account()
        elif action == "exportProgress":
            self._export_progress()
        elif action == "importProgress":
            self._import_progress()
        elif action == "ankiHome" and self.embedded:
            mw.moveToState("deckBrowser")

    def _asset(self, name: str) -> str:
        return (ADDON_DIR / "web" / name).read_text(encoding="utf-8")

    def _shell(self, data: dict) -> str:
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        return f"""<!doctype html><html><head><meta charset='utf-8'>
        <meta name='viewport' content='width=device-width,initial-scale=1'>
        <style>{self._asset('styles.css')}</style></head>
        <body><div id='app'></div><script>window.VANILLA_DATA={payload};</script>
        <script>{self._asset('app.js')}</script></body></html>"""

    def _render(self, data: dict) -> None:
        """Update the single-page app without destroying persistent audio."""
        if not self._page_loaded:
            self.web.stdHtml(self._shell(data))
            self._page_loaded = True
            return
        payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
        self.web.eval(f"window.vanillaReceive({payload})")

    def _show_decks(self) -> None:
        decks = []
        for deck in mw.col.decks.all_names_and_ids():
            count = len(mw.col.find_cards(f'deck:"{deck.name}"'))
            if count:
                decks.append({
                    "id": deck.id,
                    "name": deck.name,
                    "displayName": deck.name.split("::")[-1],
                    "count": count,
                })
        self._render({
            "view": "decks", "decks": decks, "settings": self._settings(),
            "embedded": self.embedded,
            "assetBase": f"/_addons/{__name__}/web/",
        })

    def _show_cards(self, deck_id: int) -> None:
        deck = mw.col.decks.get(deck_id)
        if not deck:
            return
        deck_name = deck["name"]
        config = mw.addonManager.getConfig(__name__) or {}
        config["last_deck"] = deck_name
        mw.addonManager.writeConfig(__name__, config)
        card_ids = mw.col.find_cards(f'deck:"{deck_name}"')[:250]
        cards = []
        available_fields = []
        seen_fields = set()
        for card_id in card_ids:
            card = mw.col.get_card(card_id)
            note = card.note()
            note_type = note.note_type() if hasattr(note, "note_type") else note.model()
            note_type_name = str(note_type.get("name", ""))
            field_names = {name.casefold() for name, _ in note.items()}
            is_image_occlusion = (
                "image occlusion" in note_type_name.casefold()
                or {"occlusion", "image"}.issubset(field_names)
            )
            # Cloze templates need Anki's special renderer and can expose raw
            # cloze markup in custom study modes, so leave them to Anki.
            if int(note_type.get("type", 0)) == 1 and not is_image_occlusion:
                continue
            # Study games need the note's fields separately. Rendered card
            # answers often repeat the question, which would reveal matches.
            field_values = [
                _plain(value).strip()
                for _, value in note.items()
                if value and value.strip()
            ]
            fields = {}
            for name, value in note.items():
                if name not in seen_fields:
                    seen_fields.add(name)
                    available_fields.append(name)
                fields[name] = _plain(value).strip()
            if is_image_occlusion:
                # Native Image Occlusion relies on the rendered card HTML for
                # its image and SVG masks; raw note fields are not sufficient.
                term = _plain(card.question())
                definition = _plain(card.answer())
            else:
                term = field_values[0] if field_values else _plain(card.question())
                definition = (
                    field_values[1]
                    if len(field_values) > 1
                    else _plain(card.answer())
                )
            cards.append({
                "id": card.id,
                "front": _plain(card.question()),
                # Use the isolated definition on the flashcard back instead of
                # the rendered answer template, which often repeats the term.
                "back": definition,
                "term": term,
                "definition": definition,
                "fields": fields,
                "imageOcclusion": is_image_occlusion,
            })
        mapping = self._field_mapping(deck_name, available_fields)
        self._render({
            "view": "study",
            "deck": deck_name,
            "displayDeck": deck_name.split("::")[-1],
            "cards": cards,
            "availableFields": available_fields,
            "fieldMapping": mapping,
            "matchRecord": self._match_record(deck_name),
            "learnSession": self._learn_session(deck_name),
            "settings": self._settings(),
            "embedded": self.embedded,
            "assetBase": f"/_addons/{__name__}/web/",
        })

    def _answer_card(self, card_id: int, ease: int) -> None:
        self._record_study_activity()
        card = mw.col.get_card(card_id)
        if not card:
            return
        try:
            mw.col.sched.answer_card(card, ease)
        except Exception:
            # Cards outside today's queue can still be studied in this MVP.
            pass

    def _field_mapping(self, deck_name: str, field_names: list[str]) -> dict:
        config = mw.addonManager.getConfig(__name__) or {}
        saved = config.get("field_mappings", {}).get(deck_name, {})
        ignored = {"word type", "part of speech", "pos", "tags", "tag", "notes",
                   "note", "extra", "source", "audio", "sound"}
        usable = [name for name in field_names if name.casefold().strip() not in ignored]
        term = saved.get("term") if saved.get("term") in field_names else (usable[0] if usable else "")
        definition = saved.get("definition") if saved.get("definition") in field_names else (
            usable[-1] if len(usable) > 1 else (field_names[1] if len(field_names) > 1 else "")
        )
        if definition == term and len(usable) > 1:
            definition = usable[1]
        return {"term": term, "definition": definition}

    def _save_field_mapping(self, deck_name: str, term_field: str, definition_field: str) -> None:
        if not deck_name or not term_field or not definition_field or term_field == definition_field:
            return
        config = mw.addonManager.getConfig(__name__) or {}
        config.setdefault("field_mappings", {})[deck_name] = {
            "term": term_field,
            "definition": definition_field,
        }
        mw.addonManager.writeConfig(__name__, config)

    def _match_record(self, deck_name: str) -> float | None:
        config = mw.addonManager.getConfig(__name__) or {}
        value = config.get("match_records", {}).get(deck_name)
        return float(value) if value is not None else None

    def _save_match_record(self, deck_name: str, seconds: float) -> None:
        if not deck_name or seconds <= 0:
            return
        config = mw.addonManager.getConfig(__name__) or {}
        records = config.setdefault("match_records", {})
        old = records.get(deck_name)
        if old is None or seconds < float(old):
            records[deck_name] = round(seconds, 1)
            mw.addonManager.writeConfig(__name__, config)

    def _settings(self) -> dict:
        config = mw.addonManager.getConfig(__name__) or {}
        streak = int(config.get("study_streak", 0))
        try:
            last_study = date.fromisoformat(config.get("last_study_date", ""))
            if (date.today() - last_study).days > 1:
                streak = 0
        except (TypeError, ValueError):
            streak = 0
        return {
            "theme": config.get("theme", "vanilla"),
            "font": config.get("font", "clean"),
            "embedded": bool(config.get("embedded", False)),
            "openOnStartup": bool(config.get("open_on_startup", False)),
            "startFullscreen": bool(config.get("start_fullscreen", False)),
            "overallStreak": streak,
            "customColor": config.get("custom_color", "#8b78d4"),
            "customEmoji": config.get("custom_emoji", "✨"),
            "deckEmoji": config.get("deck_emoji", "📚"),
            "customName": config.get("custom_name", ""),
            "greetingName": config.get("greeting_name", ""),
            "density": config.get("density", "comfortable"),
            "appearance": config.get("appearance", "auto"),
            "backgroundStyle": config.get("background_style", "gradient"),
            "backgroundColor": config.get("background_color", "#f7f8fc"),
            "textSize": config.get("text_size", "medium"),
            "roundness": config.get("roundness", "rounded"),
            "animations": config.get("animations", "full"),
            "navigation": config.get("navigation", "sidebar"),
            "showStreak": bool(config.get("show_streak", True)),
            "showRecommendations": bool(config.get("show_recommendations", True)),
            "showProgress": bool(config.get("show_progress", True)),
            "soundEnabled": bool(config.get("sound_enabled", True)),
            "musicEnabled": bool(config.get("music_enabled", True)),
            "effectsEnabled": bool(config.get("effects_enabled", True)),
            "musicVolume": float(config.get("music_volume", 0.22)),
            "effectsVolume": float(config.get("effects_volume", 0.35)),
            "selectedTrack": config.get("selected_track", "lofi-dusk"),
            "musicLoop": bool(config.get("music_loop", True)),
            "musicShuffle": bool(config.get("music_shuffle", False)),
            "studyDates": config.get("study_dates", []),
            "lastDeck": config.get("last_deck"),
            "xpTotal": int(config.get("xp_total", 0)),
            "xpDaily": config.get("xp_daily", {}),
            "accountEmail": config.get("account_email", ""),
            "profilePicture": config.get("profile_picture", ""),
            "profilePosition": "top-right",
            "profileX": float(config.get("profile_x", 50)),
            "profileY": float(config.get("profile_y", 50)),
            "profileZoom": float(config.get("profile_zoom", 1)),
            "customMusicName": config.get("custom_music_name", ""),
            "customMusicUrl": config.get("custom_music_url", ""),
            "welcomeSeen": bool(config.get("welcome_seen", False)),
        }

    def _save_settings(self, values: dict) -> None:
        allowed_themes = {"vanilla", "lavender", "sage", "midnight", "sky", "teal", "mono", "custom"}
        allowed_fonts = {"clean", "rounded", "classic", "playful", "modern", "humanist", "typewriter", "readable"}
        config = mw.addonManager.getConfig(__name__) or {}
        theme = str(values.get("theme", "vanilla"))
        font = str(values.get("font", "clean"))
        custom_color = str(values.get("customColor", "#8b78d4"))
        custom_name = str(values.get("customName", ""))
        custom_emoji = str(values.get("customEmoji", "✨"))
        greeting_name = str(values.get("greetingName", ""))
        config["theme"] = theme if theme in allowed_themes else "vanilla"
        config["font"] = font if font in allowed_fonts else "clean"
        config["embedded"] = bool(values.get("embedded", False))
        config["open_on_startup"] = bool(values.get("openOnStartup", False))
        config["start_fullscreen"] = bool(values.get("startFullscreen", False))
        config["custom_color"] = custom_color if re.fullmatch(r"#[0-9a-fA-F]{6}", custom_color) else "#8b78d4"
        blocked_words = {"fuck", "shit", "bitch", "cunt", "nigger", "nigga", "faggot", "porn", "sex"}
        blocked_emoji = {"🔞", "🍆", "💦", "🍺", "🍻", "🍷", "🥃", "🚬", "💊", "🔪", "🔫", "🎰"}
        clean_name = custom_name.strip()[:30]
        name_words = set(re.findall(r"[a-z]+", clean_name.casefold()))
        clean_emoji = custom_emoji.strip()[:8] or "✨"
        if name_words & blocked_words or re.search(r"[<>\x00-\x1f]", clean_name):
            clean_name = config.get("custom_name", "")
        if any(symbol in clean_emoji for symbol in blocked_emoji) or re.search(r"[<>\x00-\x1f]", clean_emoji):
            clean_emoji = config.get("custom_emoji", "✨")
        config["custom_emoji"] = clean_emoji
        deck_emoji = str(values.get("deckEmoji", "📚")).strip()[:8] or "📚"
        config["deck_emoji"] = deck_emoji if not re.search(r"[<>\x00-\x1f]", deck_emoji) else "📚"
        config["custom_name"] = clean_name
        config["greeting_name"] = re.sub(r"[<>\x00-\x1f]", "", greeting_name).strip()[:30]
        choices = {
            "density": ({"compact", "comfortable", "spacious"}, "comfortable"),
            "appearance": ({"light", "dark", "auto"}, "auto"),
            "background_style": ({"plain", "gradient"}, "gradient"),
            "text_size": ({"small", "medium", "large"}, "medium"),
            "roundness": ({"square", "soft", "rounded"}, "rounded"),
            "animations": ({"full", "reduced", "off"}, "full"),
            "navigation": ({"sidebar", "top", "minimal"}, "sidebar"),
            "selected_track": ({"lofi-dusk", "piano-window", "forest-rain", "deep-focus"}, "lofi-dusk"),
        }
        js_names = {"background_style": "backgroundStyle", "text_size": "textSize",
                    "selected_track": "selectedTrack"}
        for key, (allowed, default) in choices.items():
            value = str(values.get(js_names.get(key, key), default))
            config[key] = value if value in allowed else default
        background = str(values.get("backgroundColor", "#f7f8fc"))
        config["background_color"] = background if re.fullmatch(r"#[0-9a-fA-F]{6}", background) else "#f7f8fc"
        for key, js_key in {
            "show_streak": "showStreak", "show_recommendations": "showRecommendations",
            "show_progress": "showProgress", "sound_enabled": "soundEnabled",
            "music_enabled": "musicEnabled", "effects_enabled": "effectsEnabled",
            "music_loop": "musicLoop", "music_shuffle": "musicShuffle",
        }.items():
            config[key] = bool(values.get(js_key, True if key != "music_shuffle" else False))
        for key, js_key, default in (
            ("music_volume", "musicVolume", 0.22),
            ("effects_volume", "effectsVolume", 0.35),
        ):
            try:
                config[key] = max(0.0, min(1.0, float(values.get(js_key, default))))
            except (TypeError, ValueError):
                config[key] = default
        position = str(values.get("profilePosition", "top-right"))
        config["profile_position"] = "top-right"
        try:
            config["profile_x"] = max(0.0, min(100.0, float(values.get("profileX", 50))))
            config["profile_y"] = max(0.0, min(100.0, float(values.get("profileY", 50))))
            config["profile_zoom"] = max(1.0, min(2.4, float(values.get("profileZoom", 1))))
        except (TypeError, ValueError):
            config["profile_x"], config["profile_y"], config["profile_zoom"] = 50, 50, 1
        mw.addonManager.writeConfig(__name__, config)


    def _save_onboarding_profile(self, name: str) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        config["greeting_name"] = re.sub(r"[<>\x00-\x1f]", "", name).strip()[:30]
        mw.addonManager.writeConfig(__name__, config)

    def _record_study_activity(self) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        today = date.today()
        previous_text = config.get("last_study_date")
        if previous_text == today.isoformat():
            dates = config.setdefault("study_dates", [])
            if today.isoformat() not in dates:
                dates.append(today.isoformat())
                config["study_dates"] = dates[-370:]
                mw.addonManager.writeConfig(__name__, config)
            return
        try:
            previous = date.fromisoformat(previous_text) if previous_text else None
        except ValueError:
            previous = None
        config["study_streak"] = int(config.get("study_streak", 0)) + 1 if previous and (today - previous).days == 1 else 1
        config["last_study_date"] = today.isoformat()
        dates = config.setdefault("study_dates", [])
        if today.isoformat() not in dates:
            dates.append(today.isoformat())
        config["study_dates"] = dates[-370:]
        mw.addonManager.writeConfig(__name__, config)

    def _add_xp(self, amount: int, reason: str = "Study") -> None:
        amount = max(0, min(int(amount), 500))
        if not amount:
            return
        config = mw.addonManager.getConfig(__name__) or {}
        config["xp_total"] = int(config.get("xp_total", 0)) + amount
        daily = config.setdefault("xp_daily", {})
        key = date.today().isoformat()
        daily[key] = int(daily.get(key, 0)) + amount
        # Keep roughly one year of daily history.
        config["xp_daily"] = dict(sorted(daily.items())[-370:])
        mw.addonManager.writeConfig(__name__, config)

    def _save_profile_picture(self, data_url: str) -> None:
        if not data_url.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
            return
        if len(data_url) > 16_500_000:
            return
        config = mw.addonManager.getConfig(__name__) or {}
        config["profile_picture"] = data_url
        mw.addonManager.writeConfig(__name__, config)

    def _pick_profile_picture(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose profile picture", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
        except OSError:
            return
        if len(raw) > 12_000_000:
            return
        mime = mimetypes.guess_type(path)[0] or "image/png"
        if mime not in {"image/png", "image/jpeg", "image/webp"}:
            return
        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        self._save_profile_picture(data_url)
        self.web.eval(f"window.vanillaProfileSaved({json.dumps(data_url)});")

    def _pick_personal_music(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose personal study music", "", "Audio (*.mp3 *.wav *.m4a *.ogg)")
        if not path:
            return
        suffix = Path(path).suffix.lower()
        if suffix not in {".mp3", ".wav", ".m4a", ".ogg"}:
            return
        try:
            raw = Path(path).read_bytes()
        except OSError:
            return
        # Store the personal track in Anki media so the webview can play it reliably.
        filename = "vanilla_study_personal_audio" + suffix
        try:
            mw.col.media.write_data(filename, raw)
        except Exception:
            return
        config = mw.addonManager.getConfig(__name__) or {}
        old = config.get("custom_music_media", "")
        if old and old != filename:
            try:
                mw.col.media.trash_files([old])
            except Exception:
                pass
        url = "/_anki/" + filename
        config["custom_music_name"] = Path(path).name
        config["custom_music_media"] = filename
        config["custom_music_url"] = url
        mw.addonManager.writeConfig(__name__, config)
        self.web.eval(f"window.vanillaPersonalMusicSaved({json.dumps(Path(path).name)}, {json.dumps(url)});")

    def _remove_personal_music(self) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        filename = config.get("custom_music_media", "")
        if filename:
            try:
                mw.col.media.trash_files([filename])
            except Exception:
                pass
        for key in ("custom_music_name", "custom_music_media", "custom_music_url"):
            config.pop(key, None)
        mw.addonManager.writeConfig(__name__, config)
        self.web.eval("window.vanillaPersonalMusicRemoved();")

    def _mark_welcome_seen(self) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        config["welcome_seen"] = True
        mw.addonManager.writeConfig(__name__, config)

    def _create_local_account(self, email: str, password: str) -> None:
        email = email.strip().lower()
        strong = (len(password) >= 10 and re.search(r"[a-z]", password) and
                  re.search(r"[A-Z]", password) and re.search(r"\d", password) and
                  re.search(r"[^A-Za-z0-9]", password))
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or not strong:
            return
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
        config = mw.addonManager.getConfig(__name__) or {}
        config["account_email"] = email
        config["account_salt"] = salt.hex()
        config["account_password_hash"] = digest.hex()
        mw.addonManager.writeConfig(__name__, config)

    def _remove_local_account(self) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        for key in ("account_email", "account_salt", "account_password_hash"):
            config.pop(key, None)
        mw.addonManager.writeConfig(__name__, config)

    def _export_progress(self) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        excluded = {"account_salt", "account_password_hash"}
        payload = {"format": "vanilla-study-progress", "version": 1,
                   "data": {k: v for k, v in config.items() if k not in excluded}}
        path, _ = QFileDialog.getSaveFileName(self, "Export Vanilla Study progress",
                                              "vanilla-study-progress.json", "JSON files (*.json)")
        if path:
            Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _import_progress(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import Vanilla Study progress", "",
                                              "JSON files (*.json)")
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            incoming = payload.get("data", {}) if payload.get("format") == "vanilla-study-progress" else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            return
        if not isinstance(incoming, dict):
            return
        config = mw.addonManager.getConfig(__name__) or {}
        protected = {"account_salt", "account_password_hash", "account_email"}
        for key, value in incoming.items():
            if key not in protected:
                config[key] = value
        mw.addonManager.writeConfig(__name__, config)
        self._show_decks()

    def _learn_session(self, deck_name: str) -> dict | None:
        config = mw.addonManager.getConfig(__name__) or {}
        session = config.get("learn_sessions", {}).get(deck_name)
        return session if isinstance(session, dict) else None

    def _save_learn_session(self, deck_name: str, session: dict) -> None:
        if not deck_name or not isinstance(session, dict):
            return
        config = mw.addonManager.getConfig(__name__) or {}
        config.setdefault("learn_sessions", {})[deck_name] = session
        mw.addonManager.writeConfig(__name__, config)

    def _clear_learn_session(self, deck_name: str) -> None:
        config = mw.addonManager.getConfig(__name__) or {}
        sessions = config.setdefault("learn_sessions", {})
        if deck_name in sessions:
            del sessions[deck_name]
            mw.addonManager.writeConfig(__name__, config)


_dialog = None

# Expose the bundled offline soundtracks to the persistent web interface.
mw.addonManager.setWebExports(__name__, r"web/.*")


def open_vanilla_study() -> None:
    global _dialog
    config = mw.addonManager.getConfig(__name__) or {}
    embedded = bool(config.get("embedded", False))
    _dialog = VanillaStudyDialog(embedded=embedded)
    if not embedded:
        if bool(config.get("start_fullscreen", False)):
            _dialog.showFullScreen()
        else:
            _dialog.show()


def _open_on_profile_startup() -> None:
    config = mw.addonManager.getConfig(__name__) or {}
    if bool(config.get("open_on_startup", False)):
        open_vanilla_study()


gui_hooks.profile_did_open.append(_open_on_profile_startup)


action = QAction("Vanilla Study", mw)
action.setShortcut("Ctrl+Shift+V")
action.triggered.connect(open_vanilla_study)
mw.form.menuTools.addAction(action)
