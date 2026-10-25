"""AutoRL Generation: open-ended, self-directing long-form video in the Production tab.

A story seed (a world, where it starts, the characters with their looks and pictures, a visual style and the user's
direction) becomes a film that keeps going until it is stopped. Each turn of the loop:

  plan      the Director (the film's text brain: the Studio's Bolt, Claude Code, Codex or a local server) reads the
            scene graph and the last segment's ending and writes the next segment as the Studio's own prompt fields;
            an answer that restages an earlier beat, repeats the segment before, keeps a scene past its budget, or
            names what must not be seen is turned back with the reason and asked again (``MAX_ATTEMPTS``)
  render    the segment goes through the Studio's render queue like any clip: the same engines, presets, LoRAs,
            memory guard and Spark routing. It opens on the last frame of the segment before (image to video); a new
            scene opens on the cast's reference pictures (Ref2VA) where that model is installed, else from text
  check     the QA gate (qa.py: the Whisper line check and the identity sheet) with the vision brain as its eyes
  watch     the vision brain (cosmos.py: NVIDIA Cosmos3-Edge by default) looks at eight frames of the segment in time
            order with the Whisper transcript and reports only what changed: the scene graph's delta, the state the
            segment ends in, and continuity problems. Identity drift fails the segment like the QA gate does, and it is
            rendered again at the next seed (``retakes``)
  commit    the delta is merged into the scene graph; past ``COMPACT_AT`` events the oldest are folded into a chapter
  preview   every ``preview_every`` segments the film is joined again (``film.mp4``): a continuation drops the frame it
            repeats (one-frame cross-fade, picture and sound together), a new scene dissolves in a quarter second

Pause stops the loop after the segment at work; resume picks it up where it stopped, a restart of the app too.
Branch makes a new run that shares the first N segments and their memory and continues with new direction.

The idea of an endless film that watches itself and decides what happens next is Echo-Infinity's (Yuxuan Bian,
Nan Duan, Qiang Xu et al., CUHK and Joy Future Academy, JD). The loop's design, the scene-graph memory (its delta schema, the merge and id folding, the chapter
fold), the Director's rules against repetition and stagnation and the branch are adapted from Video Rebirth's
HyperFlow-Infinity (github.com/Video-Rebirth/hyperflow-infinity), Copyright Video Rebirth, Apache License 2.0; see
NOTICE and LICENSES/HyperFlow-Infinity-LICENSE. The changes: the Studio's engines, presets and queue instead of the
HyperFlow 8-step adapter and its latent hand-off (docs/autorl.md has the design of a latent-tail continuation), the
Studio's prompt fields and content rules, a vision brain of the user's choice, and the QA gate on every segment.

Run files live in ``<studio folder>/autorl/<run id>/``: ``run.json`` (settings, seed, segments, state), ``memory.json``
(the scene graph, schema in ``scene-graph.schema.json`` beside it), ``memory/S0001.json`` (the memory after each
segment), ``segments/S0001/`` (takes, frames, the Director's and the watcher's answers) and ``film.mp4``.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

import cosmos  # the vision brain (NVIDIA Cosmos3-Edge on TensorFold), beside this file
import lengths  # which frame counts the model takes
import negation  # what a prompt forbids by name
import qa  # Sam Wasserman's QA gate: the Whisper transcript, the frame sheet and its vision path
import slate  # the brains (slate.ask) and the Slate bible a seed can come from

router = APIRouter()
HOME: Path = Path()
FPS = 24
TICK = 2.0
LOG = 200
MAX_ATTEMPTS = 3  # a Director or watcher answer that does not hold up is asked again, this many tries in all
STATE_WORDS, EVENTS_PER_SEGMENT = 12, 2
COMPACT_AT, FOLD = 22, 10  # above this many events the oldest FOLD become one chapter
RECENT_EVENTS = 8
SIMILAR_ACTION, SIMILAR_BEAT = 0.9, 0.85  # difflib ratios above which a new segment repeats an old one
LIMIT = 500  # segments a run may have when it is asked for a number (0 runs until it is stopped)
ACTIVE = ("running",)
CONTINUE_FADE, CUT_FADE = 1 / FPS, 0.25  # the joins of the film: the repeated frame, and a dissolve on a new scene

DEFAULTS = {"style": "", "guidance": "", "preset": "small", "quality": "fh8", "width": None, "height": None, "seconds": 5,
            "loras": [], "segments": 6, "seed": 1, "max_scene_segments": 8, "beat_history": 12, "qa": True, "retakes": 1,
            "preview_every": 3, "identity": "auto", "ref_turbo": True, "music": "", "director": "brain",
            "brain": {"backend": slate.AUTO, "endpoint": "", "model": ""}}
IDENTITY = ("auto", "frame", "references")  # how a new scene keeps who is who: Ref2VA pictures where installed, or words

# what the server hands in (attach); tests stand in for them
submit = lambda body: (_ for _ in ()).throw(RuntimeError("the queue is not attached"))
job_of = lambda job_id: None
cancel = lambda job_id: None
compose = lambda fields, first_frame, frames=None: fields.get("action", "")
compose_references = lambda fields, count: fields.get("action", "")
ref2va = lambda: False
ref2va_turbo = lambda: False

runs: dict[str, dict] = {}
guard = threading.RLock()
starting = threading.Lock()


class Stopped(Exception):
    pass


class Failed(Exception):
    pass


def attach(home: Path, *, queue, lookup, stop_job, plain, referenced, references=None, references_turbo=None,
           quality: str = "") -> APIRouter:
    global HOME, submit, job_of, cancel, compose, compose_references, ref2va, ref2va_turbo
    HOME, submit, job_of, cancel, compose, compose_references = home, queue, lookup, stop_job, plain, referenced
    ref2va, ref2va_turbo = references or ref2va, references_turbo or ref2va_turbo
    if quality:
        DEFAULTS["quality"] = quality
    (HOME / "autorl").mkdir(parents=True, exist_ok=True)
    cosmos.attach(HOME)
    return router


def resume_later() -> None:
    later = threading.Timer(6.0, resume_all)
    later.daemon = True
    later.start()


# ---------- the scene graph (adapted from HyperFlow-Infinity's memory.py, Apache-2.0) ----------

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "AutoRL Generation scene graph",
    "description": "What the film has shown so far: who and what persists (nodes), what changed the story (events), and "
                   "older events folded into chapters. Written only from what the watcher saw, never from what was asked for.",
    "type": "object",
    "required": ["nodes", "events", "chapters"],
    "properties": {
        "nodes": {"type": "array", "items": {"type": "object", "required": ["id", "type", "label", "state"], "properties": {
            "id": {"type": "string", "description": "short unique id, reused for the same person, place or thing"},
            "type": {"enum": ["character", "place", "object"]},
            "label": {"type": "string", "description": "a name, three words at most"},
            "state": {"type": "string", "description": "how it is now, twelve words at most"}}}},
        "events": {"type": "array", "items": {"type": "object", "required": ["t", "subject", "action"], "properties": {
            "t": {"type": "string", "description": "MM:SS on the film's clock"},
            "subject": {"type": "string"}, "action": {"type": "string", "description": "a verb phrase, eight words at most"},
            "target": {"type": "string"}, "place": {"type": "string"}, "segment": {"type": "integer"}}}},
        "chapters": {"type": "array", "items": {"type": "object", "required": ["id", "title", "summary"], "properties": {
            "id": {"type": "string"}, "t_start": {"type": "string"}, "t_end": {"type": "string"},
            "title": {"type": "string"}, "summary": {"type": "string"},
            "carried_forward": {"type": "array", "items": {"type": "string"}, "maxItems": 3}}}},
    },
}
EMPTY = {"graph": {"nodes": [], "events": [], "chapters": []}, "archive": [], "observed_through": 0}


def clip_words(text, limit: int) -> str:
    return " ".join(str(text or "").split()[:limit])


def mmss(seconds: float) -> str:
    value = int(round(seconds))
    return f"{value // 60:02d}:{value % 60:02d}"


def resolve_id(new_id, known) -> str:
    """A proposed id mapped onto an existing node when it is plainly the same thing (``guard`` / ``guard_2``,
    ``ada`` / ``ada_older``, near spellings): vision models do not reliably reuse ids."""

    if not new_id or new_id in known:
        return new_id
    for old in known:
        if str(new_id).startswith(f"{old}_") or str(old).startswith(f"{new_id}_"):
            return old
    match = difflib.get_close_matches(str(new_id), [str(k) for k in known], n=1, cutoff=0.82)
    return match[0] if match else new_id


def merge(graph: dict, delta: dict, segment: int = 0) -> dict:
    """One watcher delta added to the graph: states replace, at most two events a segment, ids folded."""

    graph = copy.deepcopy(graph or EMPTY["graph"])
    graph.setdefault("chapters", [])
    by_id = {n.get("id"): n for n in graph["nodes"]}
    remap = {}
    for node in delta.get("add_nodes") or []:
        if not isinstance(node, dict) or not node.get("id"):
            continue
        raw = str(node["id"])
        target = resolve_id(raw, by_id.keys())
        remap[raw] = target
        state = clip_words(node.get("state"), STATE_WORDS)
        if target in by_id:
            if state:
                by_id[target]["state"] = state
        else:
            kind = node.get("type") if node.get("type") in ("character", "place", "object") else "character"
            graph["nodes"].append({"id": target, "type": kind, "label": clip_words(node.get("label"), 3) or target, "state": state})
            by_id[target] = graph["nodes"][-1]
    for change in delta.get("update_nodes") or []:
        if not isinstance(change, dict):
            continue
        node = by_id.get(remap.get(change.get("id"), resolve_id(change.get("id"), by_id.keys())))
        if node is not None and change.get("state"):
            node["state"] = clip_words(change["state"], STATE_WORDS)
    for event in [e for e in delta.get("add_events") or [] if isinstance(e, dict)][:EVENTS_PER_SEGMENT]:
        event = {k: event.get(k) for k in ("t", "subject", "action", "target", "place") if event.get(k)}
        for key in ("subject", "target", "place"):
            if event.get(key):
                event[key] = remap.get(event[key], resolve_id(event[key], by_id.keys()))
        event["action"] = clip_words(event.get("action"), 8)
        if segment:
            event["segment"] = segment
        if event["action"]:
            graph["events"].append(event)
    return graph


def fold(graph: dict, chapter: dict, folded: list) -> list:
    """The ``folded`` oldest events replaced by one chapter; the removed events are returned for the archive."""

    graph.setdefault("chapters", []).append({
        "id": f"ch{len(graph['chapters']) + 1}", "t_start": folded[0].get("t", ""), "t_end": folded[-1].get("t", ""),
        "title": clip_words(chapter.get("title"), 5), "summary": clip_words(chapter.get("summary"), 30),
        "carried_forward": [clip_words(x, 10) for x in (chapter.get("carried_forward") or [])[:3]]})
    graph["events"] = graph["events"][len(folded):]
    return folded


def as_text(graph: dict, recent: int = RECENT_EVENTS) -> str:
    """A bounded view for the Director: chapters, the cast as they are now, places and things, the last events.
    Bounded on purpose: a view that grew with the film grew the prompts with it."""

    graph, lines = graph or {}, []
    for ch in graph.get("chapters") or []:
        carried = "; ".join(ch.get("carried_forward") or [])
        lines.append(f"[{ch.get('t_start')}-{ch.get('t_end')}] {ch.get('title')}: {ch.get('summary')}" + (f" (still matters: {carried})" if carried else ""))
    nodes = graph.get("nodes") or []
    cast = [n for n in nodes if (n.get("type") or "character") == "character"]
    if cast:
        lines.append("Cast as they now are: " + "; ".join(f"{n.get('label')} -- {n.get('state')}" for n in cast))
    world = [n for n in nodes if (n.get("type") or "character") != "character"]
    if world:
        lines.append("Places and things in play: " + ", ".join(f"{n.get('label')} ({n.get('state')})" if n.get("state") else str(n.get("label")) for n in world))
    tail = (graph.get("events") or [])[-recent:]
    if tail:
        lines.append("Just happened: " + "; ".join(f"{e.get('t')} {e.get('subject')} {e.get('action')}" + (f" -> {e['target']}" if e.get("target") else "") for e in tail))
    return "\n".join(lines)


def scene_length(segments: list) -> int:
    """Segments since, and including, the last one that opened a scene."""

    n = 0
    for done in reversed(segments):
        n += 1
        if done.get("scene_change"):
            break
    return n


def signature(delta: dict):
    """A delta in a comparable form (timestamps left out); None when it carries no state to compare."""

    if not delta or delta.get("add_nodes") or not delta.get("add_events") or not delta.get("update_nodes"):
        return None
    norm = lambda value: " ".join(str(value or "").lower().split())
    events = sorted((norm(e.get("subject")), norm(e.get("action")), norm(e.get("target")), norm(e.get("place"))) for e in delta["add_events"] if isinstance(e, dict))
    states = sorted((norm(n.get("id")), norm(n.get("state"))) for n in delta["update_nodes"] if isinstance(n, dict))
    if any(not e[1] for e in events) or any(not s[1] for s in states):
        return None
    return [events, states]


def stagnation(segments: list):
    """Three segments in a row, in one scene, whose watcher saw the same events and states: the film is circling."""

    recent = segments[-3:]
    if len(recent) < 3 or len({s.get("scene") for s in recent}) != 1:
        return None
    marks = [signature((s.get("watch") or {}).get("delta")) for s in recent]
    if marks[0] is not None and all(m == marks[0] for m in marks):
        return {"segments": [s["index"] for s in recent], "reason": "three segments in a row show the same events and states"}
    return None


# ---------- the prompts (the Director's rules adapted from HyperFlow-Infinity's prompts.py, Apache-2.0) ----------

DIRECTOR_SYSTEM = """You are the director of a long-form film generated one short segment at a time by a \
text-to-video-and-audio model (MiniMax H3). Each segment is a few seconds long with its own synchronized sound.

