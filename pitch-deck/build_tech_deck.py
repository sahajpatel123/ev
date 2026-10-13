"""Visual investor deck v2. Native text, explanatory diagrams and budget chart.
Original generated artwork is conceptual, never a product screenshot.
Run: python3 pitch-deck/build_tech_deck.py
"""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'EV_Investor_Deck_Tech.pdf'
W, H = 1280, 720
NAVY, BLUE, VIOLET = '#080D24', '#4B6BFF', '#AA91FF'
WHITE, ICE, GRAY, DARK = '#F5F7FF', '#DFE7FF', '#A9B6D7', '#17213E'
CYAN = '#78DFFF'
for name, file in [('Regular', 'Arial.ttf'), ('Bold', 'Arial Bold.ttf'), ('Display', 'Arial Black.ttf')]:
    pdfmetrics.registerFont(TTFont(name, f'/System/Library/Fonts/Supplemental/{file}'))
lines = []
page = 0
backdrop = NAVY


def t(c, s, x, y, size=26, color=WHITE, font='Regular'):
    w = pdfmetrics.stringWidth(s, font, size)
    assert x >= 0 and x + w < W - 44, (page, s, x, w)
    assert y >= 34 and y + size <= H, (page, s, y)
    c.setFillColor(HexColor(color)); c.setFont(font, size)
    c.drawString(x, y, s)
    lines.append((page, s))


def p(c, s, x, y, width, size=26, color=WHITE, font='Regular', leading=None):
    out, cur = [], ''
    for word in s.split():
        trial = (cur + ' ' + word).strip()
        if cur and pdfmetrics.stringWidth(trial, font, size) > width:
            out.append(cur); cur = word
        else:
            cur = trial
    if cur: out.append(cur)
    for line in out:
        t(c, line, x, y, size, color, font)
        y -= leading or size * 1.27
    return y


def rule(c, x1, y1, x2, y2, color=BLUE, width=2):
    c.setStrokeColor(HexColor(color)); c.setLineWidth(width)
    c.line(x1, y1, x2, y2)


def bg(c, n, section, color=NAVY, light=False, art=False):
    global page, backdrop
    page = n
    backdrop = color
    c.setFillColor(HexColor(color)); c.rect(0, 0, W, H, fill=1, stroke=0)
    if art:
        c.drawImage(str(ROOT / 'assets/memory-ribbon.png'), 0, 0, width=W, height=H)
    t(c, 'EV', 64, 654, 24, NAVY if light else WHITE, 'Display')
    secondary = DARK if light else (WHITE if color == BLUE else GRAY)
    t(c, section.upper(), 127, 658, 12, secondary, 'Bold')
    t(c, f'{n:02d}', 1170, 40, 12, secondary)


def head(c, s, color=WHITE, size=57):
    p(c, s, 64, 552, 1140, size, color, 'Display', size * 1.12)


def foot(c, s, light=False):
    p(c, s, 64, 77, 1100, 14, DARK if light else (WHITE if backdrop == BLUE else GRAY))


def step(c, x, n, label, sub):
    # Semantic sequence node, not decorative artwork.
    c.setFillColor(HexColor(BLUE)); c.circle(x, 353, 24, fill=1, stroke=0)
    t(c, str(n), x - 7, 344, 25, WHITE, 'Bold')
    t(c, label, x - 58, 280, 30, WHITE, 'Bold')
    p(c, sub, x - 58, 226, 222, 23, GRAY)


