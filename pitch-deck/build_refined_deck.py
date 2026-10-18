"""Five-page pitch inspired by the owner's reference, without modifying it.
Run: python3 pitch-deck/build_refined_deck.py
"""
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'EV_Pitch_Refined.pdf'
W,H=1280,720
PAPER,INK,BLUE,MUTED,PALE,WHITE='#F7F8FC','#1E2843','#526BC5','#68758F','#E9EDFA','#FFFFFF'
for name,file in [('Body','Arial.ttf'),('Strong','Arial Bold.ttf')]:
    pdfmetrics.registerFont(TTFont(name,f'/System/Library/Fonts/Supplemental/{file}'))
page=0
checks=[]

def text(c,s,x,y,size=26,color=INK,font='Body'):
    width=pdfmetrics.stringWidth(s,font,size)
    assert x>=0 and x+width<=W-58,(page,s,width)
    assert y>=35 and y+size<=H,(page,s,y)
    c.setFillColor(HexColor(color));c.setFont(font,size);c.drawString(x,y,s)
    checks.append((page,s))

def paragraph(c,s,x,y,width,size=27,color=INK,font='Body',leading=None):
    lines=[];current=''
    for word in s.split():
        trial=(current+' '+word).strip()
        if current and pdfmetrics.stringWidth(trial,font,size)>width:
            lines.append(current);current=word
        else:current=trial
    if current:lines.append(current)
    for line in lines:
        text(c,line,x,y,size,color,font);y-=leading or size*1.3
    return y

def start(c,n,section,dark=False):
    global page
    page=n
    c.setFillColor(HexColor(INK if dark else PAPER));c.rect(0,0,W,H,fill=1,stroke=0)
    text(c,'Ev',68,651,24,WHITE if dark else INK,'Strong')
    text(c,section,122,654,13,'#BDC8E2' if dark else MUTED)
    text(c,f'{n} / 5',1168,43,12,'#BDC8E2' if dark else MUTED)

def line(c,x,y,end,color=BLUE):
    c.setStrokeColor(HexColor(color));c.setLineWidth(2);c.line(x,y,end,y)

def center(c,s,y,size,color=INK,font='Body'):
    width=pdfmetrics.stringWidth(s,font,size)
    text(c,s,(W-width)/2,y,size,color,font)

def build():
    c=canvas.Canvas(str(OUT),pagesize=(W,H),pageCompression=1)
    c.setTitle('Meet Ev | Personal AI companion');c.setAuthor('EV')

    start(c,1,'A personal AI companion')
    center(c,'Meet Ev.',440,112,INK)
    center(c,'A little less to remember.',350,38,BLUE)
    center(c,'A little more room to live.',299,38,BLUE)
    center(c,'Talk naturally. Ev keeps the context and helps you take the next step.',203,24)
    center(c,'One owner. Their own memory. Across their devices.',117,21,MUTED)
    c.showPage()

    start(c,2,'The everyday problem')
    text(c,'Life moves fast.',68,508,62)
    text(c,'Details get lost.',68,430,62,BLUE)
    paragraph(c,'The appointment. The preference. The promise.',72,305,510,31)
    paragraph(c,'Ev brings the thread back together.',72,194,500,27)
    # Illustrative memory records, inspired by the source's floating notes.
    for x,y,title,source in [
        (760,451,'Dentist moved to Tuesday','Calendar change'),
        (835,290,'Maya likes oat lattes','A conversation'),
        (713,129,'Renew the lease in June','An email'),
    ]:
        c.setFillColor(HexColor(WHITE));c.setStrokeColor(HexColor('#D9DFEF'))
        c.setLineWidth(1);c.roundRect(x,y,390,115,12,fill=1,stroke=1)
        text(c,title,x+23,y+67,24,INK,'Strong')
        text(c,source,x+23,y+30,19,MUTED)
    text(c,'Illustrative memories. You choose what to share.',72,77,14,MUTED)
    c.showPage()

    start(c,3,'A morning with Ev')
    text(c,'The detail becomes useful.',68,532,52)
    text(c,'YOU',72,404,15,BLUE,'Strong')
    text(c,'"What do I need to remember?"',72,350,41)
    line(c,72,303,1208,'#D9DFEF')
    text(c,'EV',72,255,15,BLUE,'Strong')
    paragraph(c,'"Your dentist appointment moved to Tuesday. Want me to update the reminder?"',72,201,1095,35,INK)
    text(c,'Remembers the change.',72,102,22,BLUE)
    text(c,'Keeps the source.',520,102,22,BLUE)
    text(c,'Asks before acting.',925,102,22,BLUE)
    text(c,'Illustrative experience, not a customer result.',72,47,13,MUTED)
    c.showPage()

    start(c,4,'The business')
    text(c,'One companion.',68,525,55)
    text(c,'$19',68,315,119,BLUE)
    text(c,'a month',79,250,32)
    text(c,'Proposed subscription',80,194,16,MUTED)
    paragraph(c,'For people juggling work, plans and everyday life.',642,433,540,36)
    paragraph(c,'Your memory stays on your system. You choose what connected AI can use.',646,273,520,26)
    text(c,'Software, updates and support.',80,126,22)
    text(c,'Owners provide hardware. AI usage allowance and delivery costs still need testing.',72,65,14,MUTED)
    c.showPage()

    start(c,5,'The next step',True)
    text(c,'Ev remembers.',68,521,62,WHITE)
    text(c,'You live.',68,438,62,'#C9D4FF')
    paragraph(c,'Help bring Ev into everyday life.',72,337,570,33,WHITE)
    text(c,'~$275K',779,450,82,WHITE)
    text(c,'Proposed grant funding',790,391,21,'#BDC8E2')
    line(c,72,273,1205,'#586990')
    text(c,'Polish the experience.',72,211,27,WHITE)
    text(c,'Validate reliability.',502,211,27,WHITE)
    text(c,'Start with early owners.',895,211,27,WHITE)
    paragraph(c,'Memory, privacy controls and client software exist. The next proof is daily use and willingness to pay.',72,126,1080,22,'#BDC8E2')
    c.showPage();c.save()
    print(f'{OUT}: 5 pages; {len(checks)} text lines checked')

if __name__=='__main__':build()