Answer with a single JSON object and nothing else:
{
  "scene": "<short name of the scene this segment belongs to>",
  "scene_change": true | false,
  "beat": "<one short line: what this segment accomplishes in the story>",
  "new_characters": [{"name": "...", "look": "<concrete visual description: age, face, hair, clothing>"}],
  "shot": {
    "setting": "<the place, the time of day and the light, concrete and visual>",
    "camera": "<framing and camera movement>",
    "action": "<what happens, in temporal order: one legible, achievable action and its visible result>",
    "dialogue": [{"speaker": "<name>", "voice": "<how the voice sounds>", "text": "<a short spoken line>"}],
    "soundscape": "<the diegetic sound: ambience, impacts, movement>",
    "music": "<the score, or N/A>"
  }
}

"scene_change" is false while the story stays in the same place with the same people: the segment then continues \
the previous one from its last frame, the action picking up exactly where it stopped, the same light and camera \
language. Set it to true when the story moves to another place or time: the segment then opens fresh. Prefer \
continuous scenes of several segments over rapid cutting, but move on once a scene's purpose is done.

Every segment moves the story one concrete, visible step. Restating the situation in other words is a wasted \
segment: "they are hunted", "they are cornered", "they hide", "they still hide" is one beat, not four. A new \
expression, another reaction, a camera change or restarting the same unfinished action is not a new step.

List a character in "new_characters" only the first time they appear, with a look concrete enough to repeat word \
for word forever after. Name characters by name in "action" and "setting".