def build():
    c = canvas.Canvas(str(OUT), pagesize=(W, H), pageCompression=1)
    c.setTitle('EV | Your life, remembered')
    c.setAuthor('EV'); c.setSubject('Visual investor proposal | October 2026')

    bg(c, 1, 'Personal AI / Investor proposal', art=True)
    t(c, 'Your life,', 64, 502, 76, WHITE, 'Display')
    t(c, 'remembered.', 64, 408, 76, VIOLET, 'Display')
    p(c, 'Personal AI that remembers your context and helps you act.', 68, 295, 485, 28)
    t(c, 'One owner. Across their devices.', 68, 140, 22, ICE)
    c.showPage()

    bg(c, 2, 'The problem', BLUE)
    head(c, 'Your context is everywhere.', size=53)
    # Fragmented information map: the owner must reconstruct relationships.
    for x, y, label, example in [
        (90, 391, 'NOTES', 'Why we chose this'),
        (835, 405, 'CHATS', 'What we promised'),
        (147, 180, 'CALENDAR', 'What happens next'),
        (795, 198, 'FILES', 'Where the detail lives'),
    ]:
        t(c, label, x, y, 15, ICE, 'Bold')
        t(c, example, x, y - 43, 27, WHITE, 'Bold')
    t(c, 'YOU', 525, 300, 69, WHITE, 'Display')
    for x1, y1, x2, y2 in [(352,351,483,321),(793,365,702,322),(425,189,499,271),(759,180,705,269)]:
        rule(c,x1,y1,x2,y2,ICE,1.5)
    foot(c, 'You spend time rebuilding the story before you can move forward.')
    c.showPage()

    bg(c, 3, 'A moment with EV')
    head(c, 'Pick up where you left off.', size=53)
    rule(c, 95, 395, 1120, 395, BLUE, 3)
    for x in [96, 652]:
        c.setFillColor(HexColor(VIOLET)); c.circle(x,395,7,fill=1,stroke=0)
    t(c, 'MONDAY', 96, 430, 15, VIOLET, 'Bold')
    t(c, 'THREE WEEKS LATER', 652, 430, 15, VIOLET, 'Bold')
    p(c, '"Delay launch until payments are fixed."', 96, 333, 440, 34, WHITE, 'Bold')
    p(c, '"Why did I delay the launch?"', 652, 333, 500, 34, WHITE, 'Bold')
    p(c, 'EV recalls the payment blocker.', 652, 213, 510, 27)
    t(c, 'Source: your Monday decision note', 652, 159, 20, CYAN)
    foot(c, 'Illustrative interaction. You can inspect the source or correct the memory.')
    c.showPage()

    bg(c, 4, 'How it works')
    head(c, 'Context becomes a next step.', size=51)
    rule(c, 146, 353, 1095, 353, BLUE, 3)
    for x, n, label, sub in [
        (146,1,'Capture','Speak or save a note.'),
        (438,2,'Recall','Find the relevant history.'),
        (730,3,'Permission','Check the action rules.'),
        (1022,4,'Act','Do the approved task.'),
    ]:
        step(c,x,n,label,sub)
    foot(c, 'Designed flow. Memory, retrieval and approval machinery exist; pilot testing must prove reliability.')
    c.showPage()

    bg(c, 5, 'Owner control')
    head(c, 'Your memory. Your rules.', size=55)
    # Architecture boundary diagram: storage and devices stay inside owner instance.
    c.setStrokeColor(HexColor(BLUE)); c.setLineWidth(2)
    c.rect(64, 168, 552, 274, fill=0, stroke=1)
    t(c, 'YOUR OWN INSTANCE', 94, 404, 15, VIOLET, 'Bold')
    t(c, 'Devices + memory', 94, 332, 37, WHITE, 'Bold')
    t(c, 'Inspect. Correct. Export.', 94, 253, 24, ICE)
    rule(c,616,304,802,304,BLUE,3)
    t(c, 'Privacy rules', 649, 347, 20, CYAN, 'Bold')
    t(c, 'Allowed context', 633, 265, 18, GRAY)
    t(c, 'Hosted AI', 850, 329, 36, WHITE, 'Bold')
    p(c, 'A replaceable model layer', 850, 276, 320, 24, GRAY)
    foot(c, 'One owner per installation. Local-first storage, permissioned cloud reasoning. Setup and security review matter.')
    c.showPage()

    bg(c, 6, 'Evidence today', WHITE, light=True)
    head(c, 'Built to demonstrate. Ready to validate.', NAVY, 45)
    rule(c, 64, 435, 1188, 435, BLUE, 2)
    t(c, 'SOFTWARE EXISTS', 64, 373, 18, BLUE, 'Bold')
    t(c, 'THE NEXT PROOF', 694, 373, 18, BLUE, 'Bold')
    for y,s in [(313,'Memory + source history'),(252,'Privacy + action controls'),(191,'Web, CLI + Apple client code')]:
        t(c,s,64,y,30,NAVY,'Bold')
    for y,s in [(313,'Reliable daily use'),(252,'Simple setup + device testing'),(191,'Retention + willingness to pay')]:
        t(c,s,694,y,28,NAVY,'Bold')
    foot(c, 'Repository evidence, not production certification. No verified traction supplied. Mobile release readiness remains unverified.', True)
    c.showPage()

    bg(c, 7, 'Positioning')
    head(c, 'A memory layer the owner runs.', size=51)
    t(c, 'ChatGPT', 64, 414, 28, WHITE, 'Bold')
    t(c, 'Conversation memory', 64, 367, 23, GRAY)
    t(c, 'Apple Intelligence', 652, 414, 28, WHITE, 'Bold')
    t(c, 'Personal context + private compute', 652, 367, 23, GRAY)
    rule(c,64,316,1188,316,BLUE,2)
    t(c, 'EV', 64, 204, 73, VIOLET, 'Display')
    p(c, 'Owner-operated memory. Inspectable sources. Model choice.', 271, 252, 850, 40, WHITE, 'Bold')
    foot(c, 'Proposed positioning, not exclusivity. Official OpenAI and Apple sources are linked in the appendix.')
    c.showPage()

    bg(c, 8, 'First customers', BLUE)
    t(c, '50', 64, 418, 152, WHITE, 'Display')
    t(c, 'pilot owners', 76, 356, 33, WHITE, 'Bold')
    p(c, 'Independent professionals with projects to remember.', 588, 505, 610, 44, WHITE, 'Display')
    p(c, 'Start with project recall and a daily brief. Recruit through direct demos.', 592, 328, 565, 25, ICE)
    t(c, '60%', 76, 203, 48, WHITE, 'Display')
    t(c, 'active in week eight', 76, 159, 22, ICE)
    t(c, '90%', 592, 203, 48, WHITE, 'Display')
    t(c, 'sourced-answer accuracy', 592, 159, 22, ICE)
    foot(c, 'Proposed pilot and targets, not results. Separate self-hosted instances. Evaluate consented recall tasks and support costs.')
    c.showPage()

    bg(c, 9, 'Business model')
    t(c, '$19', 64, 411, 155, WHITE, 'Display')
    t(c, 'per owner / month', 77, 347, 32, VIOLET, 'Bold')
    t(c, 'PROPOSED PRICE', 79, 284, 16, GRAY, 'Bold')
    t(c, 'Maintained software', 655, 449, 33, WHITE, 'Bold')
    t(c, 'Updates + support', 655, 391, 28, GRAY)
    rule(c,655,345,1186,345,BLUE,2)
    t(c, 'Hosted AI usage', 655, 293, 33, WHITE, 'Bold')
    p(c, 'Measured allowance or separate usage charge', 655, 246, 510, 25, GRAY)
    foot(c, 'Pricing hypothesis. Owners provide hardware. Pilot testing must establish willingness to pay, retention and delivery costs.')
    c.showPage()

    bg(c, 10, 'Funding proposal')
    t(c, '$275K', 64, 466, 102, WHITE, 'Display')
    t(c, '12 months to prove daily value', 66, 399, 34, VIOLET, 'Bold')
    # Native proportional budget chart, four allocations summing to the ask.
    allocations=[150,60,40,25]
    assert sum(allocations)==275
    x=64
    for amount,color in zip(allocations,[BLUE,VIOLET,'#DCE5FF','#64729A']):
        width=1120*amount/275
        c.setFillColor(HexColor(color)); c.rect(x,294,width,51,fill=1,stroke=0)
        x+=width
    for x,amount,label,sub,color in [
        (64,'$150K','Engineering','Setup + reliability',BLUE),
        (365,'$60K','Security','Review + remediation',VIOLET),
        (668,'$40K','Pilot','Onboarding + support','#DCE5FF'),
        (969,'$25K','Operations','Infra + contingency','#64729A'),
    ]:
        c.setFillColor(HexColor(color)); c.rect(x,269,25,8,fill=1,stroke=0)
        t(c,amount,x,234,31,WHITE,'Bold')
        t(c,label,x,184,24,VIOLET,'Bold')
        t(c,sub,x,149,17,GRAY)
    foot(c, 'Proposed grant or pre-seed program. Indicative allocation, not quotes. Funding structure and terms remain open.')
    c.showPage()

    bg(c, 11, 'The long-term thesis', art=True)
    t(c, 'The more life', 64, 497, 63, WHITE, 'Display')
    t(c, 'you bring,', 64, 415, 63, WHITE, 'Display')
    t(c, 'the more context', 64, 333, 55, VIOLET, 'Display')
    t(c, 'EV can use.', 64, 258, 63, VIOLET, 'Display')
    p(c, 'Back the next proof: daily use, trusted recall, willingness to pay.', 68, 150, 595, 26)
    p(c, 'Thesis: useful history grows over time. Value must be proven in real use.',64,77,510,14,GRAY)
    c.showPage()

    bg(c,12,'Appendix / Evidence & assumptions',WHITE,light=True)
    head(c,'The evidence behind the story',NAVY,44)
    t(c,'PRODUCT',64,453,17,BLUE,'Bold')
    p(c,'EV repository: memory/history/writer services, privacy boundary, tool approval machinery, README.md and SECURITY.md. Apple client code exists; mobile release readiness is unverified. Code presence does not prove production quality.',64,415,1115,21,NAVY)
    t(c,'COMPETITION',64,293,17,BLUE,'Bold')
    for y,label,url in [
        (254,'OpenAI memory','https://help.openai.com/en/articles/8590148-memory-faq'),
        (215,'Apple Intelligence privacy','https://www.apple.com/legal/privacy/data/en/intelligence-engine/'),
    ]:
        t(c,label,64,y,19,NAVY,'Bold'); t(c,url,342,y,15,BLUE)
        c.linkURL(url,(342,y-3,1165,y+19),relative=0)
    p(c,'All pricing, funding allocations, pilot sizes and quality targets are proposals. No verified revenue, customer retention, founder credentials or investment terms were supplied. Hosted reasoning requires connectivity. Artwork is conceptual, not a product screenshot.',64,152,1115,18,DARK)
    c.showPage(); c.save()
    print(f'{OUT}: 12 pages; {len(lines)} checked text lines')


if __name__ == '__main__':
    build()
