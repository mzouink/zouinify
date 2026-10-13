"""The launcher registry: <state_dir>/sessions.json, one entry per session zouinify launched
(name, tmux window, role, ticket, when, by whom). It removes the guesswork of mapping a Claude session
to its tmux window; `down` kills only what is registered here."""
from __future__ import annotations

import json
import os
import tempfile


class Registry:
    def __init__(self, cfg):
        self.path = os.path.join(cfg.state_dir, "sessions.json")

    def load(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except ValueError:
            return []
        return [e for e in data if isinstance(e, dict) and e.get("name")] if isinstance(data, list) else []

    def save(self, entries: list) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".sessions.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def get(self, name: str) -> dict | None:
        for e in self.load():
            if e.get("name") == name or e.get("tmux") == name:
                return e
        return None

    def add(self, entry: dict) -> None:
        entries = [e for e in self.load() if e.get("name") != entry["name"]]
        entries.append(entry)
        self.save(entries)

    def remove(self, name: str) -> None:
        self.save([e for e in self.load() if e.get("name") != name])