Rules that matter:
- Keep it suitable for a general audience: nothing sexual, no graphic gore or lingering on injury; conflict and \
danger are shown without graphic detail. No real public figures. Children are never placed in harm.
- Write only what a camera and a microphone can record: no inner thoughts, backstory or commentary.
- Never ask for on-screen text, subtitles, logos or watermarks.
- Say what is there, never what is not: no "no X", "without X" or "X is absent". The video model has no negative \
prompt and what is named tends to appear.
- Dialogue is optional and short: about two words a second of the segment at most. Leave "dialogue" empty for a \
segment without speech.
- Continue from what the film actually showed (the watcher's report and the scene graph), not from what an earlier \
segment asked for. Previous beats are requests, not facts.
- Write in English.

This is open-ended: there is no prescribed plot, act schedule, ending or total length. Decide the next short step \
from the evidence."""

WATCH_SYSTEM = ("You watch a film as it is generated, one short segment at a time, and keep its scene graph: you report only "
                "what each new segment changes, and anything that breaks continuity. Answer only with JSON.")

COMPACT_SYSTEM = "You compress a film's history into chapters. Answer only with JSON."


def seed_text(seed: dict, style: str) -> str:
    cast = "\n".join(f"- {c.get('name')}: {c.get('look') or 'as first seen'}" for c in seed.get("characters") or [] if c.get("name"))
    return "\n".join(bit for bit in (
        f"Inspiration (a starting point, not a plot to retell): {seed['inspiration']}" if seed.get("inspiration") else "",
        f"World: {seed.get('world') or 'open'}", f"Where the film starts: {seed.get('situation') or 'open'}",
        f"Characters:\n{cast}" if cast else "", f"Visual style: {style}" if style else "") if bit)


def direction(settings: dict, branch: dict | None, index: int) -> str:
    text = ""
    if settings.get("guidance"):
        text += f"\nThe user's creative direction: {settings['guidance']}\n"
    if branch and index > int(branch.get("after") or 0):
        text += (f"\nThe user asked for another way the story goes on after segment {branch['after']}:\n"
                 "<user_story_guidance>\n" + branch["guidance"] + "\n</user_story_guidance>\n"
                 "This is direction for what comes next, NOT something that already happened. Develop it from what the film "
                 "shows now; keep the shared past, who is who, what they hold and where they are. Get there through visible, "
                 "achievable actions: no teleporting, no restarting the opening, no pretending it already happened.\n")
    return text


def first_prompt(run: dict, seconds: float) -> str:
    s = run["settings"]
    return (seed_text(run["seed"], s.get("style", "")) + "\n" + direction(s, run.get("branch"), 1)
            + f"\nWrite segment 1, about {seconds:.0f} seconds. It is the opening: establish the world, the look and the "
              "mood, and start the situation. Name its scene and set \"scene_change\" to true.")


def next_prompt(run: dict, index: int, seconds: float, memory_text: str, note: str) -> str:
    s, segments = run["settings"], committed(run)
    previous = segments[-1]
    beats = "\n".join(f"  {seg['index']}. {seg.get('beat', '')[:110]}" for seg in segments[-int(s.get("beat_history") or 12):]) or "  (none yet)"
    budget, length = int(s.get("max_scene_segments") or 8), scene_length(segments)
    pacing = f"The current scene is {previous.get('scene')!r} and has run {length} segment(s) of a {budget}-segment budget. "
    pacing += ("That budget is spent: set \"scene_change\" to true and move the story to a new place or situation grounded in what happened."
               if length >= budget else "Advance one new visible consequence without restaging what is done.")
    cast = "\n".join(f"- {name}: {look}" for name, look in (run.get("cast") or {}).items())
    watched = previous.get("watch") or {}
    said = (previous.get("transcript") or "").strip()
    return (seed_text(run["seed"], s.get("style", "")) + "\n"
            + (f"\nCast -- repeat these looks word for word:\n{cast}\n" if cast else "")
            + f"\nBeats asked for so far (requests, not verified events; do not repeat them):\n{beats}\n"
            + f"\nYou are writing segment {index}, about {seconds:.0f} seconds. {pacing}\n"
            + f"\nThe previous segment ({previous.get('scene')}) was asked to show: {previous.get('fields', {}).get('action', '')}\n"
            + f"What the watcher saw it actually show: {watched.get('summary') or 'no report'}\n"
            + f"How it ends, on its last frame: {watched.get('ending') or 'no report'}\n"
            + (f"What was heard: {said}\n" if said else "")
            + (f"\nMemory of the film so far:\n{memory_text}\n" if memory_text else "")
            + (f"\n{note}\n" if note else "")
            + direction(s, run.get("branch"), index)
            + "\nContinue from what really came out, above all the state the last frame leaves things in. Use the memory to keep "
              "people, places and continuity consistent and to avoid repeating what already happened; do not describe the memory itself.")


def watch_prompt(run: dict, seg: dict, view: dict, older: int, transcript: str) -> str:
    cast = "\n".join(f"- {name}: {look}" for name, look in (run.get("cast") or {}).items()) or "- (no pinned looks)"
    start = float(seg.get("start") or 0)
    return (f"The images are frames of segment {seg['index']} of the film, in time order, covering {mmss(start)} to "
            f"{mmss(start + float(seg.get('seconds') or 0))} on the film's clock; the last image is its final frame.\n"
            + (f"What is heard in it (speech to text, may be rough): {transcript}\n" if transcript else "What is heard in it: no speech.\n")
            + f"\nThe segment was ASKED to show (a request, not a fact): {seg.get('fields', {}).get('action', '')}\n"
            + f"\nThe cast's pinned looks:\n{cast}\n"
            + f"\nThe scene graph so far (every node and the {RECENT_EVENTS} latest events; {older} older events are kept but not shown):\n"
            + json.dumps(view, ensure_ascii=False, indent=1)
            + "\n\nReport ONLY what this segment adds or changes, as this JSON:\n"
            '{"add_nodes": [{"id": "<short unique id>", "type": "character|place|object", "label": "<3 words max>", "state": "<12 words max>"}], '
            '"update_nodes": [{"id": "<existing id>", "state": "<12 words max>"}], '
            '"add_events": [{"t": "MM:SS", "subject": "<node id>", "action": "<verb phrase, 8 words max>", "target": "<node id or empty>", "place": "<node id>"}], '
            '"summary": "<25 words max: what actually happened>", '
            '"ending": "<40 words max: who and what is where on the final frame, poses, what they hold, the light>", '
            '"continuity": [{"kind": "identity|costume|object|place|other", "who": "<node id or name>", "note": "<15 words max>"}], '
            '"identity_drift": true | false}\n\n'
            "Rules that matter:\n"
            "- A node is a person, a place or a thing that persists. Never a node for an action, a shot or a moment. The "
            "same person older or the same crowd elsewhere keeps its node: reuse the id and update its state.\n"
            "- \"state\" replaces the old state: how it is NOW, twelve words.\n"
            "- At most TWO events, and only events that change the story: something gained or lost, a relationship turns, a "
            "place left for good, a goal met or broken. Glances, smiles, steps and camera moves are not events.\n"
            "- \"continuity\" lists only real problems you can see: a face that changes into another person's, clothing that "
            "differs from the pinned look or from earlier frames, an object that vanishes or changes, a jump in place. Empty when none.\n"
            "- \"identity_drift\" is true only when a pinned character's face, age or build clearly becomes someone else's, "
            "or melts. Turning away, distance, motion blur or new lighting is not drift.\n"
            "- Describe what the frames show, not what was asked for. Any list may be empty.")


def compact_prompt(events: list, chapters: list) -> str:
    lines = "\n".join(f"  {e.get('t', '--:--')} {e.get('subject', '')} {e.get('action', '')} {e.get('target', '')}".rstrip() for e in events)
    seen = "; ".join(c.get("title", "") for c in chapters) or "(none yet)"
    return (f"These are the events of one stretch of a film, oldest first:\n{lines}\n\nChapters already summarised: {seen}.\n\n"
            'Fold this stretch into a single chapter. Answer with:\n{"title": "<at most 5 words>", "summary": "<at most 30 words, '
            'what this stretch accomplished in the story>", "carried_forward": ["<at most 3 items, each at most 10 words: a '
            'consequence that still matters later>"]}\n\nWrite what changed, not what was shown. Anything left out is gone from the memory.')


def stagnation_note(found: dict) -> str:
    return ("The film is circling: " + found["reason"] + f" (segments {', '.join(map(str, found['segments']))}). The next segment MUST "
            "set scene_change to true, use a different scene name, and complete a visible change of situation chosen by you.")


# ---------- the brains ----------

def ask_text(run: dict, system: str, prompt: str) -> str:
    b = run["settings"].get("brain") or {}
    return slate.ask(b.get("backend") or slate.AUTO, system, prompt, "top", b.get("endpoint") or "", b.get("model") or "")


def ask_vision(system: str, frames: list, sheet: Path, prompt: str, brain: dict) -> tuple[str, str]:
    """The watcher: the vision server (cosmos.py) with every frame, else the QA gate's vision path on the sheet."""

    if cosmos.settings()["endpoint"]:
        try:
            return cosmos.look(system, frames, sheet, prompt)
        except LookupError:
            pass  # the Studio's Cosmos is not installed or has no room: the QA gate's eyes instead
    note = "The picture is a contact sheet of the frames, left to right then top to bottom, in time order: " + "; ".join(c for c, _ in frames) + ".\n\n"
    return qa.look(sheet, brain, system + "\n\n" + note + prompt)


def ask_json(asker, check, prompt: str, what: str) -> tuple[dict, list[str]]:
    """A brain asked for JSON, again with the reason when the answer does not hold up: (answer, the reasons turned back)."""

    problems, asked = [], prompt
    for _ in range(MAX_ATTEMPTS):
        try:
            data = slate.extract_json(asker(asked))
            problem = check(data) if isinstance(data, dict) else "the answer is not a JSON object"
        except ValueError as error:
            problem = str(error)
        except Exception as error:  # noqa: BLE001 - a brain that does not answer stops the run, to be resumed
            raise Failed(f"the {what} did not answer: {str(error)[:300]}")
        if not problem:
            return data, problems
        problems.append(problem)
        asked = prompt + f"\n\nYOUR LAST ANSWER COULD NOT BE USED: {problem}. Answer again with the JSON only."
    raise Failed(f"the {what}'s answer could not be used {MAX_ATTEMPTS} times: {problems[-1]}")


# ---------- the run's files ----------

def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id or ""):
        raise HTTPException(400, "bad run id")
    return HOME / "autorl" / run_id


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(path.suffix + ".tmp")
    scratch.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    scratch.replace(path)


