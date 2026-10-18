"""Five-page EV pitch. Run: python3 pitch-deck/build_short_deck.py."""
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from build_tech_deck import ROOT, W, H, NAVY, WHITE, GRAY, ICE, t, p, rule, bg

OUT = ROOT / 'EV_Pitch_5_Pages.pdf'
BLUE = '#3454E8'


def build():
    c = canvas.Canvas(str(OUT), pagesize=(W,H), pageCompression=1)
    c.setTitle('EV | Five-page investor pitch')
    c.setAuthor('EV')

    bg(c,1,'Personal AI',NAVY)
    t(c,'Pick up where',64,489,65,WHITE,'Display')
    t(c,'you left off.',64,400,65,WHITE,'Display')
    # Typographic wordmark, no conceptual product render.
    t(c,'EV',845,405,170,WHITE,'Display')
    p(c,'An AI companion that remembers your context and helps you act with permission.',68,275,725,29)
    t(c,'Your decisions. Your history. Across your devices.',68,139,23,GRAY)
    c.showPage()

    bg(c,2,'One moment with EV',WHITE,light=True)
    t(c,'Three weeks later,',64,542,54,NAVY,'Display')
    t(c,'the reason is still there.',64,470,54,BLUE,'Display')
    t(c,'YOU, MONDAY',64,351,14,BLUE,'Bold')
    p(c,'"Delay launch until payments work."',64,297,420,33,NAVY,'Bold')
    rule(c,548,340,548,170,BLUE,2)
    t(c,'YOU, THREE WEEKS LATER',633,351,14,BLUE,'Bold')
    t(c,'"Why did I delay launch?"',633,297,33,NAVY,'Bold')
    t(c,'EV recalls the payment blocker.',633,218,26,NAVY)
    t(c,'Source: your Monday note',633,170,19,BLUE)
    t(c,'Illustrative interaction. Open the source or correct the memory.',64,78,15,NAVY)
    c.showPage()

    bg(c,3,'Why EV',NAVY)
    t(c,'The memory stays yours.',64,532,58,WHITE,'Display')
    t(c,'Your history',64,358,37,WHITE,'Bold')
    t(c,'EV',583,352,58,WHITE,'Display')
    t(c,'AI models',934,358,37,WHITE,'Bold')
    # Semantic topology: the owner memory persists across model providers.
    rule(c,315,369,536,369,BLUE,3)
    rule(c,696,369,889,369,BLUE,3)
    t(c,'See the source',64,222,28,ICE,'Bold')
    t(c,'Correct the record',469,222,28,ICE,'Bold')
    t(c,'Choose the model',904,222,28,ICE,'Bold')
    t(c,'Local-first storage. Permissioned cloud reasoning.',64,139,23,GRAY)
    t(c,'Memory and permission systems implemented. Daily reliability still to prove.',64,78,15,GRAY)
    c.showPage()

    bg(c,4,'The business',BLUE)
    t(c,'$19',64,423,148,WHITE,'Display')
    t(c,'per owner / month',76,358,30,WHITE,'Bold')
    t(c,'PROPOSED SUBSCRIPTION',78,305,15,WHITE,'Bold')
    p(c,'For independent professionals with work to remember.',635,495,560,43,WHITE,'Display')
    p(c,'Start with project recall. Earn repeat use.',639,256,515,29,WHITE)
    t(c,'Software, updates and support.',78,162,26,WHITE)
    t(c,'Owners provide hardware. AI usage allowance or separate charges to be tested.',64,78,15,WHITE)
    c.showPage()

    bg(c,5,'The ask',NAVY)
    t(c,'$275K',64,480,104,WHITE,'Display')
    t(c,'to prove everyday value',69,410,40,WHITE,'Bold')
    t(c,'12 months',69,287,43,ICE,'Bold')
    t(c,'50 pilot owners',652,287,43,ICE,'Bold')
    rule(c,69,239,1183,239,BLUE,3)
    t(c,'Simpler setup.',69,175,29,WHITE,'Bold')
    t(c,'Trusted recall.',473,175,29,WHITE,'Bold')
    t(c,'Measurable repeat use.',824,175,29,WHITE,'Bold')
    t(c,'Proposed grant or pre-seed program. Pilot and runway are targets, not achieved results.',64,78,15,GRAY)
    c.showPage()
    c.save()
    print(OUT)


if __name__ == '__main__':
    build()
