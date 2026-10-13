"""Original investor PDF. Build with python3 pitch-deck/build_investor_deck.py.

The supplied PDF is preserved. Facts, proposals and examples are distinguished.
Typography is the visual language; no simulated product screenshots.
"""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'EV_Investor_Deck.pdf'
W, H = 1280, 720
PAPER, INK, MUTED, BLUE = '#F5F3ED', '#172C31', '#54656A', '#155E75'
WHITE, LIME = '#FFFFFF', '#D8F293'
checks = []


def text(c, s, x, y, size=24, color=INK, bold=False):
    font = 'Helvetica-Bold' if bold else 'Helvetica'
    width = pdfmetrics.stringWidth(s, font, size)
    assert x + width <= W - 60, (s, x, width)
    assert y >= 35, (s, y)
    c.setFont(font, size)
    c.setFillColor(HexColor(color))
    c.drawString(x, y, s)
    checks.append((s, x, y, width, size))


def para(c, s, x, y, width=1000, size=24, color=INK, bold=False, leading=None):
    font = 'Helvetica-Bold' if bold else 'Helvetica'
    lines, current = [], ''
    for word in s.split():
        candidate = (current + ' ' + word).strip()
        if pdfmetrics.stringWidth(candidate, font, size) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    for line in lines:
        text(c, line, x, y, size, color, bold)
        y -= leading or size * 1.32
    return y


def base(c, n, label, dark=False):
    bg, fg = (INK, PAPER) if dark else (PAPER, INK)
    c.setFillColor(HexColor(bg))
    c.rect(0, 0, W, H, fill=1, stroke=0)
    text(c, 'EV', 76, 650, 21, LIME if dark else BLUE, True)
    text(c, label.upper(), 135, 651, 13, '#B9C9CB' if dark else MUTED)
    text(c, f'{n:02d}', 1160, 42, 12, '#B9C9CB' if dark else MUTED)


def title(c, s, color=INK, size=46):
    return para(c, s, 76, 561, 1110, size, color, True, size * 1.12)


def note(c, s, dark=False):
    para(c, s, 76, 83, 1090, 13, '#B9C9CB' if dark else MUTED)


def row(c, y, label, body, dark=False):
    fg = PAPER if dark else INK
    text(c, label, 76, y, 26, LIME if dark else BLUE, True)
    return para(c, body, 402, y, 760, 23, fg)