def load(run_id: str) -> dict:
    run = read_json(run_dir(run_id) / "run.json")
    if not run:
        raise HTTPException(404, "no such run")
    return run


def save(run: dict) -> None:
    with guard:
        run["rev"] = int(run.get("rev") or 0) + 1
        run["updated"] = slate.now()
        write_json(run_dir(run["id"]) / "run.json", run)


def say(run: dict, line: str, **current) -> None:
    run.setdefault("log", []).append({"at": slate.now(), "text": line})
    del run["log"][:-LOG]
    run["current"] = {"text": line, **current}
    save(run)


def memory_of(run: dict) -> dict:
    return read_json(run_dir(run["id"]) / "memory.json", copy.deepcopy(EMPTY))


def committed(run: dict) -> list:
    return [s for s in run.get("segments") or [] if s.get("status") == "done"]


def relative(path: Path) -> str:
    return str(path.relative_to(HOME))


def on_disk(path: str | None) -> bool:
    if not path:
        return False
    target = (HOME / path).resolve()
    return HOME.resolve() in target.parents and target.is_file()


# ---------- the seed ----------

def clean_seed(given: dict) -> dict:
    if not isinstance(given, dict):
        raise HTTPException(400, "seed is an object: world, situation, characters")
    characters = []
    for i, c in enumerate(given.get("characters") or []):
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        image = str(c.get("image") or "").strip()
        if image and not on_disk(image):
            raise HTTPException(400, f"the picture of {c['name']} is not a file in the studio's folder: {image}")
        characters.append({"id": re.sub(r"[^a-z0-9]+", "_", str(c["name"]).lower()).strip("_") or f"c{i + 1}",
                           "name": str(c["name"]).strip()[:80], "look": str(c.get("look") or "").strip()[:600], "image": image or None})
    seed = {"inspiration": str(given.get("inspiration") or "").strip()[:600], "world": str(given.get("world") or "").strip()[:1500],
            "situation": str(given.get("situation") or "").strip()[:1500], "characters": characters[:9],
            "opening_image": str(given.get("opening_image") or "").strip() or None, "slate": given.get("slate") or None}
    if seed["opening_image"] and not on_disk(seed["opening_image"]):
        raise HTTPException(400, "the opening picture is not a file in the studio's folder")
    if not (seed["world"] or seed["situation"] or seed["inspiration"]):
        raise HTTPException(400, "give the story a seed: a world, where it starts, or an inspiration")
    return seed


@router.get("/api/autorl/seed/slate/{project}")
def seed_from_slate(project: str) -> dict:
    """A seed from a Slate film's bible: its world and logline, its characters' looks and pinned stills, its look."""

    film = slate.get_project(project)
    places = [l for l in film.get("locations") or [] if (l.get("description") or l.get("name"))]
    world = " ".join(bit for bit in (film.get("world") or "", *(slate.sentence(f"{l.get('name')}: {l.get('description') or ''}".strip(': ')) for l in places[:3])) if bit)
    characters = [{"name": c.get("name"), "look": slate.person(c), "image": (slate.stills_of(c) or [None])[0]}
                  for c in film.get("characters") or [] if c.get("name")]
    look = next((slate.sentence("; ".join(bit for bit in (s.get("tone"), s.get("palette"), s.get("lighting")) if bit))
                 for s in film.get("lookbook") or [] if s.get("tone") or s.get("palette")), "")
    return {"seed": {"world": world.strip(), "situation": film.get("logline") or "", "characters": characters,
                     "inspiration": film.get("name") or "", "slate": film.get("id")},
            "style": look, "name": film.get("name") or "Slate film"}


def clean_settings(given: dict, base: dict | None = None) -> dict:
    s = copy.deepcopy(base or DEFAULTS)
    given = given if isinstance(given, dict) else {}
    for key in ("style", "guidance", "music"):
        if given.get(key) is not None:
            s[key] = str(given[key]).strip()[:2000]
    for key in ("preset", "quality"):
        if given.get(key):
            s[key] = str(given[key])
    for key, low, high in (("seconds", 1, 15), ("segments", 0, LIMIT), ("seed", 0, 2 ** 31 - 1), ("max_scene_segments", 2, 50),
                           ("beat_history", 3, 50), ("retakes", 0, 3), ("preview_every", 1, 50), ("width", 256, 1536), ("height", 256, 1536)):
        if given.get(key) not in (None, ""):
            try:
                s[key] = max(low, min(high, int(given[key])))
            except (TypeError, ValueError):
                raise HTTPException(400, f"{key} is a whole number from {low} to {high}")
    for key in ("qa", "ref_turbo"):
        if given.get(key) is not None:
            s[key] = bool(given[key])
    if given.get("identity") in IDENTITY:
        s["identity"] = given["identity"]
    if given.get("director") in ("brain", "vision"):
        s["director"] = given["director"]
    if isinstance(given.get("loras"), list):
        s["loras"] = slate.lora_list(given["loras"])[:4]
    if isinstance(given.get("brain"), dict):
        b = given["brain"]
        s["brain"] = {"backend": str(b.get("backend") or slate.AUTO), "endpoint": slate.nearby_endpoint(str(b.get("endpoint") or "")),
                      "model": str(b.get("model") or "")}
    return s


# ---------- the routes ----------

def public(run: dict) -> dict:
    live = runs.get(run["id"])
    return {**run, "running": bool(live and live["thread"].is_alive())}


@router.get("/api/autorl/runs")
def list_runs() -> list[dict]:
    out = []
    for file in sorted((HOME / "autorl").glob("*/run.json"), key=lambda f: -f.stat().st_mtime):
        run = read_json(file)
        if not run:
            continue
        done = committed(run)
        out.append({"id": run["id"], "name": run.get("name", ""), "state": run.get("state"), "segments": len(done),
                    "seconds": round(sum(float(s.get("seconds") or 0) for s in done), 2), "updated": run.get("updated"),
                    "branch": run.get("branch"), "running": bool(runs.get(run["id"]) and runs[run["id"]]["thread"].is_alive())})
    return out


