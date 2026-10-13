"""Local models (Ollama) crowding the graphics card. Off by default: it assumes
Ollama. Turn on with models = true under [rules] in config.toml.

Every rule here is quiet when Ollama is not running: no Ollama is not a problem."""
from __future__ import annotations

import actions
from findings import Action, Finding
from rules._util import gb, plural

GB = 1024 ** 3


def _models(snap: dict) -> list[dict]:
    ol = snap.get("ollama") or {}
    return (ol.get("models") or []) if ol.get("up") else []


def _stop(m: dict) -> Action:
    return Action(f"Stop {m['name'][:24]}", actions.stop_ollama_model(m["name"]),
                  confirm_label=f"Unload {m['name'][:30]}? click again")


def starving_game(snap, cfg):
    """A model is holding video memory while something else is rendering and the card is full."""
    models = _models(snap)
    total, used = snap.get("vram_total") or 0, snap.get("vram_used") or 0
    rendering = (snap.get("gpu_types") or {}).get("3D") or 0
    held = sum(m["vram"] for m in models)
    if not models or not total or used / total < 0.95 or held < 2 * GB or rendering < 40:
        return None
    m = max(models, key=lambda m: m["vram"])
    return Finding(
        "models.starving_game", "warn", "Your model is starving whatever is rendering",
        f"Ollama holds {gb(held)} of the card's {gb(total)}, it is {used / total * 100:.0f}% full, "
        f"and the 3D engine is {rendering:.0f}% busy (a game?): the game's textures spill into slow system RAM",
        f"Unload {m['name']} while you play; it reloads by itself next time it is asked for.",
        action=_stop(m), cites=("vram",))


def vram_spill(snap, cfg):
    """A loaded model does not fit on the card, so part of it runs from system RAM."""
    spilled = [(m["size"] - m["vram"], m) for m in _models(snap) if m["size"] - m["vram"] > GB]
    if not spilled:
        return None
    over, m = max(spilled, key=lambda s: s[0])
    return Finding(
        "models.vram_spill", "warn", "A model is running partly on the CPU",
        f"{m['name']} needs {gb(m['size'])} but only {gb(m['vram'])} is on the card: "
        f"{gb(over)} sits in system RAM, which makes it several times slower and eats your RAM",
        "Use a smaller model or quantisation, or a shorter context (num_ctx); "
        "unloading other models can also make room.",
        action=_stop(m), cites=("vram",))


def keepalive_thrash(snap, cfg):
    """Ollama keeps unloading a model and loading it again."""
    now = snap.get("now") or 0
    recent = [(t, name) for t, name in snap.get("ollama_loads") or [] if now - t <= 30 * 60]
    counts: dict = {}
    for _, name in recent:
        counts[name] = counts.get(name, 0) + 1
    name, n = max(counts.items(), key=lambda kv: kv[1], default=(None, 0))
    if n < 4:
        return None
    return Finding(
        "models.keepalive_thrash", "info", "Ollama keeps reloading the same model",
        f"{name} was loaded {plural(n, 'time')} in the last 30 min; every load reads it from disk again",
        "Keep it loaded longer between requests: set OLLAMA_KEEP_ALIVE (say 30m) or pass keep_alive per request.")


def two_models(snap, cfg):
    """Two or more models are loaded at once and together do not fit on the card."""
    models = _models(snap)
    total = snap.get("vram_total") or 0
    size = sum(m["size"] for m in models)
    if len(models) < 2 or not total or size <= total:
        return None
    smaller = min(models, key=lambda m: m["size"])
    names = " + ".join(f"{m['name']} ({gb(m['size'])})" for m in models)
    return Finding(
        "models.two_models", "warn", "Loaded models do not fit on the card together",
        f"{names} = {gb(size)} against {gb(total)} of video memory, so part of them runs from system RAM",
        "Unload the one you are not using; only one at a time fits.",
        action=_stop(smaller), cites=("vram",))


RULES = [starving_game, two_models, vram_spill, keepalive_thrash]
