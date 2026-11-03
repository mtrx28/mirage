"""A PDF view of an evidence packet. The JSON packet is the record; this is a one-or-two-page rendering of it for a human to read.

    python -m mirage.packetpdf data/packets/<call id>.json            # writes data/packets/<call id>.pdf next to it

How: the packet is turned into one self-contained HTML page (no scripts, no external files, a content-security policy that allows none), and a
headless Edge or Chrome that is already installed prints it to PDF. A browser is used because it shapes Devanagari properly (conjuncts and
vowel signs) with the system's fonts; a hand-rolled PDF writer would not. Nothing is sent anywhere: the page is a local file.

The packet is verified first (hash and quotes); a packet that does not verify is refused. The PDF is not itself hashed, so it says that the
JSON file is what the hash covers. It makes no claim about legal admissibility.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from mirage.packet import verify_packet

BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/microsoft-edge", "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
)
EXCERPT_MAX = 14

CSS = """
@page { size: A4; margin: 11mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "Nirmala UI", "Noto Sans Devanagari", "Mangal", "Segoe UI", Arial, sans-serif; font-size: 8.6pt; line-height: 1.32; color: #111; margin: 0; }
.banner { border: 1.6px solid #7a4b00; background: #fff4d6; padding: 5px 8px; font-weight: 700; font-size: 9pt; margin-bottom: 7px; }
h1 { font-size: 13pt; margin: 0 0 1px 0; }
.sub { color: #444; font-size: 8pt; margin-bottom: 6px; }
h2 { font-size: 9.6pt; margin: 8px 0 3px 0; padding-bottom: 1px; border-bottom: 1px solid #999; }
table { border-collapse: collapse; width: 100%; margin: 2px 0; }
th, td { border: 1px solid #bbb; padding: 2px 4px; vertical-align: top; text-align: left; }
th { background: #eee; font-weight: 600; }
td.k { width: 26%; background: #f6f6f6; font-weight: 600; }
.mono { font-family: Consolas, "Courier New", monospace; font-size: 7.6pt; word-break: break-all; }
.small { font-size: 7.6pt; color: #333; }
.none { color: #555; font-style: italic; }
tr { page-break-inside: avoid; }
h2 { page-break-after: avoid; }
"""


def find_browser() -> str | None:
    env = os.environ.get("MIRAGE_BROWSER")
    if env and Path(env).exists():
        return env
    for c in BROWSER_CANDIDATES:
        if Path(c).exists():
            return c
    for name in ("msedge", "google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- choosing the excerpt

def excerpt_segments(packet: dict, limit: int = EXCERPT_MAX) -> list[dict]:
    """A short, honest excerpt: the segments that carry intel (and their neighbours), the first and the last few. Never reworded."""
    segs = packet["transcript_native"]
    if not segs:
        return []
    pick: set[int] = set()
    pieces = [p.strip() for item in packet["intel"] for p in item["quote"].split(" | ") if p.strip()]
    for i, s in enumerate(segs):
        if any(p in s["text"] for p in pieces):
            pick.update({i - 1, i, i + 1})
    pick.update({0, 1, 2, len(segs) - 3, len(segs) - 2, len(segs) - 1})
    keep = sorted(i for i in pick if 0 <= i < len(segs))
    if len(keep) > limit:                                     # too many: keep the intel ones first, then the ends
        core = sorted({i for i, s in enumerate(segs) if any(p in s["text"] for p in pieces)})
        ends = [0, len(segs) - 1]
        keep = sorted(set((core + ends)[:limit]) | set(keep[:max(0, limit - len(core) - 2)]))[:limit]
    return [dict(segs[i], index=i) for i in keep]


# --------------------------------------------------------------------------- the HTML

def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _kv(rows: list[tuple[str, str]]) -> str:
    return "<table>" + "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>' for k, v in rows) + "</table>"


def render_html(packet: dict) -> str:
    integ = packet["integrity"]
    intel_rows = ""
    for i in packet["intel"]:
        notes = []
        if i.get("possibly_part_of"):
            notes.append(f"possibly part of {i['possibly_part_of']}")
        if i.get("candidates"):
            notes.append("could be " + " or ".join(i["candidates"]))
        if i.get("reason"):
            notes.append(i["reason"])
        if i.get("suggested"):
            notes.append(f"a guess, not the value: {i['suggested']}")
        intel_rows += (f"<tr><td>{_e(i['kind'])}</td><td class='mono'>{_e(i['value'])}</td><td>{_e(i['confidence'])}</td>"
                       f"<td>{'yes' if i['needs_confirmation'] else 'no'}</td><td>{'yes' if i['valid'] else 'no'}</td>"
                       f"<td>{_e(i['quote'])}</td><td>{_e(i['t'])}</td><td class='small'>{_e('; '.join(notes))}</td></tr>")
    if intel_rows:
        intel_html = ("<table><tr><th>kind</th><th>value as heard</th><th>confidence</th><th>needs confirmation</th><th>valid</th>"
                      "<th>quote (verbatim)</th><th>t (s)</th><th>notes</th></tr>" + intel_rows + "</table>")
    else:
        intel_html = '<p class="none">No intel items were captured in this call.</p>'
    s = packet["intel_summary"]
    labels = "".join(f"<li><b>{_e(k)}</b>: {_e(v)}</li>" for k, v in s.get("labels", {}).items())
    pay = ""
    if packet["payment_intel"]:
        pay = "<p><b>Payment intel:</b> " + "; ".join(f"{_e(p['type'])} {_e(p['value'])} ({_e(p['confidence'])})" for p in packet["payment_intel"]) + "</p>"
    elif packet.get("payment_intel_note"):
        pay = f"<p><b>Payment intel:</b> {_e(packet['payment_intel_note'])}</p>"

    stage_rows = "".join(
        f"<tr><td>{_e(g['stage'])}</td><td>{_e(g['from_s'])} to {_e(g['to_s'])}</td>"
        f"<td class='small'>{_e('; '.join(g.get('entered_because', [])) or 'start of the call')}</td></tr>" for g in packet["stage_timeline"])

    ex = excerpt_segments(packet)
    ex_rows = "".join(f"<tr><td>{_e(x['t'])}</td><td>{_e(x['language'] or '')}</td><td>{_e(x['text'])}</td></tr>" for x in ex)
    ex_html = (f"<table><tr><th>t (s)</th><th>lang</th><th>caller, as transcribed (codemix)</th></tr>{ex_rows}</table>"
               f"<p class='small'>Excerpt: {len(ex)} of {len(packet['transcript_native'])} caller segments; Uncle's {len(packet['uncle_lines'])} lines and the "
               "full text are in the JSON. Not shown: anything the caller said while Uncle's voice played (the microphone was silenced then).</p>"
               if ex else "<p class='none'>No transcript segments.</p>")

    end, dis = packet["ending"], packet["disclosure"]
    said = f"yes, first at {_e(dis['first_said_at_s'])} s" if dis["said_automated_assistant"] else "no"
    iq = dis["identity_questions"]
    ending_html = _kv([
        ("How the call ended", f"<b>{_e(end['type'])}</b> ({_e(end['end_reason_raw'])}) at {_e(end['ended_at_s'])} s. {_e(end['detail'])}"),
        ("Uncle said he is an automated assistant", f"{said}. Identity questions: {iq['deflected']} deflected in character, {iq['answered_honestly']} answered honestly."),
        ("Disclosure policy", _e(dis["policy"])),
    ])

    summary = _kv([
        ("Call", f"<span class='mono'>{_e(packet['call_id'])}</span> (started {_e(packet['started_at'])} UTC)"),
        ("Duration", f"{_e(packet['duration_s'])} s"),
        ("Language", f"{_e(packet['language'])} requested; detected per segment: {_e(json.dumps(packet['languages_detected']))}; format {_e(packet['transcript_format'])}"),
        ("Scenario", f"{_e(packet['scenario'])} <span class='small'>(the project's keyword guess, not a finding)</span>"),
        ("Agent", f"{_e(packet['agent']['name'])}; automated"),
        ("Caller audio silenced", f"{_e(packet['capture']['caller_audio_muted_s'])} s (while Uncle spoke)"),
        ("Intel", f"{s['count']} item(s); by confidence {_e(json.dumps(s['by_confidence']))}; {s['needs_confirmation']} need confirmation"),
    ])

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>Evidence packet {_e(packet['call_id'])}</title><style>{CSS}</style></head><body>
<div class="banner">{_e(packet['label'])}</div>
<h1>Evidence packet: call {_e(packet['call_id'])}</h1>
<div class="sub">A PDF view of the JSON packet (schema {_e(packet['schema'])}), generated {_e(packet['generated_at'])}. The JSON file is the record.</div>
<h2>Call summary</h2>{summary}
<h2>Intel</h2>{intel_html}{pay}
<p class="small"><b>Confidence</b> is the extractor's own label, copied from the call; not a probability, and nothing has been upgraded.
<b>t</b> is {_e(s['t_is'])}.</p>{('<ul class="small">' + labels + '</ul>') if labels else ''}
<h2>Stage timeline</h2><table><tr><th>stage</th><th>from to (s)</th><th>entered because (rule hits)</th></tr>{stage_rows}</table>
<p class="small">{_e(packet['stage_timeline_note'])}</p>
<h2>Transcript excerpt</h2>{ex_html}
<h2>Ending and disclosure</h2>{ending_html}
<h2>Integrity</h2>{_kv([
        ("sha256", f"<span class='mono'>{_e(integ['sha256'])}</span>"),
        ("generated_at", _e(integ["generated_at"])),
        ("covers", _e(integ["covers"])),
        ("verify", f"<span class='mono'>python -m mirage.packet verify data/packets/{_e(packet['call_id'])}.json</span>"),
        ("note", _e(integ["note"]) + " This PDF is a rendering; the hash covers the JSON file, not this PDF."),
    ])}
</body></html>
"""


# --------------------------------------------------------------------------- printing

def html_to_pdf(html_path: Path, pdf_path: Path, browser: str | None = None, timeout: float = 90) -> None:
    browser = browser or find_browser()
    if not browser:
        raise RuntimeError("No Edge or Chrome found. Install one, or set MIRAGE_BROWSER to its executable.")
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if pdf_path.exists():
        pdf_path.unlink()
    with tempfile.TemporaryDirectory(prefix="mirage_pdf_") as profile:
        cmd = [browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run", "--disable-extensions",
               "--disable-background-networking", f"--user-data-dir={profile}", f"--print-to-pdf={pdf_path.resolve()}", html_path.resolve().as_uri()]
        subprocess.run(cmd, check=False, timeout=timeout, capture_output=True)
    if not pdf_path.exists() or pdf_path.stat().st_size < 1000:
        raise RuntimeError("The browser did not produce a PDF.")


def pdf_page_count(path: Path) -> int:
    data = Path(path).read_bytes()
    counts = [int(m) for m in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)] + \
             [int(m) for m in re.findall(rb"/Count\s+(\d+)[^>]*?/Type\s*/Pages\b", data)]
    if counts:
        return max(counts)
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", data))


def build_pdf(packet_path: Path, pdf_path: Path | None = None, browser: str | None = None) -> Path:
    packet_path = Path(packet_path)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    problems = verify_packet(packet)
    if problems:
        raise ValueError("The packet does not verify, so no PDF was made: " + "; ".join(problems))
    pdf_path = Path(pdf_path) if pdf_path else packet_path.with_suffix(".pdf")
    with tempfile.TemporaryDirectory(prefix="mirage_html_") as tmp:
        page = Path(tmp) / "packet.html"
        page.write_text(render_html(packet), encoding="utf-8")
        html_to_pdf(page, pdf_path, browser)
    return pdf_path


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Make a PDF view of an evidence packet (offline; needs Edge or Chrome installed).")
    p.add_argument("packets", nargs="+", type=Path)
    p.add_argument("--browser", default=None)
    args = p.parse_args(argv)
    for packet in args.packets:
        out = build_pdf(packet, browser=args.browser)
        print(f"{packet.name} -> {out}  ({pdf_page_count(out)} page(s), {out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