@router.post("/api/autorl/runs")
def create_run(body: dict, request: Request) -> dict:
    """A new run: {name, seed: {world, situation, inspiration, characters: [{name, look, image}], opening_image},
    settings: {...DEFAULTS}, start: true to begin at once}."""

    import agents

    agents.local(request, True)  # the Director and the watcher run the user's brains: the same check as the Director's
    run_id = time.strftime("r%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    seed = clean_seed(body.get("seed") or {})
    settings = clean_settings(body.get("settings") or {})
    run = {"id": run_id, "name": (str(body.get("name") or "").strip() or "AutoRL film")[:120], "created": slate.now(),
           "state": "idle", "error": "", "seed": seed, "settings": settings, "segments": [], "branch": None, "film": None,
           "cast": {c["name"]: c["look"] for c in seed["characters"] if c.get("look")}, "log": [], "current": {}, "rev": 0}
    folder = run_dir(run_id)
    folder.mkdir(parents=True)
    write_json(folder / "memory.json", copy.deepcopy(EMPTY))
    write_json(folder / "scene-graph.schema.json", SCHEMA)
    say(run, "Created.")
    if body.get("start"):
        return public(begin(run))
    return public(run)


@router.get("/api/autorl/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = load(run_id)
    return {**public(run), "memory": memory_of(run)}


@router.put("/api/autorl/runs/{run_id}")
def change_settings(run_id: str, body: dict, request: Request) -> dict:
    """New settings or a new name between segments: they apply from the next segment on."""

    import agents

    agents.local(request, True)
    live = runs.get(run_id)
    run = live["run"] if live and live["thread"].is_alive() else load(run_id)  # the loop reads them at its next segment
    with guard:
        if body.get("settings") is not None:
            run["settings"] = clean_settings(body["settings"], run["settings"])
        if body.get("name"):
            run["name"] = str(body["name"]).strip()[:120]
        save(run)
    return public(run)


def begin(run: dict) -> dict:
    with starting:
        live = runs.get(run["id"])
        if live and live["thread"].is_alive():
            raise HTTPException(409, "this run is already going")
        run["state"], run["error"], run["pause"] = "running", "", False
        for seg in run.get("segments") or []:  # a segment that failed is planned again on resume, at its own seed
            if seg.get("status") == "failed":
                seg["status"], seg["job"], seg["error"] = "planned", None, ""
        say(run, "Started." if not committed(run) else f"Resumed after segment {len(committed(run))}.")
        stop = threading.Event()
        thread = threading.Thread(target=work, args=(run, stop), daemon=True, name="autorl-" + run["id"])
        runs[run["id"]] = {"thread": thread, "stop": stop, "run": run}
        thread.start()
        return run


@router.post("/api/autorl/runs/{run_id}/start")
def start_run(run_id: str, request: Request) -> dict:
    """Start, or resume a paused, stopped or failed run where it stopped."""

    import agents

    agents.local(request, True)
    return public(begin(load(run_id)))


@router.post("/api/autorl/runs/{run_id}/pause")
def pause_run(run_id: str, request: Request) -> dict:
    """Pause after the segment at work: it renders, is checked and joins the film; then the loop waits."""

    import agents

    agents.local(request, True)
    live = runs.get(run_id)
    if not live or not live["thread"].is_alive():
        return public(load(run_id))
    live["run"]["pause"] = True
    say(live["run"], "Pausing after this segment.")
    return public(live["run"])


@router.post("/api/autorl/runs/{run_id}/stop")
def stop_run(run_id: str, request: Request) -> dict:
    """Stop now: a segment still waiting in the queue is taken out (it is planned again on resume); one rendering
    finishes and is picked up on resume."""

    import agents

    agents.local(request, True)
    live = runs.get(run_id)
    if live and live["thread"].is_alive():
        live["stop"].set()
        return {"stopping": True}
    run = load(run_id)
    if run.get("state") in ACTIVE:
        run["state"] = "paused"
        say(run, "Stopped.")
    return public(run)


@router.post("/api/autorl/runs/{run_id}/branch")
def branch_run(run_id: str, body: dict, request: Request) -> dict:
    """A new run that shares segments 1..``after`` of this one (their clips stay where they are), with the memory as
    it was after segment ``after``, and goes on with new ``guidance``. {after, guidance, name?, segments?, seed?, start?}"""

    import agents

    agents.local(request, True)
    source = load(run_id)
    done = committed(source)
    try:
        after = int(body.get("after"))
    except (TypeError, ValueError):
        raise HTTPException(400, "after is the number of the segment to branch from")
    if not 1 <= after <= len(done):
        raise HTTPException(400, f"branch from a committed segment, 1 to {len(done)}")
    guidance = str(body.get("guidance") or "").strip()
    if not guidance:
        raise HTTPException(400, "give the branch its direction: what should happen from here")
    snapshot = read_json(run_dir(run_id) / "memory" / f"S{after:04d}.json")
    if not snapshot:
        raise HTTPException(409, f"segment {after} has no memory snapshot to branch from")
    new_id = time.strftime("r%m%d-%H%M%S-") + uuid.uuid4().hex[:5]
    settings = clean_settings({k: body[k] for k in ("segments", "seed") if body.get(k) not in (None, "")}, source["settings"])
    if body.get("segments") in (None, "") and settings["segments"]:
        settings["segments"] = after + max(1, settings["segments"] - after)
    elif settings["segments"]:
        settings["segments"] = after + settings["segments"]  # a number given is how many new segments
    shared = [{**copy.deepcopy(s), "shared": run_id} for s in done[:after]]
    run = {"id": new_id, "name": (str(body.get("name") or "").strip() or f"{source.get('name')} · branch at {after}")[:120],
           "created": slate.now(), "state": "idle", "error": "", "seed": copy.deepcopy(source["seed"]), "settings": settings,
           "segments": shared, "branch": {"from": run_id, "after": after, "guidance": guidance[:2000]}, "film": None,
           "cast": copy.deepcopy(snapshot.get("cast") or source.get("cast") or {}), "log": [], "current": {}, "rev": 0}
    folder = run_dir(new_id)
    folder.mkdir(parents=True)
    memory = {key: copy.deepcopy(snapshot.get(key, EMPTY[key])) for key in EMPTY}
    write_json(folder / "memory.json", memory)
    for seg in shared:
        mark = read_json(run_dir(run_id) / "memory" / f"S{seg['index']:04d}.json")
        if mark:
            write_json(folder / "memory" / f"S{seg['index']:04d}.json", mark)
    write_json(folder / "scene-graph.schema.json", SCHEMA)
    say(run, f"Branched from {source.get('name')} after segment {after}: {guidance[:200]}")
    if body.get("start"):
        return public(begin(run))
    return public(run)


@router.post("/api/autorl/runs/{run_id}/assemble")
def assemble_now(run_id: str, request: Request) -> dict:
    import agents

    agents.local(request, True)
    live = runs.get(run_id)
    run = live["run"] if live and live["thread"].is_alive() else load(run_id)  # the loop's own copy while it runs
    if not committed(run):
        raise HTTPException(400, "no segment is finished yet")
    publish(run)
    return public(run)


@router.get("/api/autorl/vision")
def vision_state() -> dict:
    return cosmos.summary()


@router.post("/api/autorl/vision")
def vision_save(body: dict, request: Request) -> dict:
    """{endpoint: "studio:cosmos" | a nearby http://host:port | "" (the QA gate's eyes), model, frames}"""

    import agents

    agents.local(request, True)
    endpoint = str(body.get("endpoint") if body.get("endpoint") is not None else cosmos.settings()["endpoint"]).strip()
    if endpoint and endpoint.lower() != cosmos.STUDIO:
        slate.nearby_endpoint(endpoint)  # 400 unless it is on this computer, this network or the tailnet
    try:
        frames = int(body["frames"]) if body.get("frames") not in (None, "") else None
    except (TypeError, ValueError):
        raise HTTPException(400, "frames is a whole number")
    cosmos.save_settings({"endpoint": endpoint, "model": str(body.get("model") or ""), **({"frames": frames} if frames else {})})
    return cosmos.summary()


def resume_all() -> None:
    for file in (HOME / "autorl").glob("*/run.json"):
        run = read_json(file)
        if run and run.get("state") in ACTIVE and run["id"] not in runs:
            try:
                say(run, "Picked up after a restart.")
                begin(run)
            except HTTPException:
                pass


# ---------- the loop ----------

def wait(stop: threading.Event, seconds: float | None = None) -> None:
    if stop.wait(TICK if seconds is None else seconds):
        raise Stopped()


def work(run: dict, stop: threading.Event) -> None:
    try:
        while True:
            if stop.is_set():
                raise Stopped()
            if run.get("pause"):
                run["state"], run["pause"] = "paused", False
                publish(run)
                say(run, f"Paused after segment {len(committed(run))}. Resume to carry on.")
                return
            limit = int(run["settings"].get("segments") or 0)
            if limit and len(committed(run)) >= limit:
                publish(run)
                run["state"] = "done"
                say(run, f"Done: {limit} segments, {run['film']['seconds'] if run.get('film') else 0:.1f} s of film.")
                return
            segment(run, stop)
            n = len(committed(run))
            if n == 1 or n % int(run["settings"].get("preview_every") or 3) == 0:
                publish(run)
    except Stopped:
        pending = next((s for s in run.get("segments") or [] if s.get("status") == "queued"), None)
        if pending and (job_of(pending.get("job") or "") or {}).get("status") == "queued":
            cancel(pending["job"])
            pending["status"], pending["job"] = "planned", None
        run["state"] = "paused"
        say(run, "Stopped. Resume to carry on from here: every finished segment and the memory are kept.")
    except Exception as error:  # noqa: BLE001 - the reason goes to the page; resume tries again
        run["state"], run["error"] = "failed", str(getattr(error, "detail", error))[:600]
        say(run, "Stopped on a problem: " + run["error"])


def segment(run: dict, stop: threading.Event) -> None:
    """One segment from plan to commit; a segment left half-way (a stop, a restart) is picked up where it was."""

    seg = next((s for s in run["segments"] if s.get("status") not in ("done",)), None)
    if seg is None:
        seg = plan(run)
    while seg["status"] != "done":
        if seg["status"] == "planned":
            render(run, seg)
        elif seg["status"] == "queued":
            follow(run, seg, stop)
        elif seg["status"] == "rendered":
            check(run, seg)
        elif seg["status"] == "retake":
            seg["seed"] = int(seg["seed"]) + 1
            seg["status"] = "planned"
            save(run)
        elif seg["status"] == "failed":
            raise Failed(f"segment {seg['index']} could not be rendered: {seg.get('error') or 'no reason'}")


# ---------- plan ----------

def frames_of(settings: dict) -> int:
    return lengths.nearest(float(settings.get("seconds") or 5))


def plan(run: dict) -> dict:
    s, done = run["settings"], committed(run)
    index = len(done) + 1
    frames = frames_of(s)
    seconds = lengths.seconds_of(frames)
    run["state"] = "running"
    say(run, f"Segment {index}: the Director writes it.", stage="director", segment=index)
    if index == 1:
        prompt = first_prompt(run, seconds)
        stuck = None
    else:
        memory = memory_of(run)
        stuck = stagnation(done)
        prompt = next_prompt(run, index, seconds, as_text(memory["graph"]), stagnation_note(stuck) if stuck else "")

    def check_answer(data: dict) -> str:
        return director_problem(run, data, index, stuck)

    asker = (lambda text: ask_vision_text(run, prompt_with_system(text))) if s.get("director") == "vision" else (lambda text: ask_text(run, DIRECTOR_SYSTEM, text))
    data, rejected = ask_json(asker, check_answer, prompt, "Director")
    shot = data["shot"]
    if index == 1:
        data["scene_change"] = True
    for person in data.get("new_characters") or []:
        if isinstance(person, dict) and person.get("name") and person.get("look"):
            run.setdefault("cast", {}).setdefault(str(person["name"]).strip(), str(person["look"]).strip())
    seg = {"index": index, "id": f"S{index:04d}", "status": "planned", "scene": str(data["scene"]).strip()[:80],
           "scene_change": bool(data["scene_change"]), "beat": str(data["beat"]).strip()[:200],
           "fields": {"setting": str(shot.get("setting") or ""), "camera": str(shot.get("camera") or ""), "action": str(shot.get("action") or ""),
                      "dialogue": [d for d in shot.get("dialogue") or [] if isinstance(d, dict) and str(d.get("text") or "").strip()][:3],
                      "soundscape": str(shot.get("soundscape") or ""), "music": str(shot.get("music") or "N/A")},
           "rejected": rejected, "seed": int(s.get("seed") or 0) + index - 1, "frames": frames, "seconds": seconds,
           "start": round(sum(float(x.get("seconds") or 0) for x in done), 3), "takes": [], "job": None, "output": None,
           "planned": slate.now()}
    folder = run_dir(run["id"]) / "segments" / seg["id"]
    write_json(folder / "director.json", {"prompt": prompt, "answer": data, "rejected": rejected})
    run["segments"].append(seg)
    if rejected:
        say(run, f"Segment {index}: {len(rejected)} answer{'s' if len(rejected) != 1 else ''} of the Director turned back ({rejected[-1][:120]}).", segment=index)
    say(run, f"Segment {index}: {seg['beat']}", stage="planned", segment=index)
    return seg


def prompt_with_system(text: str) -> str:
    return DIRECTOR_SYSTEM + "\n\n" + text


def ask_vision_text(run: dict, text: str) -> str:
    """The Director on the vision server, from text alone (``director: vision``)."""

    with cosmos.request() as (endpoint, model):
        return cosmos.chat(endpoint, model, "Answer with JSON only.", [], text)


def director_problem(run: dict, data: dict, index: int, stuck) -> str:
    """Why a Director answer cannot be used, or "" when it can."""

    shot = data.get("shot")
    if not isinstance(shot, dict) or not str(shot.get("action") or "").strip():
        return '"shot" is an object with at least "action"'
    if not str(data.get("scene") or "").strip() or not str(data.get("beat") or "").strip():
        return '"scene" and "beat" are short lines of text'
    if not isinstance(data.get("scene_change"), bool):
        return '"scene_change" is true or false'
    fields = {"scene": str(shot.get("setting") or ""), "camera": str(shot.get("camera") or ""), "action": str(shot.get("action") or ""),
              "soundscape": str(shot.get("soundscape") or ""), "dialogue": [d for d in shot.get("dialogue") or [] if isinstance(d, dict)]}
    named = negation.check_fields(fields)["negations"]
    if named:
        return (f"\"{named[0]['phrase']}\" names what must not be seen; the video model has no negative prompt and what is "
                "named tends to appear. Say what is there instead")
    if index == 1:
        return ""
    done = committed(run)
    previous = done[-1]
    action = fields["action"].strip().lower()
    if difflib.SequenceMatcher(None, action, str(previous["fields"].get("action") or "").strip().lower()).ratio() >= SIMILAR_ACTION:
        return "the action repeats the segment before almost word for word; move the story one visible step on"
    beat = str(data["beat"]).strip().lower()
    for seg in done[-int(run["settings"].get("beat_history") or 12):]:
        if difflib.SequenceMatcher(None, beat, str(seg.get("beat") or "").strip().lower()).ratio() >= SIMILAR_BEAT:
            return f"the beat restages segment {seg['index']} (\"{seg.get('beat')}\"); choose a materially different consequence"
    budget = int(run["settings"].get("max_scene_segments") or 8)
    if scene_length(done) >= budget and not data["scene_change"]:
        return f"the scene {previous.get('scene')!r} has used its {budget}-segment budget: set scene_change to true and move on"
    if stuck and (not data["scene_change"] or str(data["scene"]).strip().lower() == str(previous.get("scene") or "").strip().lower()):
        return "the film is circling: set scene_change to true with a different scene name"
    return ""


# ---------- render ----------

def references_for(run: dict) -> list[str]:
    return [c["image"] for c in run["seed"].get("characters") or [] if c.get("image") and on_disk(c["image"])][:9]


def mode_of(run: dict, seg: dict) -> str:
    """How a segment opens: on the segment before (``previous``), the cast's pictures (``references``), the opening
    picture (``image``) or from text."""

    identity = run["settings"].get("identity", "auto")
    pictures = bool(references_for(run)) and identity != "frame" and ref2va()
    if seg["index"] > 1 and not seg["scene_change"]:
        return "previous"
    if pictures:
        return "references"
    if seg["index"] == 1 and run["seed"].get("opening_image"):
        return "image"
    return "text"


def look_lines(run: dict, fields: dict) -> str:
    """The pinned looks of the cast this segment names, said every time: the renderer has no memory."""

    text = " ".join(str(fields.get(k) or "") for k in ("setting", "action")).lower()
    return " ".join(f"{name} is {look.rstrip('.')}." for name, look in (run.get("cast") or {}).items()
                    if look and name.lower().split()[0] in text)


def prompt_of(run: dict, seg: dict, mode: str) -> str:
    s, f = run["settings"], seg["fields"]
    style = s.get("style", "").strip()
    music = f.get("music") or "N/A"
    if s.get("music") and music.upper() == "N/A":
        music = s["music"]
    dialogue = [{"speaker": d.get("speaker") or "", "voice": d.get("voice") or "a natural voice", "text": str(d["text"]).strip(), "language": "English"}
                for d in f.get("dialogue") or []]
    if mode == "references":
        pictured = [c for c in run["seed"]["characters"] if c.get("image") and on_disk(c["image"])][:9]
        names = [c["name"] for c in pictured]
        for line in dialogue:
            line["subject"] = names.index(line["speaker"]) + 1 if line["speaker"] in names else 1
        fields = {"scene": " ".join(bit for bit in (style, f.get("setting", "")) if bit), "camera": f.get("camera", ""),
                  "action": f.get("action", ""), "dialogue": dialogue, "soundscape": f.get("soundscape", ""), "music": music,
                  "subjects": names, "people": True}
        return compose_references(fields, len(names))
    scene = " ".join(bit for bit in (style, look_lines(run, f), f.get("setting", "")) if bit)
    fields = {"scene": scene, "camera": f.get("camera", ""), "action": f.get("action", ""), "dialogue": dialogue,
              "soundscape": f.get("soundscape", ""), "music": music}
    return compose(fields, mode in ("previous", "image"), frames=seg["frames"])


def render(run: dict, seg: dict) -> None:
    s = run["settings"]
    done = committed(run)
    mode = mode_of(run, seg)
    take = len(seg["takes"]) + 1
    output = f"autorl/{run['id']}/segments/{seg['id']}/take{take}.mp4"
    prompt = prompt_of(run, seg, mode)
    params = {"prompt": prompt, "preset": s["preset"], "frames": seg["frames"], "seed": int(seg["seed"]), "output": output,
              "qa": False,  # the gate runs here, with the watcher, once per take (check)
              "brain": s.get("brain") or {}, "autorl": {"run": run["id"], "segment": seg["index"]}}
    if s["preset"] == "custom" and s.get("width") and s.get("height"):
        params.update(width=int(s["width"]), height=int(s["height"]))
    if mode == "references":
        params["references"] = references_for(run)
        if s.get("ref_turbo") and ref2va_turbo():
            params["ref_turbo"] = True
    else:
        params["quality"] = s["quality"]
        if s.get("loras"):
            params["loras"] = s["loras"]
    if mode == "previous":
        params["first_frame_from"] = done[-1]["output"]
    if mode == "image":
        params["image"] = run["seed"]["opening_image"]
    try:
        job = submit({"kind": "video", "title": f"AutoRL · {run['name']} · {seg['index']}: {seg['beat'][:60]}", "params": params})
    except HTTPException as error:
        raise Failed(f"the queue refused segment {seg['index']}: {error.detail}") from None
    seg.update(status="queued", job=job["id"], mode=mode, prompt=prompt, output=output,
               settings={k: params.get(k) for k in ("preset", "quality", "frames", "width", "height", "loras", "ref_turbo") if params.get(k) is not None})
    say(run, f"Segment {seg['index']}: rendering" + (f", take {take} at seed {seg['seed']}" if take > 1 else "")
        + {"previous": ", from the last frame of the segment before", "references": ", from the cast's pictures (a new scene)",
           "image": ", from the opening picture", "text": ", from text (a new scene)"}[mode] + ".", stage="render", segment=seg["index"], job=job["id"])


def follow(run: dict, seg: dict, stop: threading.Event) -> None:
    while True:
        job = job_of(seg.get("job") or "")
        if job is None:  # the app restarted: the clip is there, or the take is queued again
            seg["status"] = "rendered" if on_disk(seg.get("output")) else "planned"
            save(run)
            return
        status = job.get("status")
        if status == "done":
            seg["output"] = (job.get("outputs") or [seg["output"]])[0]
            seg["status"] = "rendered" if on_disk(seg["output"]) else "failed"
            seg["error"] = "" if seg["status"] == "rendered" else "the job finished without its clip"
            save(run)
            return
        if status == "cancelled":
            seg["status"], seg["job"] = "planned", None
            save(run)
            raise Stopped()
        if status == "failed":
            seg["status"], seg["error"] = "failed", job.get("error") or "the render failed"
            save(run)
            return
        wait(stop)


# ---------- check and watch ----------

def sample(clip: Path, folder: Path, count: int) -> tuple[list[tuple[str, Path]], Path, float]:
    """``count`` frames evenly through the clip and its last frame, 448x256 PNG, with captions; and one sheet of them."""

    length = qa.duration(clip)
    folder.mkdir(parents=True, exist_ok=True)
    times = [round(i * max(length - 0.1, 0) / max(count - 1, 1), 2) for i in range(count)]
    frames = []
    for i, at in enumerate(times):
        target = folder / f"f{i + 1:02d}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(at), "-i", str(clip), "-frames:v", "1", "-vf",
                        f"scale={cosmos.FRAME[0]}:{cosmos.FRAME[1]}", str(target)], check=True, env=qa.ENV, timeout=60)
        frames.append((f"Frame {i + 1}, at {at:.2f} s", target))
    last = folder / "last.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-sseof", "-0.1", "-i", str(clip), "-update", "1", "-vf",
                    f"scale={cosmos.FRAME[0]}:{cosmos.FRAME[1]}", str(last)], check=True, env=qa.ENV, timeout=60)
    frames.append((f"Final frame, at {length:.2f} s", last))
    sheet = folder / "sheet.png"
    paths = [p for _, p in frames]
    while len(paths) % 4:
        paths.append(paths[-1])
    command = ["ffmpeg", "-v", "error", "-y"]
    for p in paths:
        command += ["-i", str(p)]
    rows = len(paths) // 4
    graph = "".join(f"{''.join(f'[{r * 4 + c}:v]' for c in range(4))}hstack=4[r{r}];" for r in range(rows))
    graph += "".join(f"[r{r}]" for r in range(rows)) + (f"vstack={rows}[out]" if rows > 1 else "copy[out]")
    subprocess.run(command + ["-filter_complex", graph, "-map", "[out]", "-frames:v", "1", str(sheet)], check=True, env=qa.ENV, timeout=120)
    return frames, sheet, length


