"""The launcher registry: <state_dir>/sessions.json, one entry per session zouinify launched
(name, tmux window, role, ticket, when, by whom). It removes the guesswork of mapping a Claude session
to its tmux window; `down` kills only what is registered here (and re-checks the window against the
config's role sessions, since this file is writable by every session). Writes hold an flock on
sessions.json.lock so two concurrent launches never lose an entry."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile


class Registry:
    def __init__(self, cfg):
        self.path = os.path.join(cfg.state_dir, "sessions.json")

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def load(self) -> list:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return []
        except ValueError:
            return []
        return [e for e in data if isinstance(e, dict) and e.get("name")] if isinstance(data, list) else []

    def _save(self, entries: list) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".sessions.", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=1, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    def save(self, entries: list) -> None:
        with self._locked():
            self._save(entries)

    def get(self, name: str) -> dict | None:
        for e in self.load():
            if e.get("name") == name or e.get("tmux") == name:
                return e
        return None

    def add(self, entry: dict) -> None:
        with self._locked():
            entries = [e for e in self.load() if e.get("name") != entry["name"]]
            entries.append(entry)
            self._save(entries)

    def remove(self, name: str) -> None:
        with self._locked():
            self._save([e for e in self.load() if e.get("name") != name])
