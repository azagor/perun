"""Operator inbox: things that need a human decision, and where the answer goes.

The design rule is simple and deliberate: **a notification is not a message, it
is a typed target**.  Every item stores ``target_kind`` + ``target_id`` (a
session, a permission request, a form, a room, a proposal) and the reply is
dispatched by an adapter for that target.  Nothing is routed by inference from
"the current project" or "the newest session", so several blocked sessions can
be waiting at once without crossing wires.

This module is transport-agnostic.  It writes rows, renders cards and performs
the reply side-effect; the Telegram bridge owns sending, and injects nothing.
Per the layering rules in ``AGENTS.md`` it never imports ``telegram`` or
``perunchat_bridge`` at module import time (the bridge import is local to the
room adapter, because the bridge itself imports this module for its producers).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import action_proposals, config, db, opencode_api, registry

LOG = logging.getLogger("perun.attention")

DEFAULT_TTL_MINUTES = 240
WAITER_FRESH_SECONDS = 5.0

# Options are stored verbatim in ``attention_requests.options`` and rendered as
# Telegram inline buttons.  ``id`` doubles as the OpenCode permission decision,
# which keeps the adapter mapping obvious.
PERMISSION_OPTIONS: List[Dict[str, str]] = [
    {"id": "once", "label": "Allow once"},
    {"id": "always", "label": "Always allow"},
    {"id": "reject", "label": "Reject"},
]
PROPOSAL_OPTIONS: List[Dict[str, str]] = [
    {"id": "approve", "label": "Approve"},
    {"id": "reject", "label": "Reject"},
]

_STATE_LABELS = {
    "answered": "answered",
    "approved": "approved",
    "rejected": "rejected",
    "expired": "expired",
    "cancelled": "cancelled",
    "undeliverable": "undeliverable",
}

# Anything that looks like a credential must never leave the machine in a card.
# Shell commands are the risky case: a card shows the command it is asking about,
# and `curl -H "Authorization: Bearer …"` or `mysql --password …` would otherwise
# send the secret to Telegram. Order matters: the broader header rule runs first
# so the narrower ones cannot leave a fragment behind.
_AUTH_HEADER = re.compile(
    r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
    r"\s*:\s*[^\r\n\"']+"
)
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]*:[^\s/@]+@")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{6,}")
_PREFIXED_KEY = re.compile(
    r"(?i)\b(?:sk|ghp|gho|ghu|ghs|ghr|xox[abprs])[-_][A-Za-z0-9_\-]{8,}"
)
_SECRETISH = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization)\b"
    r"\s*[:=]\s*[\"']?[^\s\"'\[][^\s\"']*[\"']?"
)
_FLAG_SECRET = re.compile(
    r"(?i)(--?[a-z0-9_-]*(?:key|token|secret|password|passwd|auth)[a-z0-9_-]*\s+)"
    r"[\"']?[^\s\"'\[][^\s\"']*[\"']?"
)


class DeliveryError(RuntimeError):
    """The reply could not be delivered to its target."""


# --------------------------------------------------------------------------- #
# Producers
# --------------------------------------------------------------------------- #
def request(
    project: Optional[Dict[str, Any]],
    target_kind: str,
    target_id: str,
    source: str,
    question: str,
    *,
    options: Optional[List[Dict[str, str]]] = None,
    ref: Optional[Dict[str, Any]] = None,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    dedupe_key: str = "",
) -> Optional[Dict[str, Any]]:
    """Record one thing that needs the operator.

    Returns the new row, or the already-open duplicate (same ``dedupe_key``).
    Sending is the notifier's job, never the producer's.
    """
    project_id = int(project["id"]) if project and project.get("id") else None
    item = registry.add_attention(
        project_id,
        target_kind,
        target_id,
        source,
        question,
        options=options or [],
        ref=ref or {},
        ttl_minutes=ttl_minutes,
        dedupe_key=dedupe_key,
    )
    if item is None:
        return None
    # Only log the first creation of a deduplicated item, not every replay.
    is_new = bool(item.pop("_new", False))
    item["is_new"] = is_new
    if is_new:
        registry.log_activity(
            project_id,
            "attention_requested",
            f"Needs operator: {source} ({target_kind} {target_id})",
            {
                "attention_id": item["id"],
                "target_kind": target_kind,
                "target_id": target_id,
                "source": source,
            },
        )
    return item


def project_for_location(directory: str) -> Optional[Dict[str, Any]]:
    """Map an OpenCode location to a registered project (best effort)."""
    if not directory:
        return None
    project = registry.get_project_by_path(directory)
    if project:
        return project
    # A session created inside a subdirectory still belongs to its project.
    try:
        target = str(directory).rstrip("\\/").lower()
    except Exception:
        return None
    for candidate in registry.list_projects(include_hidden=True):
        path = str(candidate.get("path") or "").rstrip("\\/").lower()
        if path and (target == path or target.startswith(path + "\\")
                     or target.startswith(path + "/")):
            return candidate
    return None


# --------------------------------------------------------------------------- #
# Policy: may we notify, and to whom
# --------------------------------------------------------------------------- #
def _quiet_now(quiet_hours: str, now: Optional[datetime] = None) -> bool:
    spec = (quiet_hours or "").strip()
    if not spec or "-" not in spec:
        return False
    try:
        start_s, end_s = [p.strip() for p in spec.split("-", 1)]
        start = dtime.fromisoformat(start_s)
        end = dtime.fromisoformat(end_s)
    except ValueError:
        return False
    current = (now or datetime.now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def eligible(item: Dict[str, Any],
             settings: Optional[config.Settings] = None
             ) -> Tuple[bool, str]:
    """Whether this item should produce a Telegram card right now."""
    settings = settings or config.Settings.load()
    if not getattr(settings, "attention_enabled", False):
        return False, "attention disabled"
    if int(item.get("ttl_epoch") or 0) <= int(time.time()):
        return False, "expired"
    project = registry.get_project(item["project_id"]) if item.get("project_id") else None
    slug = str((project or {}).get("slug") or "").lower()
    allowed_projects = {str(s).lower() for s in (settings.attention_projects or [])}
    if allowed_projects and slug not in allowed_projects:
        return False, "project not enabled"
    if _quiet_now(getattr(settings, "attention_quiet_hours", "")):
        return False, "quiet hours"
    cap = int(getattr(settings, "attention_max_per_hour", 6) or 6)
    # ``created_at`` is UTC; compare in UTC so the window is not skewed by the
    # machine's local offset.
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    count = registry.attention_count_since(item.get("project_id"), since)
    if count > cap:
        return False, "hourly cap reached"
    return True, ""


def recipients(item: Dict[str, Any], *, allowed: List[str],
               muted: Optional[Callable[[str], set]] = None) -> List[str]:
    """Chats that should receive *item*.

    Every statically paired chat gets every notice: the inbox is the operator's
    pager, not a per-project channel.  A chat can mute individual projects, and
    runtime-paired chats never receive notices (they are not the owner).
    """
    project = registry.get_project(item["project_id"]) if item.get("project_id") else None
    slug = str((project or {}).get("slug") or "").lower()
    out: List[str] = []
    for chat_id in allowed or []:
        chat_id = str(chat_id)
        if not chat_id:
            continue
        muted_projects = (muted(chat_id) if muted else set()) or set()
        if slug and slug in {str(m).lower() for m in muted_projects}:
            continue
        out.append(chat_id)
    return out


def _redact(text: str) -> str:
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}: [redacted]", text or "")
    text = _URL_CREDS.sub(lambda m: f"{m.group(1)}[redacted]@", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} [redacted]", text)
    text = _PREFIXED_KEY.sub("[redacted]", text)
    text = _SECRETISH.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _FLAG_SECRET.sub(lambda m: f"{m.group(1)}[redacted]", text)
    return text


def redact(text: str) -> str:
    """Public entry point: anything Perun composes for a human goes through this."""
    return _redact(text)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _display_resource(resource: str, show_paths: bool) -> str:
    """Shorten a *file path* to its basename; leave commands, URLs and globs alone.

    Permission resources are not only paths: a shell permission carries the whole
    command, a webfetch carries a URL, a skill carries its id. Collapsing those to
    a basename would hide exactly what the operator has to judge.
    """
    resource = str(resource or "")
    if show_paths or "://" in resource:
        return resource
    if not re.match(r"^(?:[A-Za-z]:[\\/]|~?/)", resource):
        return resource
    tail = re.split(r"[\\/]", resource.rstrip("\\/"))[-1]
    return f"…/{tail}" if tail else "[path]"


def _session_label(item: Dict[str, Any]) -> str:
    ref = item.get("ref") or {}
    title = str(ref.get("session_title") or "").strip()
    if title:
        return _clip(title, 60)
    sid = str(ref.get("session_id") or item.get("target_id") or "")
    return sid[:14] if sid else "session"


def project_label(item: Dict[str, Any]) -> str:
    project = registry.get_project(item["project_id"]) if item.get("project_id") else None
    return str((project or {}).get("slug") or "perun")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render(item: Dict[str, Any], *, show_paths: bool = False) -> str:
    """Plain-text card body (the bridge applies Telegram's HTML conversion)."""
    ident = item["id"]
    kind = item["target_kind"]
    ref = item.get("ref") or {}
    head = f"*{project_label(item)}* · {_session_label(item)} · att_{ident}"

    if kind == "permission":
        action = _clip(ref.get("action") or "use a tool", 40)
        resources = ref.get("resources") or []
        if str(ref.get("action") or "") == "shell":
            # A shell permission's resource *is* the command: show it (redacted),
            # never a basename.
            shown = ", ".join(_redact(_clip(r, 200)) for r in resources[:3])
        else:
            shown = ", ".join(
                _redact(_display_resource(r, show_paths)) for r in resources[:3]
            )
        if len(resources) > 3:
            shown += f" (+{len(resources) - 3} more)"
        lines = [
            f"🔐 {head}",
            f"Permission requested: *{action}*"
            + (f" — {shown}" if shown else ""),
        ]
        save = ref.get("save") or []
        if save:
            saved = ", ".join(_redact(str(s)) for s in save[:3])
            lines.append(f"⚙️ _Always allow_ would save: `{saved}`")
        lines.append("Choose, or reply with feedback to reject.")
        return "\n".join(lines)

    if kind == "form":
        fields = [f for f in (ref.get("fields") or []) if isinstance(f, dict)]
        lines = [f"📝 {head}", _redact(item.get("question") or "")]
        if not fields:
            return "\n".join(lines + ["Reply with your answer."])
        shown = fields[:6]
        lines.append("Answer this field:" if len(shown) == 1
                     else f"Answer these {len(shown)} fields:")
        for index, field in enumerate(shown, 1):
            key = str(field.get("key") or "")
            title = str(field.get("title") or "").strip()
            label = f"*{_redact(title)}*" if title and title != key else f"`{key}`"
            notes: List[str] = []
            if title and title != key:
                notes.append(f"`{key}`")
            if field.get("required"):
                notes.append("required")
            options = [str(o) for o in (field.get("options") or [])][:6]
            if options:
                notes.append("one of: " + ", ".join(_redact(o) for o in options))
            suffix = f" — {'; '.join(notes)}" if notes else ""
            lines.append(f"{index}. {label}{suffix}")
        if len(shown) == 1:
            field = shown[0]
            options = [str(o) for o in (field.get("options") or [])]
            if _is_multiselect(field) and options:
                picked = [options[i] for i in sorted(_toggled(item))
                          if 0 <= i < len(options)]
                if picked:
                    lines.append("Selected: " + ", ".join(_redact(p) for p in picked))
                lines.append("Tap the options below, then Send.")
            elif _form_answer_field(item) is not None:
                lines.append("Pick one below.")
            else:
                lines.append("Reply with the value.")
        else:
            keys = [str(f.get("key")) for f in shown]
            lines.append("Reply with one line per field:")
            lines.extend(f"`{k}`: …" for k in keys)
            if len(keys) <= 3:
                sample = ", ".join(f'"{k}": "…"' for k in keys)
                lines.append(f"Or as JSON: `{{{sample}}}`")
        return "\n".join(lines)

    if kind == "proposal":
        return "\n".join([
            f"📋 {head}",
            _redact(item.get("question") or ""),
            "Approve or reject. Approving pins the current target contents.",
        ])

    if kind == "room":
        return "\n".join([
            f"💬 {head} · `{item.get('target_id')}`",
            _redact(item.get("question") or ""),
            *_option_lines(item),
            "Reply to this message and it will be posted in that room.",
        ])

    # session / handoff
    return "\n".join([
        f"❓ {head}",
        _redact(item.get("question") or ""),
        *_option_lines(item),
        "Reply to this message; your answer goes into that session.",
    ])


#: Buttons on a single card stay a glance, not a menu. Beyond this the card
#: lists the values instead, so the operator still knows what to type.
MAX_CHOICE_BUTTONS = 8


def _form_answer_field(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The single field a form card can answer with buttons (``None`` otherwise).

    Only a one-field form is button-answerable: a tap carries exactly one value,
    and a multi-field form needs all of them at once.
    """
    fields = [f for f in ((item.get("ref") or {}).get("fields") or [])
              if isinstance(f, dict)]
    if len(fields) != 1:
        return None
    field = fields[0]
    ftype = str(field.get("type") or "string")
    if ftype == "boolean":
        return field
    if field.get("options") and ftype in ("select", "multiselect", "choice",
                                          "radio", "enum", "string"):
        return field
    return None


def _is_multiselect(field: Dict[str, Any]) -> bool:
    return str(field.get("type") or "") == "multiselect"


def _choice_labels(field: Dict[str, Any]) -> List[str]:
    if str(field.get("type") or "") == "boolean":
        return ["Yes", "No"]
    return [str(o) for o in (field.get("options") or [])]


def _choice_value(field: Dict[str, Any], label: str) -> Any:
    """The value the service expects for the tapped label."""
    if str(field.get("type") or "") == "boolean":
        return label.strip().lower() in ("yes", "1", "true", "y", "on")
    return label


def _choice_rows(item: Dict[str, Any], labels: List[str], *, prefix: str = "o",
                 toggled: Optional[set] = None) -> List[List[Dict[str, str]]]:
    rows = []
    for index, label in enumerate(labels[:MAX_CHOICE_BUTTONS]):
        text = label
        if toggled is not None:
            text = ("✅ " if index in toggled else "⬜️ ") + label
        rows.append([{"text": str(text)[:60],
                      "callback_data": f"att:{item['id']}:{prefix}{index}"}])
    return rows


def keyboard(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Inline keyboard for the initial card (``None`` when a reply is enough)."""
    kind = item["target_kind"]
    if kind == "permission":
        # Keep "Allow once" first, the destructive choices on their own rows.
        return {"inline_keyboard": [
            [{"text": "Allow once", "callback_data": f"att:{item['id']}:once"}],
            [{"text": "Always allow", "callback_data": f"att:{item['id']}:always"}],
            [{"text": "Reject", "callback_data": f"att:{item['id']}:reject"}],
        ]}
    if kind == "proposal":
        options = item.get("options") or PROPOSAL_OPTIONS
        return {"inline_keyboard": [
            [{"text": str(opt.get("label") or opt.get("id")),
              "callback_data": f"att:{item['id']}:{opt.get('id')}"}]
            for opt in options
        ]}
    if kind == "form":
        field = _form_answer_field(item)
        if field is None:
            return None
        labels = _choice_labels(field)
        if not labels or len(labels) > MAX_CHOICE_BUTTONS:
            return None
        if _is_multiselect(field):
            toggled = _toggled(item)
            rows = _choice_rows(item, labels, prefix="t", toggled=toggled)
            chosen = len([i for i in toggled if 0 <= i < len(labels)])
            rows.append([{
                "text": f"✔️ Send {chosen} selected" if chosen else "✔️ Send selection",
                "callback_data": f"att:{item['id']}:d",
            }])
            return {"inline_keyboard": rows}
        return {"inline_keyboard": _choice_rows(item, labels)}
    # session / room / handoff: the options an agent passed to
    # ``perun_ask_operator`` are choices, so they are buttons too.
    options = item.get("options") or []
    if not options or len(options) > MAX_CHOICE_BUTTONS:
        return None
    rows = []
    for index, opt in enumerate(options):
        label = str(opt.get("label") or opt.get("id") or f"Option {index + 1}")
        rows.append([{"text": label[:60],
                      "callback_data": f"att:{item['id']}:o{index}"}])
    return {"inline_keyboard": rows}


def _toggled(item: Dict[str, Any]) -> set:
    """Multi-select indices already picked on this card (stored in ``ref``)."""
    raw = (item.get("ref") or {}).get("toggles") or []
    return {int(i) for i in raw if str(i).lstrip("-").isdigit()}


def _option_lines(item: Dict[str, Any]) -> List[str]:
    """Fallback text for options that do not fit on the keyboard."""
    options = item.get("options") or []
    if not options:
        return []
    if len(options) <= MAX_CHOICE_BUTTONS:
        return ["Choose one below, or reply in your own words."]
    return ["Options:", *[
        f"{index}. {_redact(str(opt.get('label') or opt.get('id') or ''))}"
        for index, opt in enumerate(options[:12], 1)
    ]]


def confirmation(item: Dict[str, Any], result: Dict[str, Any]) -> str:
    """Text sent after a successful answer."""
    state = (result.get("item") or {}).get("state") or "answered"
    label = _STATE_LABELS.get(state, state)
    mode = (result.get("delivery") or {}).get("mode")
    if mode == "prompt":
        return (f"⏳ queued behind the current turn — att_{item['id']}\n"
                "Use the buttons to interrupt or undo.")
    if mode == "queued":
        return f"📥 reply queued for `{item.get('target_id')}` — will be posted when it reconnects."
    if mode == "tool":
        return f"✅ delivered to the waiting agent ({label})."
    if mode == "steer":
        return "⚡ delivered now — the answer is no longer queued."
    if mode == "undone":
        return "↩ answer withdrawn — reply again to send a new one."
    if item["target_kind"] == "room":
        return f"✅ posted in `{item.get('target_id')}`."
    return f"✅ {label} — att_{item['id']}"


def followup_keyboard(item: Dict[str, Any],
                      result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if (result.get("delivery") or {}).get("mode") != "prompt":
        return None
    return {"inline_keyboard": [[
        {"text": "⚡ Interrupt now", "callback_data": f"att:{item['id']}:steer"},
        {"text": "↩ Undo", "callback_data": f"att:{item['id']}:undo"},
    ]]}


# --------------------------------------------------------------------------- #
# Reply side: adapters per target kind
# --------------------------------------------------------------------------- #
def _permission_decision(option: str) -> str:
    if option in ("once", "always", "reject"):
        return option
    raise DeliveryError(f"unknown permission choice: {option}")


#: ``key: value`` (or ``key = value``) — the phone-friendly answer format. JSON
#: stays supported, but a JSON template with made-up field names is exactly what
#: made the Telegram card unreadable.
_FIELD_LINE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*[:=]\s*(.*)$")


def _form_hint(fields: List[Dict[str, Any]]) -> str:
    keys = [str(f.get("key")) for f in fields[:6]]
    hint = ("reply with one line per field (" + " · ".join(f"{k}: …" for k in keys)
            + ") or as a JSON object")
    return hint[0].upper() + hint[1:]


def _coerce_form_answer(item: Dict[str, Any], text: str) -> Dict[str, Any]:
    fields = [f for f in ((item.get("ref") or {}).get("fields") or [])
              if isinstance(f, dict)]
    text = (text or "").strip()
    if not fields:
        raise DeliveryError("this form has no fields to answer")
    if len(fields) == 1 and not text.startswith("{"):
        # One field never needs a format: the whole message is the value.
        parsed = {str(fields[0].get("key")): text}
    elif text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise DeliveryError("That JSON is not valid — " + _form_hint(fields)) from exc
        if not isinstance(parsed, dict):
            raise DeliveryError("A form answer must be a JSON object — "
                                + _form_hint(fields))
    else:
        parsed = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            match = _FIELD_LINE.match(line)
            if not match:
                raise DeliveryError(_form_hint(fields))
            parsed[match.group(1)] = match.group(2).strip()
        if not parsed:
            raise DeliveryError(_form_hint(fields))
    known = {str(f.get("key")) for f in fields}
    if known and set(parsed) - known:
        raise DeliveryError("unknown form field(s): " + ", ".join(sorted(set(parsed) - known))
                            + " — " + _form_hint(fields))
    out: Dict[str, Any] = {}
    for field in fields:
        key = str(field.get("key"))
        if key not in parsed:
            continue
        value = parsed[key]
        ftype = str(field.get("type") or "string")
        if ftype == "boolean" and not isinstance(value, bool):
            value = str(value).strip().lower() in ("1", "true", "yes", "y", "on")
        elif ftype == "multiselect":
            if isinstance(value, str):
                value = [v.strip() for v in value.split(",") if v.strip()]
            elif not isinstance(value, list):
                raise DeliveryError(f"{key} must be a list of choices")
            value = [str(v) for v in value]
            allowed = {str(o) for o in (field.get("options") or [])}
            unknown = [v for v in value if allowed and v not in allowed]
            if unknown:
                raise DeliveryError(f"{key}: unknown choice(s) " + ", ".join(unknown))
        elif ftype in ("number", "integer"):
            try:
                value = float(value) if ftype == "number" else int(value)
            except (TypeError, ValueError) as exc:
                raise DeliveryError(f"{key} must be a {ftype}") from exc
        out[key] = value
    if not out:
        raise DeliveryError("no values supplied")
    return out


def _deliver_text(item: Dict[str, Any], text: str, by: str, *,
                  form_answer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    kind = item["target_kind"]
    ref = item.get("ref") or {}

    if kind in ("permission", "proposal"):
        raise DeliveryError("this one needs one of the buttons")

    if kind == "session":
        # A blocked perun_ask_operator call will return this answer as its tool
        # result; only inject a prompt when nobody is waiting any more.
        seen = item.get("waiter_seen_epoch")
        if seen and (time.time() - float(seen)) <= WAITER_FRESH_SECONDS:
            return {"mode": "tool"}
        api = opencode_api.default_api()
        sid = str(item["target_id"])
        message_id = f"msg_att_{item['id']}"
        data = api.prompt(
            sid, text,
            delivery=str(item.get("delivery") or "queue"),
            message_id=message_id,
        )
        inbox_id = str(data.get("id") or "")
        if inbox_id:
            registry.update_attention_ref(item["id"], inbox_id=inbox_id,
                                          session_id=sid, message_id=message_id)
        return {"mode": "prompt", "inbox_id": inbox_id, "session_id": sid}

    if kind == "form":
        api = opencode_api.default_api()
        # A button answer is already a typed value (a toggle list, a boolean);
        # only a typed message has to be coerced.
        answer = form_answer if form_answer is not None else _coerce_form_answer(item, text)
        api.reply_form(str(item["target_id"]), str(ref.get("form_id") or ""), answer)
        return {"mode": "form"}

    if kind in ("room", "handoff"):
        from . import perunchat_bridge  # local import: the bridge imports us

        room_id = str(item["target_id"])
        bridge = perunchat_bridge.get_bridge()
        body = f"operator (via Telegram): {text}"
        if bridge is not None and bridge.is_connected():
            posted = bridge.post_to_room(
                room_id, body, in_reply_to=str(ref.get("frame_id") or "") or None
            )
            if not posted:
                raise DeliveryError("PerunChat rejected the message; reply kept open")
            return {"mode": "room"}
        registry.attention_outbox_add(
            item["id"], kind, room_id, {"text": body, "in_reply_to": ref.get("frame_id") or ""}
        )
        return {"mode": "queued"}

    raise DeliveryError(f"cannot deliver to {kind}")


def answer(attention_id: int, text: str, *, by: str) -> Dict[str, Any]:
    """Deliver a free-text answer, then close the item."""
    return _answer(attention_id, text, by=by)


def _answer(attention_id: int, text: str, *, by: str,
            form_answer: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Deliver an answer (typed or button-chosen), then close the item.

    Order matters: the adapter runs first, so a failed delivery leaves the item
    open and the operator can retry instead of losing the answer.
    """
    item = registry.get_attention(attention_id)
    if not item:
        return {"ok": False, "error": "attention item not found"}
    if item["state"] != "open":
        return {"ok": False, "error": f"already {item['state']}", "item": item}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty answer", "item": item}
    try:
        # The answer is the operator's own text and must reach the target
        # verbatim (a credential typed into the reply is intentional). Only the
        # *notification* is redacted, because that is what Perun composes and
        # sends to a cloud chat.
        delivery = _deliver_text(item, text, by, form_answer=form_answer)
    except DeliveryError as exc:
        registry.log_activity(
            item.get("project_id"), "attention_failed",
            f"Could not deliver answer for att_{item['id']}: {exc}",
            {"attention_id": item["id"], "target_kind": item["target_kind"]},
        )
        return {"ok": False, "error": str(exc), "item": item}
    except opencode_api.OpenCodeError as exc:
        status = getattr(exc, "status", 0)
        if status == 404 and item["target_kind"] in ("session", "form"):
            # The session/form is gone; the answer can never land. Close it so
            # the operator is not left tapping a dead card until the TTL.
            registry.close_attention(item["id"], "undeliverable",
                                     answer=text, answered_by=by)
            registry.log_activity(
                item.get("project_id"), "attention_failed",
                f"att_{item['id']} undeliverable: the {item['target_kind']} target is gone",
                {"attention_id": item["id"], "target_kind": item["target_kind"]},
            )
            return {
                "ok": False,
                "error": "the target is gone (session closed?) — the answer was not delivered",
                "item": registry.get_attention(item["id"]),
            }
        return {"ok": False, "error": f"opencode unavailable: {exc}", "item": item}
    if not registry.close_attention(item["id"], "answered", answer=text,
                                    answered_by=by):
        return {"ok": False, "error": "already handled", "item": item}
    registry.log_activity(
        item.get("project_id"), "attention_answered",
        f"att_{item['id']} answered by {by}",
        {"attention_id": item["id"], "target_kind": item["target_kind"],
         "target_id": item["target_id"], "mode": delivery.get("mode")},
    )
    return {"ok": True, "item": registry.get_attention(item["id"]), "delivery": delivery}


def _choice_index(action: str, prefix: str) -> Optional[int]:
    if not action.startswith(prefix):
        return None
    tail = action[len(prefix):]
    return int(tail) if tail.isdigit() else None


def toggle(attention_id: int, index: int, *, by: str = "") -> Dict[str, Any]:
    """Flip one multi-select choice on a form card — nothing is sent yet.

    The state lives in ``ref['toggles']`` so a re-render (or a second device)
    shows the same marks; the bridge calls :func:`choose` with ``d`` to submit.
    """
    item = registry.get_attention(attention_id)
    if not item:
        return {"ok": False, "error": "attention item not found"}
    if item["state"] != "open":
        return {"ok": False, "error": f"already {item['state']}", "item": item}
    field = _form_answer_field(item)
    if item["target_kind"] != "form" or field is None or not _is_multiselect(field):
        return {"ok": False, "error": "this card has nothing to toggle", "item": item}
    labels = _choice_labels(field)
    try:
        index = int(index)
    except (TypeError, ValueError):
        return {"ok": False, "error": "that choice is no longer on the card",
                "item": item}
    if not 0 <= index < len(labels):
        return {"ok": False, "error": "that choice is no longer on the card",
                "item": item}
    chosen = _toggled(item)
    if index in chosen:
        chosen.discard(index)
    else:
        chosen.add(index)
    updated = registry.update_attention_ref(item["id"], toggles=sorted(chosen))
    LOG.info("att_%s: toggled %r (%d picked) by %s", item["id"],
             labels[index], len(chosen), by or "someone")
    return {"ok": True, "item": updated or item, "delivery": {"mode": "toggle"}}


def choose(attention_id: int, option: str, *, by: str) -> Dict[str, Any]:
    """Deliver a button choice (permission, proposal, option, or form choice)."""
    item = registry.get_attention(attention_id)
    if not item:
        return {"ok": False, "error": "attention item not found"}
    if item["state"] != "open":
        return {"ok": False, "error": f"already {item['state']}", "item": item}
    kind = item["target_kind"]
    action = str(option or "").strip()
    option = action.lower()
    try:
        if kind in ("session", "room", "handoff"):
            index = _choice_index(option, "o")
            if index is None:
                raise DeliveryError("this one needs a text reply")
            options = item.get("options") or []
            if not 0 <= index < len(options):
                raise DeliveryError("that choice is no longer on the card")
            label = str(options[index].get("label") or options[index].get("id") or "")
            if not label:
                raise DeliveryError("that choice is empty")
            return _answer(item["id"], label, by=by)
        if kind == "form":
            field = _form_answer_field(item)
            if field is None:
                raise DeliveryError("this form needs a text reply")
            key = str(field.get("key") or "")
            labels = _choice_labels(field)
            if option == "d":
                if not _is_multiselect(field):
                    raise DeliveryError("this card has nothing to send")
                picked = [labels[i] for i in sorted(_toggled(item))
                          if 0 <= i < len(labels)]
                if not picked:
                    raise DeliveryError("pick at least one option first")
                return _answer(item["id"], ", ".join(picked), by=by,
                               form_answer={key: picked})
            index = _choice_index(option, "o")
            if index is None:
                raise DeliveryError(f"this form needs a text reply ({_form_hint([field])})")
            if not 0 <= index < len(labels):
                raise DeliveryError("that choice is no longer on the card")
            label = labels[index]
            return _answer(item["id"], label, by=by,
                           form_answer={key: _choice_value(field, label)})
        if kind == "permission":
            decision = _permission_decision(option)
            ref = item.get("ref") or {}
            session_id = str(ref.get("session_id") or "")
            request_id = str(ref.get("request_id") or item["target_id"])
            if not session_id:
                raise DeliveryError("permission item has no session id")
            opencode_api.default_api().reply_permission(
                session_id, request_id, decision,
            )
            state = "rejected" if decision == "reject" else "approved"
            if decision == "reject":
                # OpenCode rejects every pending request in that session too.
                for sibling in registry.open_attention(limit=200, state="open"):
                    sibling_ref = sibling.get("ref") or {}
                    if sibling["id"] == item["id"]:
                        continue
                    if sibling["target_kind"] != "permission":
                        continue
                    if str(sibling_ref.get("session_id") or "") != session_id:
                        continue
                    registry.close_attention(
                        sibling["id"], "cancelled",
                        answer="rejected together with another request in this session",
                        answered_by=by,
                    )
        elif kind == "proposal":
            if option not in ("approve", "reject"):
                raise DeliveryError(f"unknown proposal choice: {option}")
            if option == "approve":
                action_proposals.approve(
                    str(item["target_id"]), f"telegram:{by}", allow_self=False
                )
                state = "approved"
            else:
                action_proposals.reject(
                    str(item["target_id"]), f"telegram:{by}",
                    reason=str((item.get("ref") or {}).get("reason") or
                               "rejected from Telegram"),
                )
                state = "rejected"
        else:
            raise DeliveryError("this one needs a text reply")
    except (DeliveryError, opencode_api.OpenCodeError) as exc:
        status = getattr(exc, "status", 0)
        if status == 404:
            # The request was resolved elsewhere (TUI, another device).
            registry.close_attention(item["id"], "cancelled",
                                     answer="resolved elsewhere",
                                     answered_by=by)
            return {"ok": False,
                    "error": "that request was already resolved elsewhere",
                    "item": registry.get_attention(item["id"])}
        return {"ok": False, "error": str(exc), "item": item}
    except ValueError as exc:
        # A proposal that changed state under the card ("proposal is approved")
        # can never accept this tap; close it instead of leaving a dead card.
        message = str(exc)
        if "proposal is " in message or "not found" in message:
            registry.close_attention(item["id"], "cancelled", answer=message,
                                     answered_by=by)
            return {"ok": False, "error": message,
                    "item": registry.get_attention(item["id"])}
        return {"ok": False, "error": message, "item": item}

    if not registry.close_attention(item["id"], state, answer=option, answered_by=by):
        return {"ok": False, "error": "already handled", "item": item}
    registry.log_activity(
        item.get("project_id"), "attention_answered",
        f"att_{item['id']} {state} by {by}",
        {"attention_id": item["id"], "target_kind": kind,
         "target_id": item["target_id"], "choice": option},
    )
    return {"ok": True, "item": registry.get_attention(item["id"]),
            "delivery": {"mode": kind}}


def set_delivery(attention_id: int, delivery: str = "steer", *,
                 by: str = "") -> Dict[str, Any]:
    """⚡ promote a queued answer to an interrupt, in place."""
    item = registry.get_attention(attention_id)
    if not item:
        return {"ok": False, "error": "attention item not found"}
    if item["target_kind"] != "session":
        return {"ok": False, "error": "only session answers can be steered"}
    inbox_id = str((item.get("ref") or {}).get("inbox_id") or "")
    if not inbox_id:
        return {"ok": False, "error": "the answer was already delivered"}
    try:
        opencode_api.default_api().set_inbox_delivery(
            str(item["target_id"]), inbox_id, delivery
        )
    except opencode_api.OpenCodeError as exc:
        return {"ok": False, "error": str(exc), "item": item}
    registry.set_attention_delivery(item["id"], delivery)
    registry.log_activity(
        item.get("project_id"), "attention_delivered",
        f"att_{item['id']} promoted to {delivery} by {by or 'operator'}",
        {"attention_id": item["id"], "delivery": delivery},
    )
    return {"ok": True, "item": registry.get_attention(item["id"]),
            "delivery": {"mode": delivery}}


def undo(attention_id: int, *, by: str = "") -> Dict[str, Any]:
    """↩ cancel an undelivered answer and reopen the item."""
    item = registry.get_attention(attention_id)
    if not item:
        return {"ok": False, "error": "attention item not found"}
    if item["state"] != "answered":
        return {"ok": False, "error": f"cannot undo a {item['state']} item"}
    inbox_id = str((item.get("ref") or {}).get("inbox_id") or "")
    if item["target_kind"] != "session" or not inbox_id:
        return {"ok": False, "error": "nothing queued to undo"}
    try:
        opencode_api.default_api().cancel_inbox(str(item["target_id"]), inbox_id)
    except opencode_api.OpenCodeError as exc:
        return {"ok": False, "error": str(exc), "item": item}
    registry.update_attention_ref(item["id"], inbox_id="")
    if not registry.reopen_attention(item["id"], reason="undone by operator"):
        return {"ok": False, "error": "could not reopen the item", "item": item}
    registry.log_activity(
        item.get("project_id"), "attention_delivered",
        f"att_{item['id']} answer undone by {by or 'operator'}",
        {"attention_id": item["id"]},
    )
    return {"ok": True, "item": registry.get_attention(item["id"]),
            "delivery": {"mode": "undone"}}


# --------------------------------------------------------------------------- #
# Waiting (used by the MCP ask tool)
# --------------------------------------------------------------------------- #
def wait(attention_id: int, seconds: float, *, poll: float = 1.0) -> Optional[str]:
    """Block until the item is answered/closed or *seconds* elapse.

    The heartbeat written each poll is what tells :func:`answer` not to inject a
    prompt: the answer travels back as this call's tool result instead.
    """
    deadline = time.time() + max(0.0, float(seconds))
    while True:
        item = registry.get_attention(attention_id)
        if not item:
            return None
        if item["state"] != "open":
            return str(item.get("answer") or "")
        if item["ttl_epoch"] and int(item["ttl_epoch"]) <= int(time.time()):
            registry.expire_attention()
            item = registry.get_attention(attention_id)
            return str((item or {}).get("answer") or "")
        registry.touch_attention_waiter(attention_id)
        if time.time() >= deadline:
            # Close the race with an answer that landed just after the heartbeat:
            # if the item is no longer open, hand it back instead of "pending".
            latest = registry.get_attention(attention_id)
            if latest and latest["state"] != "open":
                return str(latest.get("answer") or "")
            return None
        time.sleep(max(0.2, poll))


def expire_stale() -> int:
    return registry.expire_attention()


def drain_outbox(limit: int = 20) -> int:
    """Post operator answers queued while the PerunChat socket was down.

    Called on every bridge (re)connect and from the notifier tick, so a reply
    typed during an outage is delivered as soon as the room is reachable. A
    failure increments ``attempts`` (and is logged) instead of losing the reply;
    after ten attempts the row is left for the dashboard to show.
    """
    from . import perunchat_bridge  # local import: the bridge imports this module

    bridge = perunchat_bridge.get_bridge()
    if bridge is None or not bridge.is_connected():
        return 0
    sent = 0
    for row in registry.attention_outbox_pending(limit=limit):
        if int(row.get("attempts") or 0) >= 10:
            # Give up visibly instead of retrying forever (and after the parent
            # item is pruned, pointing at a row that no longer exists).
            registry.attention_outbox_done(row["id"])
            registry.log_activity(
                None, "attention_failed",
                f"queued operator answer {row['id']} abandoned after 10 attempts",
                {"outbox_id": row["id"], "attention_id": row.get("attention_id"),
                 "room_id": row.get("target_id"),
                 "error": row.get("last_error") or ""},
            )
            continue
        payload = row.get("payload") or {}
        room_id = str(row.get("target_id") or "")
        try:
            posted = bridge.post_to_room(
                room_id,
                str(payload.get("text") or ""),
                in_reply_to=str(payload.get("in_reply_to") or "") or None,
            )
        except Exception as exc:  # pragma: no cover - depends on the socket
            registry.attention_outbox_failed(row["id"], str(exc))
            continue
        if posted:
            registry.attention_outbox_done(row["id"])
            registry.log_activity(
                None, "attention_delivered",
                f"queued operator answer {row['id']} posted in {room_id}",
                {"outbox_id": row["id"], "attention_id": row.get("attention_id")},
            )
            sent += 1
        else:
            registry.attention_outbox_failed(row["id"], "bridge rejected the message")
    return sent


__all__ = [
    "DeliveryError", "PERMISSION_OPTIONS", "PROPOSAL_OPTIONS",
    "answer", "choose", "confirmation", "drain_outbox", "eligible", "expire_stale",
    "followup_keyboard", "keyboard", "project_for_location", "recipients",
    "render", "request", "set_delivery", "undo", "wait",
]