def watcher_brain(run: dict) -> dict:
    """The QA gate's vision model: the vision server when one is set (a local backend), else the film's brain."""

    said = cosmos.settings()
    if said["endpoint"]:
        if said["endpoint"].lower() != cosmos.STUDIO:
            return {"backend": "local", "endpoint": said["endpoint"], "model": said["model"]}
        if cosmos.installed():
            return {"backend": "local", "endpoint": cosmos.STUDIO, "model": cosmos.SERVED}
    return run["settings"].get("brain") or {}


def gate(clip: Path, prompt: str, sheet: Path, brain: dict) -> dict:
    """qa.check with the Studio's Cosmos as its eyes when that is the vision brain (loaded for the call)."""

    if brain.get("endpoint") == cosmos.STUDIO:
        try:
            with cosmos.request() as (endpoint, model):
                return qa.check(clip, prompt, sheet, {"backend": "local", "endpoint": endpoint, "model": model})
        except LookupError:
            brain = {}
    return qa.check(clip, prompt, sheet, brain)


def check_watch(data: dict) -> str:
    for key in ("add_nodes", "update_nodes", "add_events", "continuity"):
        if data.get(key) is not None and not isinstance(data.get(key), list):
            return f'"{key}" is a list'
    if not isinstance(data.get("identity_drift"), bool):
        return '"identity_drift" is true or false'
    if not str(data.get("ending") or "").strip():
        return '"ending" says what the final frame shows'
    return ""