def build():
    c = canvas.Canvas(str(OUT), pagesize=(W, H), pageCompression=1)
    c.setTitle('EV | Personal AI with memory you control')
    c.setAuthor('EV')
    c.setSubject('Investor and grant proposal | October 2026')

    base(c, 1, 'Investor & grant proposal / October 2026', True)
    text(c, 'Personal AI.', 76, 495, 78, PAPER, True)
    text(c, 'Memory you control.', 76, 395, 78, LIME, True)
    para(c, 'EV helps you pick up where you left off: recalling your decisions, showing their sources, and helping you act with permission.', 80, 277, 930, 28, PAPER)
    text(c, 'One person. Their own memory. Across their devices.', 80, 133, 22, '#B9C9CB')
    c.showPage()

    base(c, 2, 'The problem')
    title(c, 'Your context is scattered across your life')
    para(c, 'A decision lives in a chat. The reason sits in a note. The next step is buried in a calendar.', 76, 440, 1050, 30)
    row(c, 318, 'When you return', 'You search across apps and rebuild what you already knew.')
    row(c, 221, 'When you ask AI', 'You still need to supply the right history and check its answer.')
    note(c, 'Initial customer hypothesis: independent professionals managing long-running projects and personal commitments.')
    c.showPage()

    base(c, 3, 'The experience')
    title(c, 'A useful answer includes the reason')
    text(c, 'YOU ASK', 76, 433, 15, BLUE, True)
    para(c, '"Why did I delay the launch?"', 76, 384, 1090, 39, INK, True)
    text(c, 'EV RECALLS', 76, 286, 15, BLUE, True)
    para(c, '"You wanted to resolve the payment issue first. Your decision note from Monday names it as the blocker."', 76, 241, 1010, 29)
    text(c, 'You can open the source, correct the memory, or ask for the next step.', 76, 138, 23, BLUE)
    note(c, 'Illustrative interaction, not a customer testimonial or measured production result.')
    c.showPage()

    base(c, 4, 'Product design', True)
    title(c, 'A personal memory with an audit trail', PAPER)
    for x, label, body in [
        (76, 'Capture', 'Speak naturally or add a note. Keep the events you choose to share.'),
        (450, 'Recall', 'Bring back relevant memories with dates and source links.'),
        (824, 'Act', 'Use connected tools under approval rules. Keep a record of what happened.'),
    ]:
        text(c, label, x, 420, 37, LIME, True)
        para(c, body, x, 352, 325, 26, PAPER)
    text(c, 'One continuous conversation, grounded in the owner\'s history.', 76, 174, 27, PAPER)
    note(c, 'Repository implementation: immutable events, versioned memories, retrieval, privacy filters and tool approval machinery. End-to-end reliability remains to be proven in a pilot.', True)
    c.showPage()

    base(c, 5, 'Ownership & privacy')
    title(c, 'The owner controls the memory layer')
    row(c, 425, 'Your own installation', 'Each person runs a separate, self-hosted instance. EV has one owner per instance.')
    row(c, 304, 'Selective model access', 'Privacy rules restrict which context can reach hosted AI providers.')
    row(c, 183, 'Portable history', 'Export and correction paths keep the memory independent of the model provider.')
    note(c, 'Local-first storage does not mean fully offline AI. Current reasoning and live speech use hosted services. Security depends on correct setup and needs independent review.')
    c.showPage()

    base(c, 6, 'Current evidence')
    title(c, 'Software exists. Customer proof comes next.')
    row(c, 430, 'In the repository', 'Memory and provenance services, privacy boundaries, tool workflows, web/CLI clients and native Apple client code.')
    row(c, 303, 'Productization work', 'Reliable daily use, simpler installation, independent security review and validation on target devices.')
    row(c, 177, 'Commercial evidence', 'No verified paying-customer count, retention cohort or revenue data supplied for this pitch.')
    note(c, 'Evidence is source-code and repository documentation, not a fresh full-suite or production acceptance result. Mobile release readiness is unverified.')
    c.showPage()

    base(c, 7, 'Why this opportunity')
    title(c, 'Memory and privacy are becoming baseline')
    row(c, 432, 'ChatGPT', 'Offers memory across conversations. Persistence alone is already competitive territory.')
    row(c, 323, 'Apple Intelligence', 'Combines personal context with on-device processing and Private Cloud Compute.')
    row(c, 205, 'EV\'s proposed wedge', 'An owner-operated memory system with source history and a replaceable model layer.')
    note(c, 'Sources: OpenAI Memory FAQ and Apple Intelligence privacy documentation, linked in the appendix. EV differentiation is a product thesis, not a claim of exclusive features.')
    c.showPage()

    base(c, 8, 'First customers')
    title(c, 'A narrow pilot before a broad launch')
    text(c, '50', 76, 382, 110, BLUE, True)
    text(c, 'proposed pilot owners', 76, 330, 23)
    para(c, 'Independent professionals who already use AI and can run a personal Mac/home-server setup.', 525, 439, 655, 26)
    para(c, 'Begin with project recall and a daily brief. Recruit through founder-led demos and professional communities.', 525, 321, 655, 26)
    text(c, '60%', 76, 203, 42, BLUE, True)
    text(c, 'active in week eight', 76, 163, 21)
    text(c, '90%', 525, 203, 42, BLUE, True)
    text(c, 'sourced-answer accuracy', 525, 163, 21)
    note(c, 'Targets, not results. Separate owner instances. Score recall on a consented evaluation set. Measure onboarding, support costs and willingness to pay alongside these targets.')
    c.showPage()

    base(c, 9, 'Business model', True)
    title(c, 'A subscription to test, with costs to prove', PAPER)
    text(c, '$19', 76, 385, 100, LIME, True)
    text(c, 'per owner / month', 76, 330, 26, PAPER)
    para(c, 'Proposed pricing for maintained software, updates and support. Owners provide the host hardware.', 565, 409, 630, 27, PAPER)
    para(c, 'Hosted AI usage needs a measured allowance or a separate usage charge. Pricing must cover inference, support and payment costs.', 565, 274, 630, 25, PAPER)
    text(c, '1,000 paying owners would produce $228,000 annualized revenue.', 76, 166, 24, PAPER)
    note(c, 'Scenario only: 1,000 x $19 x 12. Not a forecast, traction claim or margin estimate. Pilot testing must establish retention, acquisition cost and cost per active owner.', True)
    c.showPage()

    base(c, 10, 'Funding proposal')
    title(c, '$275,000 to validate everyday use')
    para(c, 'Proposed 12-month grant or pre-seed program. Funding buys a measured pilot and a safer, easier product.', 76, 437, 1050, 26)
    for x, y, amount, body in [
        (76, 324, '$150,000', 'Engineering: installation, reliability and the memory experience.'),
        (675, 324, '$60,000', 'Independent security review and remediation.'),
        (76, 194, '$40,000', 'Pilot onboarding and support.'),
        (675, 194, '$25,000', 'Infrastructure and contingency.'),
    ]:
        text(c, amount, x, y, 34, BLUE, True)
        para(c, body, x, y - 44, 490, 22)
    note(c, 'Indicative allocation, not committed quotes. Release readiness, pilot retention and measured delivery costs inform the next funding decision. Funding structure and terms remain open.')
    c.showPage()

    base(c, 11, 'The investment case', True)
    text(c, 'A memory that becomes', 76, 498, 62, PAPER, True)
    text(c, 'more useful over time.', 76, 421, 62, LIME, True)
    para(c, 'The long-term thesis is simple: useful history accumulates. If EV reliably brings that history into daily decisions, it can earn a lasting place in the owner\'s life.', 76, 295, 1030, 29, PAPER)
    text(c, 'The next proof: people return, trust the sources, and choose to pay.', 76, 150, 25, PAPER)
    note(c, 'Seeking a funding partner for the proposed validation program. Next discussion: live workflow demonstration, pilot plan and budget diligence.', True)
    c.showPage()

    base(c, 12, 'Appendix / evidence & assumptions')
    title(c, 'Sources and the limits of this proposal', size=39)
    para(c, 'Product evidence', 76, 451, 1030, 23, BLUE, True)
    para(c, 'EV repository: README.md, docs/SECURITY.md, docs/APPLE_CLIENTS.md, docs/FLEET_LAW.md, memory and security implementations. Reviewed October 11, 2026. Repository presence does not establish production quality.', 76, 411, 1090, 20)
    para(c, 'Competitive context', 76, 306, 1030, 23, BLUE, True)
    url1 = 'https://help.openai.com/en/articles/8590148-memory-faq'
    url2 = 'https://www.apple.com/legal/privacy/data/en/intelligence-engine/'
    text(c, 'OpenAI: Memory FAQ', 76, 267, 20)
    text(c, url1, 76, 239, 15, BLUE)
    c.linkURL(url1, (76, 235, 650, 259), relative=0)
    text(c, 'Apple: Apple Intelligence & Privacy', 76, 202, 20)
    text(c, url2, 76, 174, 15, BLUE)
    c.linkURL(url2, (76, 170, 740, 194), relative=0)
    note(c, 'Pricing, pilot size, budget, retention and quality targets are proposals. Market size, customer traction, founder credentials and investment terms were not supplied and are not asserted. No financial return is promised.')
    c.showPage()
    c.save()
    print(f'Created {OUT}: 12 pages; {len(checks)} checked text lines')


if __name__ == '__main__':
    build()
