"""Format matches as a Telegram (HTML) digest or plain text."""

from datetime import datetime
from html import escape

RESERVED_LABEL = {
    "youth": "🧑 Youth (AGPO)", "women": "👩 Women (AGPO)", "pwd": "♿ PWD (AGPO)",
    "agpo_any": "AGPO groups", "open": "Open to all", "unknown": "Not stated",
}
TG_LIMIT = 4000  # Telegram's hard limit is 4096 characters per message


def days_left(close_at, today=None):
    today = (today or datetime.now()).date()
    return (datetime.strptime(close_at[:10], "%Y-%m-%d").date() - today).days


def deadline_text(close_at, today=None):
    d = days_left(close_at, today)
    when = datetime.strptime(close_at[:10], "%Y-%m-%d").strftime("%a %d %b")
    return f"{when} ({'today' if d == 0 else 'tomorrow' if d == 1 else f'in {d} days'})"


def doc_link(m):
    ext = m.get("extraction") or {}
    if ext.get("doc_url"):
        return ext["doc_url"]
    docs = m.get("documents") or []
    return docs[0]["url"] if docs else "https://tenders.go.ke/tenders"


def format_match(m, today=None):
    ext = m.get("extraction") or {}
    lines = [
        f"<b>{escape(m['title'][:160])}</b>",
        f"🏛 {escape(m.get('entity') or '?')}" + (f" · {escape(m['county'])}" if m.get("county") else ""),
        f"⏰ Closes {deadline_text(m['close_at'], today)} · {escape(m.get('category') or '')}",
    ]
    if ext.get("status") == "ok":
        lines.append(f"🎯 Eligibility: {RESERVED_LABEL.get(ext.get('reserved_for'), 'Not stated')}"
                     + (f" · NCA {ext['nca_category']}" if ext.get("nca_category") else ""))
    if m.get("reason"):
        lines.append(f"💡 {escape(m['reason'])} (fit {m.get('score', 0):.0f}/10)")
    lines.append(f"📄 <a href=\"{escape(doc_link(m))}\">Tender document</a> · Ref {escape(m.get('tender_ref') or '')}")
    return "\n".join(lines)


def build_messages(profile_name, matches, today=None, new_only=False):
    """Return a list of Telegram-ready HTML messages (split to respect the size limit)."""
    head = f"📢 <b>Tender matches for {escape(profile_name)}</b>\n{(today or datetime.now()):%A %d %B %Y}"
    if not matches:
        return [head + "\n\nNo new matching tenders today."]
    head += f" · {len(matches)} {'new ' if new_only else ''}match{'es' if len(matches) != 1 else ''}"
    messages, current = [], head
    for i, m in enumerate(matches, 1):
        block = f"\n\n{i}. {format_match(m, today)}"
        if len(current) + len(block) > TG_LIMIT:
            messages.append(current)
            current = block.strip()
        else:
            current += block
    messages.append(current)
    footer = "\n\n<i>Source: PPIP, tenders.go.ke. Always confirm requirements in the official document.</i>"
    if len(messages[-1]) + len(footer) <= TG_LIMIT:
        messages[-1] += footer
    return messages