def check(run: dict, seg: dict) -> None:
    """The take judged: the QA gate (Whisper line check, identity sheet) and the watcher (scene-graph delta,
    continuity). A take that fails either is rendered again at the next seed while ``retakes`` allow; the last take
    stands otherwise, the reason said beside it."""

    s = run["settings"]
    clip = HOME / seg["output"]
    folder = run_dir(run["id"]) / "segments" / seg["id"]
    take = len(seg["takes"]) + 1
    say(run, f"Segment {seg['index']}: checking and watching take {take}.", stage="watch", segment=seg["index"])
    report = {"passed": None, "skipped": True, "note": "the QA gate is off for this run"}
    transcript = ""
    if s.get("qa", True) and qa.ready()["runs"]:
        try:
            report = gate(clip, seg.get("prompt") or "", folder / f"qa-take{take}.png", watcher_brain(run))
            transcript = ((report.get("audio") or {}).get("heard") or "").strip()
        except Exception as error:  # noqa: BLE001 - the clip is made; a gate that cannot run does not lose it
            report = {"passed": None, "error": str(error)[-300:]}
    elif qa.ready()["transcript"]:
        try:
            transcript = (qa.transcribe(clip) or "").strip()
        except Exception:  # noqa: BLE001
            transcript = ""
    frames, sheet, length = sample(clip, folder / f"frames-take{take}", cosmos.settings()["frames"])
    memory = memory_of(run)
    graph = memory["graph"]
    view = {"nodes": graph["nodes"], "events": graph["events"][-RECENT_EVENTS:], "chapters": graph.get("chapters") or []}
    prompt = watch_prompt(run, {**seg, "seconds": length}, view, max(0, len(graph["events"]) - RECENT_EVENTS), transcript)
    who = []

    def asker(text: str) -> str:
        answer, by = ask_vision(WATCH_SYSTEM, frames, sheet, text, run["settings"].get("brain") or {})
        who.append(by)
        return answer
    watched, rejected = ask_json(asker, check_watch, prompt, "watcher")
    watched = {"delta": {k: watched.get(k) or [] for k in ("add_nodes", "update_nodes", "add_events")},
               "summary": clip_words(watched.get("summary"), 40), "ending": clip_words(watched.get("ending"), 60),
               "continuity": [c for c in watched.get("continuity") or [] if isinstance(c, dict)][:6],
               "identity_drift": watched["identity_drift"], "by": who[-1] if who else "", "rejected": rejected,
               "sheet": relative(sheet), "frames": [relative(p) for _, p in frames]}
    write_json(folder / f"watch-take{take}.json", watched)
    reasons = []
    if report.get("passed") is False:
        reasons += [f"{k} gate: {(report.get(k) or {}).get('note', '')}" for k in ("audio", "identity") if (report.get(k) or {}).get("result") == "fail"]
    if watched["identity_drift"]:
        reasons.append("the watcher saw identity drift: " + "; ".join(c.get("note", "") for c in watched["continuity"] if c.get("kind") == "identity")[:200])
    seg["takes"].append({"take": take, "seed": seg["seed"], "output": seg["output"], "job": seg.get("job"), "passed": not reasons,
                         "reasons": reasons, "qa": {k: report.get(k) for k in ("passed", "audio", "identity", "note", "error") if report.get(k) is not None},
                         "watch": watched})
    if reasons and take <= int(s.get("retakes") or 0):
        seg["status"] = "retake"
        say(run, f"Segment {seg['index']}: take {take} failed ({'; '.join(reasons)[:200]}); again at the next seed.", segment=seg["index"])
        return
    commit(run, seg, watched, transcript, reasons)


def commit(run: dict, seg: dict, watched: dict, transcript: str, reasons: list) -> None:
    memory = memory_of(run)
    graph = merge(memory["graph"], watched["delta"], seg["index"])
    for event in graph["events"]:
        if event.get("segment") == seg["index"] and not event.get("t"):
            event["t"] = mmss(float(seg["start"]))
    archive = memory.get("archive") or []
    if len(graph["events"]) > COMPACT_AT:
        folded = copy.deepcopy(graph["events"][:FOLD])
        chapter, _ = ask_json(lambda text: ask_text(run, COMPACT_SYSTEM, text),
                              lambda d: "" if str(d.get("title") or "").strip() and str(d.get("summary") or "").strip() else '"title" and "summary" are text',
                              compact_prompt(folded, graph.get("chapters") or []), "memory's chapter fold")
        archive = archive + fold(graph, chapter, folded)
    memory = {"graph": graph, "archive": archive, "observed_through": seg["index"]}
    folder = run_dir(run["id"])
    write_json(folder / "memory" / f"{seg['id']}.json", {**memory, "delta": watched["delta"], "cast": run.get("cast") or {},
                                                          "video": seg["output"]})
    write_json(folder / "memory.json", memory)
    seg.update(status="done", watch=watched, transcript=transcript, passed=not reasons, reasons=reasons, committed=slate.now())
    say(run, f"Segment {seg['index']} joins the film" + (f" (it did not pass: {'; '.join(reasons)[:160]})" if reasons else "") + ".",
        stage="committed", segment=seg["index"])


# ---------- the film ----------

def has_audio(clip: Path) -> bool:
    done = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(clip)],
                          capture_output=True, text=True, env=qa.ENV, timeout=30)
    return bool(done.stdout.strip())


def publish(run: dict) -> dict | None:
    """Every committed segment joined into ``film.mp4``: a continuation cross-fades over the one frame it repeats,
    a new scene dissolves in; picture and sound fade together, so they stay in step."""

    parts = [s for s in committed(run) if on_disk(s.get("output"))]
    if not parts:
        return None
    folder = run_dir(run["id"])
    say(run, f"Joining the film: {len(parts)} segment{'s' if len(parts) != 1 else ''}.", stage="preview")
    size = None
    inputs, graph, lengths_ = [], [], []
    for i, seg in enumerate(parts):
        clip = HOME / seg["output"]
        length = qa.duration(clip)
        lengths_.append(length)
        inputs += ["-i", str(clip)]
        if size is None:
            probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(clip)],
                                   capture_output=True, text=True, env=qa.ENV, timeout=30).stdout.strip().split(",")
            size = (int(probe[0]) // 2 * 2, int(probe[1]) // 2 * 2) if len(probe) == 2 and all(x.isdigit() for x in probe) else (864, 480)
        graph.append(f"[{i}:v]fps={FPS},scale={size[0]}:{size[1]},setsar=1,format=yuv420p,settb=AVTB[v{i}]")
        if has_audio(clip):
            graph.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=0:{length:.4f}[a{i}]")
        else:
            graph.append(f"anullsrc=r=48000:cl=stereo,atrim=0:{length:.4f},aformat=sample_fmts=fltp[a{i}]")
    vlast, alast, total = "[v0]", "[a0]", lengths_[0]
    for i in range(1, len(parts)):
        fade = CONTINUE_FADE if parts[i].get("mode") == "previous" else CUT_FADE
        fade = min(fade, lengths_[i] / 2, total / 2)
        graph.append(f"{vlast}[v{i}]xfade=transition=fade:duration={fade:.4f}:offset={total - fade:.4f}[vx{i}]")
        graph.append(f"{alast}[a{i}]acrossfade=d={fade:.4f}:c1=tri:c2=tri[ax{i}]")
        vlast, alast, total = f"[vx{i}]", f"[ax{i}]", total + lengths_[i] - fade
    target = folder / "film.mp4"
    scratch = folder / "film.part.mp4"
    command = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", vlast, "-map", alast,
               "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
               "-movflags", "+faststart", str(scratch)]
    done = subprocess.run(command, capture_output=True, text=True, env=qa.ENV, timeout=3600)
    if done.returncode or not scratch.is_file():
        say(run, "The film could not be joined: " + (done.stderr or "").strip()[-300:])
        return None
    scratch.replace(target)
    run["film"] = {"path": relative(target), "segments": len(parts), "seconds": round(qa.duration(target), 2), "at": slate.now()}
    save(run)
    return run["film"]
